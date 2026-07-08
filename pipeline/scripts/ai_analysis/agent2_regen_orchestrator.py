#!/usr/bin/env python3
"""Agent 2 regeneration orchestrator.

This module wires the existing Phase ζ bundle builder, GenOS runner,
validator, and non-live staging tables into a single regeneration flow.
The default mode is dry-run: it may build bundles, call wf217, validate,
and persist to zeta_analysis_runs/outputs, but it does not swap live cache.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, assert_never


PHASE_ZETA_ROOT = Path(__file__).resolve().parent
if str(PHASE_ZETA_ROOT) not in sys.path:
    sys.path.insert(0, str(PHASE_ZETA_ROOT))

from bundle_builder import BundleConfig, build_brand_bundle
from bundle_builder.agent2_density_router import ProcessingMode
from bundle_builder.agent2_zero_template import KpiSnapshot, render_zero_template
from bundle_builder.hash_util import compute_bundle_hash
from agent2_density_worklist import RoutedAgent2Brand, load_density_worklist
from agent2_processing_modes import (
    PROCESSING_MODE_FULL,
    formatter_policy_for_mode,
    normalize_processing_mode,
    trim_bundle_for_mode,
)
from phase_zeta_runner.config import RunnerConfig
from phase_zeta_runner.llm_runner import call_llm
from phase_zeta_runner.output_composer import compose_and_persist
from phase_zeta_runner.run_pipeline import FullValidationResult, run_full_validation


STAGES = ("phenomenon", "cause", "prediction", "recommendation")
DEFAULT_FORMATTER_VERSION = "wf217-order2-v10.3"
DEFAULT_WORKFLOW_REVISION_ID = 3727
DUAL_SOURCE_BRANDS = frozenset({"가드렛", "가드메트", "엔커버"})

TAG_RE = re.compile(r"\((ML|CD)·([^()·]+)·([^()·]+)(?:·([^()·]+))?\)")
PERIOD_RE = re.compile(r"^20\d{2}-(?:Q[1-4]|\d{2})$")
DAMAGED_DATE_RE = re.compile(r"(?:2,0\d{2}\.00|20\d{2}\.00|\d,0\d{2}\.00-\d|\.00\.00)")
THREE_PLUS_DECIMAL_RE = re.compile(r"(?<!\d)\d[\d,]*\.\d{3,}\s*(?:%p|%|배)?")
KRW_QTY_DECIMAL_RE = re.compile(r"(?<!\d)\d[\d,]*\.\d+\s*(?:원|개)")
SOURCE_PERIOD_PREFIX_RE = re.compile(r"(IQVIA|UBIST)\s*(20\d{2}-(?:Q[1-4]|\d{2}))\s*기준(?:으로는|으로)?\s*")
HEX_NEWS_ID_RE = re.compile(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{16}\b", re.IGNORECASE)
FORBIDDEN_CERTAINTY_RE = re.compile(r"(반드시|확실히|틀림없이|분명히\s*(?:성장|하락|감소|증가))")


def _json_dumps(obj: Any, indent: int | None = 2) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=indent, default=str)


def compute_idempotency_key(
    brand: str,
    bundle_hash: str,
    workflow_revision_id: int | str,
    formatter_version: str,
) -> str:
    return f"{brand}|{bundle_hash}|rev:{workflow_revision_id}|formatter:{formatter_version}"


@dataclass
class LLMCallResult:
    success: bool
    parsed_output: dict[str, Any]
    raw_response: str
    tokens_in: int
    tokens_out: int
    duration_sec: float
    model_version: str
    retry_count: int
    error: str | None = None


@dataclass
class ValidationOutcome:
    valid: bool
    summary: dict[str, Any]
    details: dict[str, Any]


@dataclass
class FormatterContractResult:
    valid: bool
    errors: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JsonRunStore:
    """Small JSON idempotency ledger used by manual dry-runs and future Jobs."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"success_by_key": {}, "records": []}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, payload: dict[str, Any]) -> None:
        self.path.write_text(_json_dumps(payload) + "\n", encoding="utf-8")

    def get_success(self, idempotency_key: str) -> dict[str, Any] | None:
        return self.load().get("success_by_key", {}).get(idempotency_key)

    def record(self, idempotency_key: str, record: dict[str, Any], success: bool) -> None:
        payload = self.load()
        payload.setdefault("records", []).append(record)
        if success:
            payload.setdefault("success_by_key", {})[idempotency_key] = record
        self.save(payload)


@dataclass
class DependencyPorts:
    build_bundle: Callable[[str], dict[str, Any]]
    call_llm: Callable[[dict[str, Any]], LLMCallResult]
    validate: Callable[[dict[str, Any], dict[str, Any]], ValidationOutcome]
    compose: Callable[[str, dict[str, Any], LLMCallResult, ValidationOutcome], dict[str, Any]]


def _walk_strings(obj: Any) -> Iterable[tuple[str, str]]:
    if isinstance(obj, str):
        yield "", obj
    elif isinstance(obj, dict):
        for key, value in obj.items():
            for sub_path, text in _walk_strings(value):
                yield f"{key}{'.' + sub_path if sub_path else ''}", text
    elif isinstance(obj, list):
        for idx, value in enumerate(obj):
            for sub_path, text in _walk_strings(value):
                yield f"[{idx}]{'.' + sub_path if sub_path else ''}", text


def _body_sentence_count(text: str) -> int:
    normalized = re.sub(r"\d[.,]\d", "0", text or "")
    return len(re.findall(r"(?:다|요|니다|됩니다|합니다|입니다)[.!?]?", normalized))


def _has_duplicate_period_prefix(text: str) -> bool:
    for match in SOURCE_PERIOD_PREFIX_RE.finditer(text or ""):
        period = match.group(2)
        following = text[match.end() : match.end() + 90]
        if re.search(r"\((?:ML|CD)·(?:IQVIA|UBIST)·[^)]*?·" + re.escape(period) + r"\)", following):
            return True
    return False


def _stage_text(stage_data: dict[str, Any]) -> str:
    pieces = [str(stage_data.get("title", "")), str(stage_data.get("body", ""))]
    pieces.extend(str(item) for item in stage_data.get("bullets", []) or [])
    return "\n".join(pieces)


def validate_formatter_contract(
    parsed_output: dict[str, Any],
    brand: str,
    mode: str | ProcessingMode = PROCESSING_MODE_FULL,
) -> FormatterContractResult:
    policy = formatter_policy_for_mode(mode)
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    all_text = "\n".join(text for _, text in _walk_strings(parsed_output))

    for stage in STAGES:
        stage_data = parsed_output.get(stage)
        if not isinstance(stage_data, dict):
            errors.append({"type": "missing_stage", "stage": stage})
            continue
        body = str(stage_data.get("body", ""))
        if _body_sentence_count(body) < policy.min_body_sentences:
            errors.append({"type": "body_too_short", "stage": stage, "sentence_count": _body_sentence_count(body)})
        if len(stage_data.get("bullets", []) or []) < policy.min_bullets:
            errors.append({"type": "too_few_bullets", "stage": stage, "bullet_count": len(stage_data.get("bullets", []) or [])})

    for path, text in _walk_strings(parsed_output):
        if DAMAGED_DATE_RE.search(text):
            errors.append({"type": "damaged_date_or_double_format", "path": path})
        if KRW_QTY_DECIMAL_RE.search(text):
            errors.append({"type": "krw_or_qty_decimal", "path": path})
        for match in THREE_PLUS_DECIMAL_RE.finditer(text):
            errors.append({"type": "three_plus_decimal", "path": path, "value": match.group(0)})
        if _has_duplicate_period_prefix(text):
            errors.append({"type": "duplicate_source_period_prefix", "path": path})
        for match in TAG_RE.finditer(text):
            period = match.group(4)
            if not period or not PERIOD_RE.match(period):
                errors.append({"type": "tag_period_missing_or_invalid", "path": path, "tag": match.group(0)})

    if HEX_NEWS_ID_RE.search(all_text):
        errors.append({"type": "news_id_hex_present"})
    if FORBIDDEN_CERTAINTY_RE.search(all_text):
        errors.append({"type": "prediction_certainty_phrase"})
    if brand in DUAL_SOURCE_BRANDS:
        has_iqvia = bool(re.search(r"\((?:ML|CD)·IQVIA·", all_text))
        has_ubist = bool(re.search(r"\((?:ML|CD)·UBIST·", all_text))
        if not (has_iqvia and has_ubist):
            errors.append({"type": "dual_source_missing", "has_iqvia": has_iqvia, "has_ubist": has_ubist})

    tag_count = len(TAG_RE.findall(all_text))
    if tag_count == 0:
        errors.append({"type": "inline_source_tag_missing"})

    return FormatterContractResult(
        valid=not errors,
        errors=errors,
        warnings=warnings,
        summary={
            "tag_count": tag_count,
            "stage_count": sum(1 for stage in STAGES if isinstance(parsed_output.get(stage), dict)),
            "brand": brand,
        },
    )


class Agent2RegenOrchestrator:
    def __init__(
        self,
        *,
        workflow_revision_id: int,
        formatter_version: str,
        run_store: JsonRunStore,
        ports: DependencyPorts,
        dry_run: bool = True,
        fail_threshold: int = 5,
    ):
        self.workflow_revision_id = workflow_revision_id
        self.formatter_version = formatter_version
        self.run_store = run_store
        self.ports = ports
        self.dry_run = dry_run
        self.fail_threshold = fail_threshold

    def run(self, brands: list[str]) -> dict[str, Any]:
        started_at = datetime.now(timezone.utc).isoformat()
        manifest: dict[str, Any] = {
            "run_id": f"agent2_regen_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            "started_at": started_at,
            "workflow_revision_id": self.workflow_revision_id,
            "formatter_version": self.formatter_version,
            "dry_run": self.dry_run,
            "brands": {},
        }
        swap_candidates: list[str] = []
        failures = 0

        for brand in brands:
            record = self._run_brand(brand)
            manifest["brands"][brand] = record
            if record["status"] == "validated":
                swap_candidates.append(brand)
            elif record["status"] == "failed":
                failures += 1
            if failures > self.fail_threshold:
                manifest["abort_reason"] = f"failures exceeded threshold {self.fail_threshold}"
                break

        manifest["swap_plan"] = self._swap_plan(swap_candidates, failures)
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        return manifest

    def run_routed(self, worklist: list[RoutedAgent2Brand]) -> dict[str, Any]:
        started_at = datetime.now(timezone.utc).isoformat()
        manifest: dict[str, Any] = {
            "run_id": f"agent2_regen_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            "started_at": started_at,
            "workflow_revision_id": self.workflow_revision_id,
            "formatter_version": self.formatter_version,
            "dry_run": self.dry_run,
            "routing_identity": "brand_key",
            "brands": {},
        }
        swap_candidates: list[str] = []
        failures = 0

        for item in worklist:
            match item.route.mode:
                case ProcessingMode.TEMPLATE_ZERO:
                    record = self._run_zero_template(item)
                case ProcessingMode.LLM_FULL | ProcessingMode.LLM_COMPACT | ProcessingMode.LLM_RECAP:
                    record = self._run_brand(item.canonical_brand_name, item.route.mode)
                    record["brand_key"] = item.brand_key
                    record["canonical_brand_name"] = item.canonical_brand_name
                    record["density_route"] = _route_metadata(item)
                case unreachable:
                    assert_never(unreachable)
            manifest["brands"][item.brand_key] = record
            if record["status"] in ("validated", "template_zero", "skipped"):
                swap_candidates.append(item.brand_key)
            elif record["status"] == "failed":
                failures += 1
            if failures > self.fail_threshold:
                manifest["abort_reason"] = f"failures exceeded threshold {self.fail_threshold}"
                break

        manifest["swap_plan"] = self._swap_plan(swap_candidates, failures)
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        return manifest

    def _run_zero_template(self, item: RoutedAgent2Brand) -> dict[str, Any]:
        template = render_zero_template(KpiSnapshot(brand=item.canonical_brand_name))
        return {
            "brand": item.canonical_brand_name,
            "brand_key": item.brand_key,
            "canonical_brand_name": item.canonical_brand_name,
            "status": "template_zero",
            "template": template,
            "density_route": _route_metadata(item),
            "workflow_revision_id": self.workflow_revision_id,
            "formatter_version": self.formatter_version,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }

    def _run_brand(self, brand: str, mode: str | ProcessingMode = PROCESSING_MODE_FULL) -> dict[str, Any]:
        started_at = datetime.now(timezone.utc).isoformat()
        mode_name = normalize_processing_mode(mode)
        try:
            bundle = self.ports.build_bundle(brand)
            bundle = trim_bundle_for_mode(bundle, mode_name)
            bundle_hash = bundle.get("bundle_meta", {}).get("bundle_hash") or compute_bundle_hash(bundle)
            idempotency_key = compute_idempotency_key(
                brand,
                bundle_hash,
                self.workflow_revision_id,
                self.formatter_version,
            )
            previous = self.run_store.get_success(idempotency_key)
            if previous:
                skipped_record: dict[str, Any] = {
                    "brand": brand,
                    "status": "skipped",
                    "reason": "idempotency_key_already_successful",
                    "idempotency_key": idempotency_key,
                    "bundle_hash": bundle_hash,
                    "previous": previous,
                    "started_at": started_at,
                }
                if mode_name != PROCESSING_MODE_FULL:
                    skipped_record["processing_mode"] = mode_name
                return skipped_record

            llm_result = self.ports.call_llm(bundle)
            if not llm_result.success:
                return self._record_failure(brand, idempotency_key, bundle_hash, "llm_failed", llm_result.error)

            formatter = validate_formatter_contract(llm_result.parsed_output, brand, mode_name)
            validation = self.ports.validate(llm_result.parsed_output, bundle)
            if not formatter.valid or not validation.valid:
                return self._record_failure(
                    brand,
                    idempotency_key,
                    bundle_hash,
                    "validation_failed",
                    {
                        "formatter": formatter.to_dict(),
                        "validation": validation.summary,
                    },
                )

            composition = self.ports.compose(brand, bundle, llm_result, validation)
            record = {
                "brand": brand,
                "status": "validated",
                "idempotency_key": idempotency_key,
                "bundle_hash": bundle_hash,
                "workflow_revision_id": self.workflow_revision_id,
                "formatter_version": self.formatter_version,
                "tokens_in": llm_result.tokens_in,
                "tokens_out": llm_result.tokens_out,
                "model_version": llm_result.model_version,
                "retry_count": llm_result.retry_count,
                "validation_summary": validation.summary,
                "formatter_summary": formatter.summary,
                "composition": composition,
                "started_at": started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
            if mode_name != PROCESSING_MODE_FULL:
                record["processing_mode"] = mode_name
            self.run_store.record(idempotency_key, record, success=True)
            return record
        except Exception as exc:
            synthetic_key = compute_idempotency_key(brand, "bundle_hash_unavailable", self.workflow_revision_id, self.formatter_version)
            return self._record_failure(brand, synthetic_key, "bundle_hash_unavailable", "exception", f"{type(exc).__name__}: {exc}")

    def _record_failure(self, brand: str, idempotency_key: str, bundle_hash: str, reason: str, detail: Any) -> dict[str, Any]:
        record = {
            "brand": brand,
            "status": "failed",
            "reason": reason,
            "detail": detail,
            "idempotency_key": idempotency_key,
            "bundle_hash": bundle_hash,
            "workflow_revision_id": self.workflow_revision_id,
            "formatter_version": self.formatter_version,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        self.run_store.record(idempotency_key, record, success=False)
        return record

    def _swap_plan(self, swap_candidates: list[str], failures: int) -> dict[str, Any]:
        if self.dry_run:
            return {
                "mode": "dry-run",
                "live_cache_swap_executed": False,
                "candidate_count": len(swap_candidates),
                "candidate_brands": swap_candidates,
                "excluded_failures": failures,
                "planned_policy": "blue-green rename only when --apply is used in a separately approved run",
            }
        return {
            "mode": "apply-requested",
            "live_cache_swap_executed": False,
            "candidate_count": len(swap_candidates),
            "candidate_brands": swap_candidates,
            "blocked_in_this_build": "live swap execution is intentionally not invoked by dry-run verification",
        }


def _connect_pymysql(host: str, port: int, user: str, password: str, database: str, dict_cursor: bool = True):
    import pymysql

    cursorclass = pymysql.cursors.DictCursor if dict_cursor else None
    kwargs = {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "database": database,
        "charset": "utf8mb4",
        "autocommit": False,
    }
    if cursorclass is not None:
        kwargs["cursorclass"] = cursorclass
    return pymysql.connect(**kwargs)


def _connect_bundle_db(config: BundleConfig):
    return _connect_pymysql(
        config.db.host,
        config.db.port,
        os.environ.get(config.db.user_env, "root"),
        os.environ.get(config.db.password_env, ""),
        config.db.database,
        dict_cursor=True,
    )


def _connect_runner_db(config: RunnerConfig):
    return _connect_pymysql(
        config.composer.db_host,
        config.composer.db_port,
        os.environ.get("DB_USER", config.composer.db_user),
        os.environ.get("DB_ROOT_PASSWORD", ""),
        config.composer.db_name,
        dict_cursor=True,
    )


def check_upstream_freshness(db_conn: Any) -> dict[str, Any]:
    required_tables = (
        "cache_cause",
        "cache_deep_analysis",
        "cache_deep_analysis_ai_analysis",
    )
    optional_tables = (
        "mart_strategic_ml_brand_metric",
        "mart_strategic_cd_brand_metric",
    )
    result: dict[str, Any] = {"valid": True, "tables": {}, "warnings": []}
    cursor = db_conn.cursor()
    for table in (*required_tables, *optional_tables):
        required = table in required_tables
        try:
            cursor.execute(f"SELECT COUNT(*) AS c FROM {table}")
            count = int((cursor.fetchone() or {}).get("c") or 0)
            result["tables"][table] = {"row_count": count, "required": required}
            if count <= 0:
                if required:
                    result["valid"] = False
                result["tables"][table]["error"] = "empty_table"
        except Exception as exc:
            result["tables"][table] = {"required": required, "error": f"{type(exc).__name__}: {exc}"}
            if required:
                result["valid"] = False
            else:
                result["warnings"].append(f"optional_table_unavailable:{table}")
    return result


def _load_brand_list(db_conn: Any, fallback: Iterable[str]) -> list[str]:
    cursor = db_conn.cursor()
    try:
        cursor.execute("SELECT brand FROM cache_deep_analysis_ai_analysis ORDER BY brand")
        brands = [str(row["brand"]) for row in cursor.fetchall() if row.get("brand")]
        if brands:
            return brands
    except Exception:
        pass
    return list(fallback)


def _load_mart_brand_universe(db_conn: Any) -> list[str]:
    cursor = db_conn.cursor()
    cursor.execute(
        """
        SELECT DISTINCT brand_name AS brand
        FROM mart_strategic_ml_brand_metric
        WHERE brand_name IS NOT NULL AND brand_name <> ''
        ORDER BY brand_name
        """
    )
    return [str(row["brand"]) for row in cursor.fetchall() if row.get("brand")]


def _route_metadata(item: RoutedAgent2Brand) -> dict[str, Any]:
    return {
        "brand_key": item.brand_key,
        "canonical_brand_name": item.canonical_brand_name,
        "bucket": item.route.bucket,
        "mode": item.route.mode.value,
        "evidence_count": item.route.evidence_count,
        "included_processors": list(item.route.included_processors),
    }


def _route_plan_manifest(worklist: list[RoutedAgent2Brand], diagnostics: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "route_plan_only",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "routing_identity": "brand_key",
        "diagnostics": diagnostics,
        "brand_count": len(worklist),
        "routes": [_route_metadata(item) for item in worklist],
    }


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json_dumps(payload) + "\n", encoding="utf-8")


def make_real_ports(
    *,
    bundle_config: BundleConfig,
    runner_config: RunnerConfig,
    snapshot_at: datetime,
    catalog_path: str,
    work_dir: Path,
) -> tuple[DependencyPorts, Callable[[], None], dict[str, Any]]:
    bundle_conn = _connect_bundle_db(bundle_config)
    runner_conn = _connect_runner_db(runner_config)
    diagnostics = {"upstream_freshness": check_upstream_freshness(bundle_conn)}

    def close() -> None:
        bundle_conn.close()
        runner_conn.close()

    def build_bundle_port(brand: str) -> dict[str, Any]:
        bundle = build_brand_bundle(brand, snapshot_at, bundle_config, bundle_conn, catalog_path)
        bundle.setdefault("bundle_meta", {})
        bundle["bundle_meta"]["bundle_hash"] = bundle["bundle_meta"].get("bundle_hash") or compute_bundle_hash(bundle)
        bundle_path = work_dir / "bundles" / f"{brand}.json"
        _write_json(bundle_path, bundle)
        return bundle

    def call_llm_port(bundle: dict[str, Any]) -> LLMCallResult:
        result = call_llm(bundle, runner_config)
        return LLMCallResult(
            success=result.success,
            parsed_output=result.parsed_output,
            raw_response=result.raw_response,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            duration_sec=result.duration_sec,
            model_version=result.model_version,
            retry_count=result.retry_count,
            error=result.error,
        )

    def validate_port(parsed_output: dict[str, Any], bundle: dict[str, Any]) -> ValidationOutcome:
        validation: FullValidationResult = run_full_validation(parsed_output, bundle, runner_conn, runner_config)
        return ValidationOutcome(valid=validation.valid, summary=validation.summary, details=validation.to_dict())

    def compose_port(
        brand: str,
        bundle: dict[str, Any],
        llm_result: LLMCallResult,
        validation: ValidationOutcome,
    ) -> dict[str, Any]:
        from phase_zeta_runner.llm_runner import LLMResult
        from phase_zeta_runner.run_pipeline import FullValidationResult

        # compose_and_persist expects the project dataclasses. Rebuild the small
        # LLM object directly; for validation, keep the details in raw outputs
        # and use a minimal adapter carrying the required attributes.
        real_llm = LLMResult(
            success=llm_result.success,
            parsed_output=llm_result.parsed_output,
            raw_response=llm_result.raw_response,
            tokens_in=llm_result.tokens_in,
            tokens_out=llm_result.tokens_out,
            cost_usd=0.0,
            duration_sec=llm_result.duration_sec,
            model_version=llm_result.model_version,
            retry_count=llm_result.retry_count,
            error=llm_result.error,
        )
        # Re-run full validation here to preserve stage_results dataclass shape
        # for output_composer. This is read-only before staging insert.
        full_validation: FullValidationResult = run_full_validation(real_llm.parsed_output, bundle, runner_conn, runner_config)
        composition = compose_and_persist(brand, snapshot_at, bundle, real_llm, full_validation, runner_config, runner_conn)
        parsed_path = work_dir / "parsed_outputs" / f"{brand}_parsed.json"
        _write_json(parsed_path, real_llm.parsed_output)
        validation_path = work_dir / "validation" / f"{brand}_validation.json"
        _write_json(validation_path, full_validation.to_dict())
        raw_path = work_dir / "raw_responses" / f"{brand}_raw.json"
        _write_json(raw_path, {"raw_response": real_llm.raw_response})
        return composition.to_dict()

    return DependencyPorts(build_bundle=build_bundle_port, call_llm=call_llm_port, validate=validate_port, compose=compose_port), close, diagnostics


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase ζ Agent 2 regeneration orchestrator.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True, help="Generate/stage only; never swap live cache (default).")
    mode.add_argument("--apply", action="store_true", default=False, help="Reserved for separately approved live swap run.")
    parser.add_argument("--brands", nargs="*", help="Explicit brands to process. Overrides --brand-source.")
    parser.add_argument(
        "--brand-source",
        choices=("ai-analysis-cache", "mart-universe", "general-density"),
        default="ai-analysis-cache",
        help=(
            "Worklist source when --brands is omitted. Defaults to the existing "
            "cache_deep_analysis_ai_analysis sink for safe JW25-sized runs; use "
            "mart-universe explicitly for all mart_strategic_ml_brand_metric brands; "
            "use general-density for brand_key routing over mart_general_brand_metric."
        ),
    )
    parser.add_argument(
        "--route-plan-only",
        action="store_true",
        help="For --brand-source general-density, build the routed worklist only and do not call wf217.",
    )
    parser.add_argument("--runner-config", default=str(PHASE_ZETA_ROOT / "configs" / "genos_runner_v1.yaml"))
    parser.add_argument("--bundle-config", default=str(PHASE_ZETA_ROOT / "configs" / "phase_zeta_v1_1.yaml"))
    parser.add_argument("--catalog", default="docs/crawl/_catalog.json")
    parser.add_argument("--work-dir", default="outputs/phase_zeta_agent2_regen_orchestrator/manual_run")
    parser.add_argument("--snapshot-at", default=None, help="ISO datetime. Defaults to now.")
    parser.add_argument("--workflow-revision-id", type=int, default=DEFAULT_WORKFLOW_REVISION_ID)
    parser.add_argument("--formatter-version", default=DEFAULT_FORMATTER_VERSION)
    parser.add_argument("--fail-threshold", type=int, default=5)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    dry_run = not args.apply
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    bundle_config = BundleConfig.from_yaml(args.bundle_config)
    runner_config = RunnerConfig.from_yaml(args.runner_config)
    snapshot_at = datetime.fromisoformat(args.snapshot_at) if args.snapshot_at else datetime.now()

    ports, close, diagnostics = make_real_ports(
        bundle_config=bundle_config,
        runner_config=runner_config,
        snapshot_at=snapshot_at,
        catalog_path=args.catalog,
        work_dir=work_dir,
    )
    try:
        if not diagnostics["upstream_freshness"]["valid"]:
            _write_json(work_dir / "run_manifest.json", {"status": "aborted", "diagnostics": diagnostics})
            print(_json_dumps({"status": "aborted", "reason": "upstream_freshness_failed", "diagnostics": diagnostics}))
            return 2
        if args.brands:
            brands = args.brands
            routed_worklist = None
        else:
            brand_conn = _connect_bundle_db(bundle_config)
            try:
                if args.brand_source == "mart-universe":
                    brands = _load_mart_brand_universe(brand_conn)
                    routed_worklist = None
                elif args.brand_source == "general-density":
                    density_worklist = load_density_worklist(brand_conn)
                    routed_worklist = list(density_worklist.routed)
                    brands = [item.canonical_brand_name for item in routed_worklist]
                    diagnostics["density_worklist"] = {
                        "unmatched_known": list(density_worklist.evidence.unmatched_known),
                        "unmatched_unknown": list(density_worklist.evidence.unmatched_unknown),
                    }
                else:
                    brands = _load_brand_list(brand_conn, bundle_config.pilot_brands)
                    routed_worklist = None
            finally:
                brand_conn.close()
        diagnostics["brand_worklist"] = {"source": "explicit" if args.brands else args.brand_source, "count": len(brands)}
        if routed_worklist is not None and args.route_plan_only:
            manifest = _route_plan_manifest(routed_worklist, diagnostics)
            _write_json(work_dir / "run_manifest.json", manifest)
            print(_json_dumps(manifest))
            return 0
        orchestrator = Agent2RegenOrchestrator(
            workflow_revision_id=args.workflow_revision_id,
            formatter_version=args.formatter_version,
            run_store=JsonRunStore(work_dir / "idempotency_manifest.json"),
            ports=ports,
            dry_run=dry_run,
            fail_threshold=args.fail_threshold,
        )
        manifest = orchestrator.run_routed(routed_worklist) if routed_worklist is not None else orchestrator.run(list(brands))
        manifest["diagnostics"] = diagnostics
        _write_json(work_dir / "run_manifest.json", manifest)
        print(_json_dumps(manifest))
        if manifest.get("abort_reason"):
            return 3
        return 0
    finally:
        close()


if __name__ == "__main__":
    raise SystemExit(main())
