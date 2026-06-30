from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from .config import RunnerConfig
from .llm_runner import GeminiResult, STAGES
from .metric_validator import ValidationResult


@dataclass
class CompositionResult:
    run_id: int | None
    cache_updated: bool
    status: str
    trace_files: list[str]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _table_has_column(db_conn: Any, table: str, column: str) -> bool:
    cursor = db_conn.cursor()
    cursor.execute(f"SHOW COLUMNS FROM {table} LIKE %s", (column,))
    return cursor.fetchone() is not None


def _status(gemini_result: GeminiResult, validation_result: ValidationResult) -> str:
    if not gemini_result.success:
        return "failed"
    if not validation_result.valid:
        return "partial"
    return "ok"


def _insert_run(
    db_conn: Any,
    brand: str,
    snapshot_at: datetime,
    bundle: dict[str, Any],
    gemini_result: GeminiResult,
    validation_result: ValidationResult,
    config: RunnerConfig,
) -> int:
    has_variant_column = _table_has_column(db_conn, "zeta_analysis_runs", "analysis_variant")
    if not has_variant_column and config.analysis_variant != "legacy":
        raise RuntimeError("zeta_analysis_runs.analysis_variant is required for short/long Agent2 runs")

    if not has_variant_column:
        sql = """
        INSERT INTO zeta_analysis_runs (
            brand, snapshot_at, config_version, builder_version, bundle_hash,
            model_version, status, total_tokens_in, total_tokens_out, cost_usd,
            duration_sec, input_bundle, error_log
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        """
        values = (
            brand,
            snapshot_at,
            config.config_version,
            config.builder_version,
            bundle.get("bundle_meta", {}).get("bundle_hash", ""),
            gemini_result.model_version,
            _status(gemini_result, validation_result),
            gemini_result.tokens_in,
            gemini_result.tokens_out,
            gemini_result.cost_usd,
            gemini_result.duration_sec,
            _json_dumps(bundle),
            gemini_result.error or "",
        )
        cursor = db_conn.cursor()
        cursor.execute(sql, values)
        return int(cursor.lastrowid)

    sql = """
    INSERT INTO zeta_analysis_runs (
        brand, snapshot_at, analysis_variant, config_version, builder_version, bundle_hash,
        model_version, status, total_tokens_in, total_tokens_out, cost_usd,
        duration_sec, input_bundle, error_log
    ) VALUES (
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
    )
    """
    cursor = db_conn.cursor()
    cursor.execute(
        sql,
        (
            brand,
            snapshot_at,
            config.analysis_variant,
            config.config_version,
            config.builder_version,
            bundle.get("bundle_meta", {}).get("bundle_hash", ""),
            gemini_result.model_version,
            _status(gemini_result, validation_result),
            gemini_result.tokens_in,
            gemini_result.tokens_out,
            gemini_result.cost_usd,
            gemini_result.duration_sec,
            _json_dumps(bundle),
            gemini_result.error or "",
        ),
    )
    return int(cursor.lastrowid)


def _insert_outputs(
    db_conn: Any,
    run_id: int,
    gemini_result: GeminiResult,
    validation_result: ValidationResult,
) -> None:
    if not gemini_result.success:
        return

    sql = """
    INSERT INTO zeta_analysis_outputs (
        run_id, stage, title, body, bullets,
        raw_response, validated, validation_log, tokens_in, tokens_out
    ) VALUES (
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
    )
    """
    cursor = db_conn.cursor()
    full_validation = validation_result.to_dict() if hasattr(validation_result, "to_dict") else {}
    for stage in STAGES:
        stage_data = gemini_result.parsed_output.get(stage, {}) or {}
        stage_validation = validation_result.stage_results.get(stage)
        validation_log = {
            "stage_validation": stage_validation.to_dict() if stage_validation else {},
            "full_validation_summary": full_validation.get("summary", {}),
            "full_validation_layers": full_validation.get("layers", {}),
        }
        cursor.execute(
            sql,
            (
                run_id,
                stage,
                str(stage_data.get("title", "")),
                str(stage_data.get("body", "")),
                _json_dumps(stage_data.get("bullets", [])),
                _json_dumps(stage_data),
                1 if stage_validation and stage_validation.valid else 0,
                _json_dumps(validation_log),
                None,
                None,
            ),
        )


def _update_cache_deep_analysis(db_conn: Any, brand: str, gemini_result: GeminiResult) -> int:
    ai_analysis_obj = {
        "ai_analysis": {
            "generated_at": datetime.now().isoformat(),
            "model_version": gemini_result.model_version,
            **gemini_result.parsed_output,
        }
    }
    sql = """
    UPDATE cache_deep_analysis
    SET response_json = JSON_MERGE_PATCH(
            response_json,
            JSON_OBJECT('data', %s)
        ),
        updated_at = NOW()
    WHERE brand = %s
    """
    cursor = db_conn.cursor()
    cursor.execute(sql, (_json_dumps(ai_analysis_obj), brand))
    return int(cursor.rowcount)


def compose_and_persist(
    brand: str,
    snapshot_at: datetime,
    bundle: dict[str, Any],
    gemini_result: GeminiResult,
    validation_result: ValidationResult,
    config: RunnerConfig,
    db_conn: Any,
) -> CompositionResult:
    status = _status(gemini_result, validation_result)
    try:
        run_id = _insert_run(db_conn, brand, snapshot_at, bundle, gemini_result, validation_result, config)
        _insert_outputs(db_conn, run_id, gemini_result, validation_result)
        cache_updated = False
        if status == "ok" and config.composer.update_cache_deep_analysis:
            _update_cache_deep_analysis(db_conn, brand, gemini_result)
            cache_updated = True
        db_conn.commit()
        return CompositionResult(
            run_id=run_id,
            cache_updated=cache_updated,
            status=status,
            trace_files=[],
            error=None,
        )
    except Exception as exc:
        db_conn.rollback()
        return CompositionResult(
            run_id=None,
            cache_updated=False,
            status="failed",
            trace_files=[],
            error=f"{type(exc).__name__}: {exc}",
        )
