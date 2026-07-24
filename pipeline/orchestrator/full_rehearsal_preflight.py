"""Fail-closed checks that run before the expensive R-1 rehearsal."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pipeline.orchestrator.full_rehearsal import (
    FullInputManifest,
    FullRehearsalConfig,
    RehearsalStep,
    build_full_rehearsal_plan,
)
from pipeline.orchestrator.full_rehearsal_preflight_db import (
    check_database_readiness,
)
from pipeline.orchestrator.full_rehearsal_preflight_inputs import (
    bounded_input_failures,
    capacity_failures,
)
from pipeline.orchestrator.iqvia_roles import (
    IqviaRoleContractError,
    bind_iqvia_sources,
    canonical_nsa_source,
)


REQUIRED_ENV_KEYS = frozenset(
    {
        "R1_RUN_ID",
        "R1_SOURCE_SUBPATH",
        "R1_IMAGE_DIGEST",
        "R1_GIT_COMMIT",
        "MARIADB_HOST",
        "MARIADB_PORT",
        "MARIADB_USER",
        "MARIADB_PASSWORD",
        "CACHE_MARIADB_USER",
        "MYSQL_PWD",
        "DB_HOST",
        "DB_PORT",
        "DB_USER",
        "DB_PASSWORD",
    }
)
REQUIRED_ASSETS = (
    Path("data/cache/prototype_11_step_c4_target_priority_precompute_sample.csv"),
    Path("inputs/molecule_v4_worklist.csv"),
)
PLACEHOLDER_RE = re.compile(r"^REPLACE_WITH_")


@dataclass(frozen=True)
class Finding:
    check_id: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "check": self.check_id,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PreflightRequest:
    rehearsal: FullRehearsalConfig
    inventory_path: Path
    job_manifest_path: Path
    evidence_dir: Path
    repo_root: Path
    environment: Mapping[str, str]


def _finding(check_id: str, failures: list[str], success: str) -> Finding:
    return Finding(check_id, not failures, "; ".join(failures) if failures else success)


def check_sidecar_exclusions(
    inputs: FullInputManifest, plan: tuple[RehearsalStep, ...]
) -> Finding:
    expected = {
        f"{int(part.split('=', 1)[1]):04d}-{int(month.split('=', 1)[1]):02d}"
        for sidecar in inputs.ubist_parquet_sidecars
        for part in sidecar.relative_path.parts
        for month in sidecar.relative_path.parts
        if part.startswith("year=") and month.startswith("month=")
    }
    load = next((step for step in plan if step.key == "load_ubist"), None)
    actual: set[str] = set()
    if load:
        for index, arg in enumerate(load.argv[:-1]):
            if arg == "--exclude-ubist-month":
                actual.add(load.argv[index + 1])
    missing = sorted(expected - actual)
    failures = [f"sidecar months not excluded from s1: {','.join(missing)}"] if missing else []
    return _finding("4-sidecar-exclusions", failures, f"excluded={len(actual)}")


def check_required_environment(environment: Mapping[str, str]) -> Finding:
    missing = sorted(key for key in REQUIRED_ENV_KEYS if key not in environment)
    placeholders = sorted(
        key
        for key in ("R1_RUN_ID", "R1_SOURCE_SUBPATH", "R1_IMAGE_DIGEST", "R1_GIT_COMMIT")
        if key in environment and PLACEHOLDER_RE.match(environment[key])
    )
    failures = []
    if missing:
        failures.append(f"missing env keys: {','.join(missing)}")
    if placeholders:
        failures.append(f"placeholder env keys: {','.join(placeholders)}")
    digest = environment.get("R1_IMAGE_DIGEST", "")
    commit = environment.get("R1_GIT_COMMIT", "")
    if digest and not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        failures.append("R1_IMAGE_DIGEST is not a pinned sha256")
    if commit and not re.fullmatch(r"[0-9a-f]{40}", commit):
        failures.append("R1_GIT_COMMIT is not a full commit SHA")
    return _finding("1-runtime-identity", failures, f"present_keys={len(REQUIRED_ENV_KEYS)}")


def check_unicode_paths(inputs: FullInputManifest) -> Finding:
    roots = (inputs.ubist_source_dir, inputs.iqvia_source_dir)
    paths = [inputs.mi_master]
    for root in roots:
        paths.extend(path for path in root.rglob("*") if path.is_file())
    invalid = sorted(
        str(path)
        for path in paths
        if unicodedata.normalize("NFC", str(path)) != str(path)
    )
    failures = [f"non-NFC input paths: {len(invalid)}"] if invalid else []
    return _finding("3-unicode-paths", failures, f"checked={len(paths)}")


def check_iqvia_source_roles(inputs: FullInputManifest) -> Finding:
    try:
        sources = bind_iqvia_sources(inputs.iqvia_source_dir)
        canonical = canonical_nsa_source(sources)
    except IqviaRoleContractError as exc:
        return Finding("3b-iqvia-source-roles", False, str(exc))
    roles: dict[str, int] = {}
    for source in sources:
        roles[source.role] = roles.get(source.role, 0) + 1
    detail = (
        f"canonical_nsa={canonical.relative_path.as_posix()} "
        f"roles={json.dumps(roles, sort_keys=True)}"
    )
    return Finding("3b-iqvia-source-roles", True, detail)


def check_required_assets(repo_root: Path) -> Finding:
    missing = [str(path) for path in REQUIRED_ASSETS if not (repo_root / path).is_file()]
    return _finding("5-required-assets", missing, f"checked={len(REQUIRED_ASSETS)}")


def check_job_contract(manifest: str) -> Finding:
    clauses = {
        "durable tee": "2>&1 | tee" in manifest and "set -euo pipefail" in manifest,
        "evidence PVC": "claimName: r1-evidence-nfs" in manifest
        and "mountPath: /work/evidence" in manifest,
        "checkpoint PVC": "claimName: r1-checkpoint-nfs" in manifest
        and "mountPath: /work/checkpoints" in manifest,
        "TTL": "ttlSecondsAfterFinished: 86400" in manifest,
        "node pin": "knp-jw-agn-dev-genos-api-01" in manifest,
        "stage markers": "[stage]" in manifest or "rehearse-full" in manifest,
        "single-run lock": 'mkdir "${R1_LOCK_DIR}"' in manifest
        and "exit 75" in manifest,
        "preflight before s1": (
            "python -m pipeline.orchestrator preflight-full" in manifest
            and manifest.index("python -m pipeline.orchestrator preflight-full")
            < manifest.index("python -m pipeline.orchestrator rehearse-full")
        ),
    }
    failures = [name for name, passed in clauses.items() if not passed]
    return _finding("7-job-durability", failures, f"clauses={len(clauses)}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_inventory(inputs: FullInputManifest, inventory_path: Path) -> Finding:
    failures: list[str] = []
    try:
        payload = json.loads(inventory_path.read_text(encoding="utf-8"))
        rows = payload["objects"]
        raw_buckets = payload.get("raw_buckets")
        if (
            payload.get("schema_version") != 2
            or not isinstance(raw_buckets, dict)
            or set(raw_buckets) != {"ubist", "iqvia"}
            or not all(isinstance(value, str) and value for value in raw_buckets.values())
            or len(set(raw_buckets.values())) != 2
        ):
            failures.append("inventory raw bucket roles are invalid")
            raw_buckets = {}
        if payload.get("classification") != "census" or payload.get("population") != len(rows):
            failures.append("inventory census metadata mismatch")
        roots = {
            raw_buckets.get("ubist"): inputs.ubist_source_dir,
            raw_buckets.get("iqvia"): inputs.iqvia_source_dir,
        }
        for row in rows:
            bucket, key = row["bucket"], row["key"]
            if bucket in roots:
                path = roots[bucket] / key
            elif bucket == "repository":
                path = inputs.mi_master
            elif bucket == "pvc-sidecar":
                match = [s.path for s in inputs.ubist_parquet_sidecars if str(s.relative_path) == key]
                path = match[0] if match else Path("__missing__")
            else:
                failures.append(f"unknown inventory bucket: {bucket}")
                continue
            if not path.is_file():
                failures.append(f"missing inventory object: {bucket}/{key}")
            elif path.stat().st_size != row["size"] or _sha256(path) != row["sha256"]:
                failures.append(f"inventory identity mismatch: {bucket}/{key}")
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        failures.append(f"invalid input inventory: {type(exc).__name__}")
    return _finding("2-input-census", failures, "inventory census matched")


def check_bounded_inputs(inputs: FullInputManifest) -> Finding:
    checked, failures = bounded_input_failures(inputs)
    return _finding("8-bounded-parse", failures, f"checked={checked}")


def check_plan(plan: tuple[RehearsalStep, ...]) -> Finding:
    failures: list[str] = []
    if not plan or plan[0].key != "load_ubist":
        failures.append("s1 UBIST is not the first rehearsal step")
    for step in plan:
        if "-m" in step.argv:
            module = step.argv[step.argv.index("-m") + 1]
            if importlib.util.find_spec(module) is None:
                failures.append(f"missing module for {step.key}: {module}")
    return _finding("9-plan-render", failures, f"steps={len(plan)}")


def check_capacity(
    config: FullRehearsalConfig,
    inputs: FullInputManifest,
    evidence_dir: Path,
) -> Finding:
    source_size, failures = capacity_failures(config, inputs, evidence_dir)
    return _finding("0-isolation-capacity", failures, f"source_bytes={source_size}")


def check_database(environment: Mapping[str, str], config: FullRehearsalConfig) -> Finding:
    passed, detail = check_database_readiness(environment, config)
    return Finding("6-database-readiness", passed, detail)


def run_preflight(request: PreflightRequest) -> tuple[Finding, ...]:
    inputs = request.rehearsal.validate()
    plan = build_full_rehearsal_plan(request.rehearsal)
    return (
        check_capacity(request.rehearsal, inputs, request.evidence_dir),
        check_required_environment(request.environment),
        check_inventory(inputs, request.inventory_path),
        check_unicode_paths(inputs),
        check_iqvia_source_roles(inputs),
        check_sidecar_exclusions(inputs, plan),
        check_required_assets(request.repo_root),
        check_database(request.environment, request.rehearsal),
        check_job_contract(request.job_manifest_path.read_text(encoding="utf-8")),
        check_bounded_inputs(inputs),
        check_plan(plan),
    )
