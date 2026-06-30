#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any


PHASE_ZETA_ROOT = Path(__file__).resolve().parent
if str(PHASE_ZETA_ROOT) not in sys.path:
    sys.path.insert(0, str(PHASE_ZETA_ROOT))

from phase_zeta_runner.config import RunnerConfig
from phase_zeta_runner.llm_runner import LLMResult, call_llm
from phase_zeta_runner.output_composer import compose_and_persist
from phase_zeta_runner.prompt_builder import build_question_string
from phase_zeta_runner.run_pipeline import run_full_validation


def _json_dumps(obj: Any, indent: int | None = 2) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=indent, default=str)


def _connect_db(config: RunnerConfig):
    import pymysql

    return pymysql.connect(
        host=config.composer.db_host,
        port=config.composer.db_port,
        user=os.environ.get("DB_USER", config.composer.db_user),
        password=os.environ.get("DB_ROOT_PASSWORD", ""),
        database=config.composer.db_name,
        charset="utf8mb4",
        autocommit=False,
    )


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, payload: Any) -> None:
    _write_text(path, _json_dumps(payload) + "\n")


def _render_preview(parsed_output: dict[str, Any], brand: str, run_id: int | None) -> str:
    lines = [
        f"# Phase ζ Stage 3-C Dry-Test Preview — {brand}",
        "",
        f"- run_id: {run_id if run_id is not None else 'N/A'}",
        "",
    ]
    labels = {
        "phenomenon": "1. 현상",
        "cause": "2. 원인",
        "prediction": "3. 예측",
        "recommendation": "4. 권고",
    }
    for stage, label in labels.items():
        stage_data = parsed_output.get(stage, {}) or {}
        lines.append(f"## {label}")
        lines.append("")
        lines.append(f"### {stage_data.get('title', '')}")
        lines.append("")
        lines.append(str(stage_data.get("body", "")))
        lines.append("")
        for bullet in stage_data.get("bullets", []) or []:
            lines.append(f"- {bullet}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _override_cache_setting(config: RunnerConfig, update_cache: bool) -> RunnerConfig:
    if not update_cache:
        return config
    return replace(config, composer=replace(config.composer, update_cache_deep_analysis=True))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Phase ζ unified Gemini analysis for one brand.")
    parser.add_argument("--brand", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--bundle-path", required=True)
    parser.add_argument("--update-cache", action="store_true", default=False)
    parser.add_argument("--audit-dir", default=None)
    parser.add_argument("--use-cached-run-id", type=int, default=None)
    parser.add_argument("--analysis-variant", choices=["legacy", "short", "long"], default="legacy")
    return parser.parse_args()


def _load_cached_result(db_conn, run_id: int) -> LLMResult:
    cursor = db_conn.cursor()
    cursor.execute(
        """
        SELECT model_version, total_tokens_in, total_tokens_out, cost_usd, duration_sec, status
        FROM zeta_analysis_runs
        WHERE run_id = %s
        """,
        (run_id,),
    )
    run_row = cursor.fetchone()
    if not run_row:
        raise ValueError(f"zeta_analysis_runs.run_id={run_id} not found")
    if not isinstance(run_row, dict):
        columns = [desc[0] for desc in cursor.description]
        run_row = dict(zip(columns, run_row))

    cursor.execute(
        """
        SELECT stage, raw_response
        FROM zeta_analysis_outputs
        WHERE run_id = %s
        """,
        (run_id,),
    )
    output_rows = cursor.fetchall()
    parsed: dict[str, Any] = {}
    for row in output_rows:
        if not isinstance(row, dict):
            columns = [desc[0] for desc in cursor.description]
            row = dict(zip(columns, row))
        raw_stage = row.get("raw_response") or "{}"
        parsed[str(row["stage"])] = json.loads(raw_stage) if isinstance(raw_stage, str) else raw_stage

    return LLMResult(
        success=bool(parsed),
        parsed_output=parsed,
        raw_response=_json_dumps({"cached_run_id": run_id, "parsed_output": parsed}, indent=None),
        tokens_in=int(run_row.get("total_tokens_in") or 0),
        tokens_out=int(run_row.get("total_tokens_out") or 0),
        cost_usd=float(run_row.get("cost_usd") or 0.0),
        duration_sec=float(run_row.get("duration_sec") or 0.0),
        model_version=str(run_row.get("model_version") or f"cached_run_{run_id}"),
        retry_count=0,
        error=None if parsed else f"cached run {run_id} has no outputs",
    )


def main() -> int:
    args = parse_args()
    config = _override_cache_setting(
        RunnerConfig.from_yaml(args.config).with_analysis_variant(args.analysis_variant),
        args.update_cache,
    )
    bundle_path = Path(args.bundle_path)
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    audit_dir = Path(args.audit_dir) if args.audit_dir else Path("outputs/phase_zeta_stage3c/dry_test")
    audit_dir.mkdir(parents=True, exist_ok=True)

    snapshot_at = datetime.now()
    question = build_question_string(bundle, config)
    _write_text(audit_dir / f"{args.brand}_question.txt", question)

    cached_run_id = args.use_cached_run_id
    gemini_result: LLMResult

    composition = None
    try:
        with _connect_db(config) as db_conn:
            if cached_run_id is not None:
                gemini_result = _load_cached_result(db_conn, cached_run_id)
            else:
                gemini_result = call_llm(bundle, config)
            validation_result = run_full_validation(gemini_result.parsed_output, bundle, db_conn, config)
            if cached_run_id is None:
                composition = compose_and_persist(
                    args.brand,
                    snapshot_at,
                    bundle,
                    gemini_result,
                    validation_result,
                    config,
                    db_conn,
                )
    except Exception as exc:
        composition_error = f"{type(exc).__name__}: {exc}"
        if "gemini_result" not in locals():
            gemini_result = LLMResult(
                success=False,
                parsed_output={},
                raw_response="",
                tokens_in=0,
                tokens_out=0,
                cost_usd=0.0,
                duration_sec=0.0,
                model_version=f"genos_workflow_{config.genos.workflow_id}",
                retry_count=0,
                error=composition_error,
            )
        from phase_zeta_runner.metric_validator import validate_output

        validation_result = validate_output(gemini_result.parsed_output, bundle, config.validator)
    else:
        composition_error = composition.error if composition else None

    run_id = cached_run_id if cached_run_id is not None else (composition.run_id if composition else None)
    _write_json(audit_dir / f"{args.brand}_genos_response.json", {"text": gemini_result.raw_response})
    _write_json(audit_dir / f"{args.brand}_gemini_raw_response.json", {"text": gemini_result.raw_response})
    _write_json(audit_dir / f"{args.brand}_parsed_output.json", gemini_result.parsed_output)
    _write_json(audit_dir / f"{args.brand}_validation_result.json", validation_result.to_dict())
    _write_text(audit_dir / f"{args.brand}_narrative_preview.md", _render_preview(gemini_result.parsed_output, args.brand, run_id))

    summary = {
        "brand": args.brand,
        "bundle_path": str(bundle_path),
        "bundle_hash": bundle.get("bundle_meta", {}).get("bundle_hash"),
        "run_id": run_id,
        "status": composition.status if composition else ("ok" if cached_run_id is not None and validation_result.valid else "failed"),
        "validation_verdict": getattr(validation_result, "summary", {}).get("verdict", "PASS" if validation_result.valid else "FAIL"),
        "composition_error": composition_error,
        "gemini_success": gemini_result.success,
        "gemini_error": gemini_result.error,
        "validation_valid": validation_result.valid,
        "unmatched_numbers_count": len(validation_result.unmatched_numbers),
        "tokens_in": gemini_result.tokens_in,
        "tokens_out": gemini_result.tokens_out,
        "cost_usd": gemini_result.cost_usd,
        "duration_sec": gemini_result.duration_sec,
        "model_version": gemini_result.model_version,
    }
    _write_text(
        audit_dir / f"{args.brand}_run_summary.txt",
        "\n".join(f"{key}: {value}" for key, value in summary.items()) + "\n",
    )
    _write_json(audit_dir / f"{args.brand}_run_summary.json", summary)
    print(_json_dumps(summary))
    return 0 if gemini_result.success and (composition is not None or cached_run_id is not None) else 1


if __name__ == "__main__":
    raise SystemExit(main())
