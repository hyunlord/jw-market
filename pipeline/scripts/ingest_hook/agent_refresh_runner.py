"""Run agent-derived refreshes after numeric ingest has committed."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from datetime import datetime, timezone
from typing import Literal

from pipeline.scripts.ingest_hook import config

NumericSource = Literal["ubist", "iqvia_nsa"]
AGENT2_OUTPUT_ROOT = Path("outputs/phase_zeta_agent2_regen_orchestrator")


@dataclass(frozen=True, slots=True)
class ResolvedAgentScope:
    source: NumericSource
    market_ids: tuple[str, ...]
    brand_keys: tuple[str, ...]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scope_source(category: str) -> NumericSource:
    match category:
        case "ubist":
            return "ubist"
        case "iqvia_nsa":
            return "iqvia_nsa"
        case _:
            raise ValueError(f"category has no numeric Agent scope: {category}")


def resolve_affected_scope(
    *,
    category: str,
    affected_scope: dict[str, object],
) -> ResolvedAgentScope:
    source = _scope_source(category)
    dimension = affected_scope.get("dimension")
    raw_values = affected_scope.get("values")
    count = affected_scope.get("count")
    if not isinstance(raw_values, list) or not all(
        isinstance(value, str) and value.strip() for value in raw_values
    ):
        raise ValueError("affected_scope values must be non-empty strings")
    if count != len(raw_values):
        raise ValueError("affected_scope count must match values")

    match dimension:
        case "atc4":
            market_ids = tuple(sorted({value.strip().upper() for value in raw_values}))
        case "source":
            if set(raw_values) != {category}:
                raise ValueError("source scope must contain the terminal category")
            market_ids = ()
        case _:
            raise ValueError(f"unsupported affected_scope dimension: {dimension!r}")

    connection = config.open_mart_connection()
    try:
        with connection.cursor() as cursor:
            sql = (
                "SELECT DISTINCT brand_key FROM mart_general_brand_metric "
                "WHERE source=%s AND measure='sales' "
                "AND brand_key IS NOT NULL AND brand_key<>''"
            )
            params: tuple[str, ...] = (source,)
            if market_ids:
                placeholders = ", ".join(["%s"] * len(market_ids))
                sql += f" AND UPPER(atc4_code) IN ({placeholders})"
                params += market_ids
            sql += " ORDER BY brand_key"
            cursor.execute(sql, params)
            brand_keys = tuple(str(row["brand_key"]) for row in cursor.fetchall())
    finally:
        connection.close()
    if not brand_keys:
        raise ValueError("affected_scope resolved to zero numeric brands")
    return ResolvedAgentScope(
        source=source,
        market_ids=market_ids,
        brand_keys=brand_keys,
    )


def _agent2_unknown_brand_skips(run_id: str) -> tuple[str, ...]:
    names: set[str] = set()
    manifests_found = 0
    for variant in ("short", "long"):
        manifest_path = (
            AGENT2_OUTPUT_ROOT
            / f"orchestrated_{run_id}_{variant}"
            / "run_manifest.json"
        )
        if not manifest_path.is_file():
            continue
        manifests_found += 1
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        density = (manifest.get("diagnostics") or {}).get("density_worklist") or {}
        raw_names = density.get("unmatched_unknown") or []
        if not isinstance(raw_names, list) or not all(
            isinstance(name, str) for name in raw_names
        ):
            raise ValueError(f"invalid Agent2 unmatched_unknown manifest: {manifest_path}")
        names.update(name for name in raw_names if name)
    if manifests_found != 2:
        raise FileNotFoundError(
            f"expected 2 Agent2 run manifests for {run_id}, found {manifests_found}"
        )
    return tuple(sorted(names))


def run(
    *,
    epoch: str,
    category: str,
    manifest_sha: str,
    ingest_run_id: str,
    agent_run_id: str | None = None,
    reuse_forecast_staging: bool = False,
    resume_from_agent2: bool = False,
    affected_scope: dict[str, object] | None = None,
    agent2_resume_snapshot_at: str | None = None,
    agent2_fail_threshold: int | None = None,
) -> int:
    if resume_from_agent2 and not (reuse_forecast_staging and affected_scope is not None):
        raise ValueError(
            "resume_from_agent2 requires reuse_forecast_staging and affected_scope"
        )
    if (agent2_resume_snapshot_at is not None or agent2_fail_threshold is not None) and not resume_from_agent2:
        raise ValueError("Agent2 retry controls require resume_from_agent2")
    if agent2_fail_threshold is not None and agent2_fail_threshold < 0:
        raise ValueError("agent2_fail_threshold must be non-negative")
    ledger = config.open_configured_ledger()
    run_id = agent_run_id or f"{ingest_run_id}:agent-refresh"
    started_at = _now()
    ledger.record_stage(
        epoch,
        category,
        manifest_sha,
        run_id=run_id,
        seq=1,
        stage="agent_refresh",
        status="running",
        started_at=started_at,
    )
    command = [
        sys.executable,
        "-m",
        "pipeline.orchestrator",
        "run",
        "--mode",
        "incremental",
        "--profile",
        "agent",
        "--run-id",
        run_id.replace(":", "-"),
    ]
    if resume_from_agent2:
        profile_index = command.index("--profile")
        command[profile_index : profile_index + 2] = [
            "--stages",
            "shortlong,elements",
            "--force",
        ]
    elif reuse_forecast_staging and affected_scope is not None:
        profile_index = command.index("--profile")
        command[profile_index : profile_index + 2] = [
            "--stages",
            "strength,shortlong,elements",
            "--force",
        ]
    elif not reuse_forecast_staging:
        command.insert(-2, "--force")
    resolved_scope = None
    skipped_unknown: tuple[str, ...] = ()
    child_env = None
    if agent2_resume_snapshot_at is not None:
        child_env = os.environ.copy()
        child_env["AGENT2_RECOVERY_SNAPSHOT_AT"] = agent2_resume_snapshot_at
        if agent2_fail_threshold is not None:
            child_env["AGENT2_RECOVERY_FAIL_THRESHOLD"] = str(agent2_fail_threshold)
    try:
        if affected_scope is not None:
            resolved_scope = resolve_affected_scope(
                category=category,
                affected_scope=affected_scope,
            )
            command.extend(["--scope-source", resolved_scope.source])
            if resolved_scope.market_ids:
                command.extend(
                    ["--scope-market-ids", ",".join(resolved_scope.market_ids)]
                )
            with TemporaryDirectory(prefix="agent-affected-scope-") as temp_dir:
                brands_file = Path(temp_dir) / "brand-keys.json"
                brands_file.write_text(
                    json.dumps(resolved_scope.brand_keys, ensure_ascii=False),
                    encoding="utf-8",
                )
                command.extend(["--brands-file", str(brands_file)])
                result = (
                    subprocess.run(command, check=False, env=child_env)
                    if child_env is not None
                    else subprocess.run(command, check=False)
                )
        else:
            result = (
                subprocess.run(command, check=False, env=child_env)
                if child_env is not None
                else subprocess.run(command, check=False)
            )
        returncode = result.returncode
        skipped_unknown = (
            _agent2_unknown_brand_skips(run_id.replace(":", "-"))
            if returncode == 0 and resume_from_agent2
            else ()
        )
        scope_reason = (
            None
            if resolved_scope is None
            else (
                f"scope source={resolved_scope.source} "
                f"markets={len(resolved_scope.market_ids)} "
                f"brands={len(resolved_scope.brand_keys)}"
            )
        )
        skip_reason = None
        if resume_from_agent2:
            skip_reason = (
                f"skipped_unknown={len(skipped_unknown)} "
                f"names={','.join(skipped_unknown) or '[]'}"
            )
        reason = (
            "; ".join(part for part in (scope_reason, skip_reason) if part) or None
            if returncode == 0
            else f"orchestrator rc={returncode}"
        )
    except Exception as exc:
        returncode = 1
        reason = f"{type(exc).__name__}: {exc}"
    finished_at = _now()
    status = "complete" if returncode == 0 else "failed"
    ledger.record_stage(
        epoch,
        category,
        manifest_sha,
        run_id=run_id,
        seq=1,
        stage="agent_refresh",
        status=status,
        reason=reason,
        started_at=started_at,
        finished_at=finished_at,
    )
    if returncode == 0:
        for seq, stage in enumerate(("agent3", "agent2", "dashboard"), start=2):
            if stage == "agent3" and resume_from_agent2:
                stage_reason = "reused prior successful strength stage; substage timing unavailable"
            elif stage == "agent2" and resume_from_agent2:
                stage_reason = (
                    "derived from successful aggregate agent_refresh; "
                    f"skipped_unknown={len(skipped_unknown)} "
                    f"names={','.join(skipped_unknown) or '[]'}; substage timing unavailable"
                )
            else:
                stage_reason = "derived from successful aggregate agent_refresh; substage timing unavailable"
            ledger.record_stage(
                epoch,
                category,
                manifest_sha,
                run_id=run_id,
                seq=seq,
                stage=stage,
                status="complete",
                reason=stage_reason,
                started_at=finished_at,
                finished_at=finished_at,
                duration_ms=0,
            )
    return returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--epoch", required=True)
    parser.add_argument("--category", required=True)
    parser.add_argument("--manifest-sha", required=True)
    parser.add_argument("--ingest-run-id", required=True)
    parser.add_argument("--agent-run-id")
    parser.add_argument("--affected-scope-json")
    parser.add_argument(
        "--reuse-forecast-staging",
        action="store_true",
        help="resume a failed forecast from matching staging rows instead of forcing recomputation",
    )
    parser.add_argument(
        "--resume-from-agent2",
        action="store_true",
        help="reuse a completed Agent3 strength stage and resume at Agent2",
    )
    parser.add_argument("--agent2-resume-snapshot-at")
    parser.add_argument("--agent2-fail-threshold", type=int)
    args = parser.parse_args(argv)
    affected_scope = (
        json.loads(args.affected_scope_json)
        if args.affected_scope_json is not None
        else None
    )
    if affected_scope is not None and not isinstance(affected_scope, dict):
        parser.error("--affected-scope-json must be a JSON object")
    return run(
        epoch=args.epoch,
        category=args.category,
        manifest_sha=args.manifest_sha,
        ingest_run_id=args.ingest_run_id,
        agent_run_id=args.agent_run_id,
        reuse_forecast_staging=args.reuse_forecast_staging,
        resume_from_agent2=args.resume_from_agent2,
        affected_scope=affected_scope,
        agent2_resume_snapshot_at=args.agent2_resume_snapshot_at,
        agent2_fail_threshold=args.agent2_fail_threshold,
    )


if __name__ == "__main__":
    raise SystemExit(main())
