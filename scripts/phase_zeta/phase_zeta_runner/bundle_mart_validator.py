from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from .config import ValidatorConfig


@dataclass
class BundleMartCheck:
    view_id: str
    period: str
    field: str
    bundle_value: float | int | None
    mart_value: float | int | None
    match: bool
    error: str | None


def _source_candidates(source: str) -> tuple[str, ...]:
    normalized = source.upper()
    if normalized == "UBIST":
        return ("UBIST", "ubist")
    if normalized == "IQVIA":
        return ("IQVIA", "iqvia_nsa", "iqvia")
    return (source, source.lower(), source.upper())


def _market_id(view: dict[str, Any]) -> tuple[str, str, str]:
    market_meta = view.get("market_meta", {}) or {}
    market_id = str(market_meta.get("market_id_internal") or "")
    if not market_id:
        raw_view_id = str(view.get("view_id", ""))
        first = raw_view_id.split(".")[0]
        market_id = first if first.startswith(("ml_", "cd_")) else ""
    market_type = "cd" if market_id.startswith("cd_") else "ml"
    id_col = "cd_market_id" if market_type == "cd" else "ml_id"
    return market_type, market_id, id_col


def _row_to_dict(cursor: Any, row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    description = getattr(cursor, "description", None)
    if description:
        return {description[idx][0]: value for idx, value in enumerate(row)}
    return None


def _load_history(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def _numeric(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _execute_row(cursor: Any, table: str, id_col: str, market_id: str, source: str, measure: str, brand: str) -> dict[str, Any] | None:
    sql = f"""
    SELECT brand_name, metric_history
    FROM {table}
    WHERE {id_col} = %s
      AND source IN %s
      AND measure = %s
      AND brand_name = %s
    LIMIT 1
    """
    cursor.execute(sql, (market_id, _source_candidates(source), measure, brand))
    return _row_to_dict(cursor, cursor.fetchone())


def _append_compare(
    checks: list[BundleMartCheck],
    view_id: str,
    period: str,
    field: str,
    bundle_value: Any,
    mart_value: Any,
    tolerance: float,
) -> None:
    bundle_num = _numeric(bundle_value)
    mart_num = _numeric(mart_value)
    if bundle_num is None or mart_num is None:
        return
    match = abs(bundle_num - mart_num) <= tolerance
    checks.append(
        BundleMartCheck(
            view_id=view_id,
            period=period,
            field=field,
            bundle_value=int(bundle_num) if field == "rank" else bundle_num,
            mart_value=int(mart_num) if field == "rank" else mart_num,
            match=match,
            error=None if match else f"mismatch: bundle={bundle_num} mart={mart_num}",
        )
    )


def _validate_history(
    checks: list[BundleMartCheck],
    view_id: str,
    bundle_history: dict[str, Any],
    mart_history: dict[str, Any],
    config: ValidatorConfig,
) -> None:
    for period, bundle_period in (bundle_history or {}).items():
        if not isinstance(bundle_period, dict):
            continue
        mart_period = mart_history.get(period, {}) or {}
        if not isinstance(mart_period, dict):
            continue
        _append_compare(checks, view_id, period, "raw_value", bundle_period.get("raw_value"), mart_period.get("raw_value"), config.tolerance_default)
        _append_compare(checks, view_id, period, "ms", bundle_period.get("ms_pct", bundle_period.get("ms")), mart_period.get("ms"), config.tolerance_percent)
        _append_compare(checks, view_id, period, "rank", bundle_period.get("rank"), mart_period.get("rank"), 0.0)


def validate_bundle_against_mart(bundle: dict[str, Any], db_conn: Any, config: ValidatorConfig) -> dict[str, Any]:
    cursor = db_conn.cursor()
    brand_name = (bundle.get("brand_context", {}) or {}).get("name") or (bundle.get("bundle_meta", {}) or {}).get("brand")
    checks: list[BundleMartCheck] = []

    for view in bundle.get("market_views", []) or []:
        view_id = str(view.get("view_id", "unknown_view"))
        market_type, market_id, id_col = _market_id(view)
        table = f"mart_strategic_{market_type}_brand_metric"
        source = str(view.get("source", ""))
        measure = str(view.get("measure", ""))

        mart_row = _execute_row(cursor, table, id_col, market_id, source, measure, str(brand_name))
        if not mart_row:
            checks.append(BundleMartCheck(view_id, "N/A", "row_existence", None, None, False, f"mart row not found: {table} {market_id} {source} {measure} {brand_name}"))
            continue
        _validate_history(
            checks,
            view_id,
            ((view.get("target_brand_metric", {}) or {}).get("history", {}) or {}),
            _load_history(mart_row.get("metric_history")),
            config,
        )

        for comp in view.get("competitors_top5", []) or []:
            comp_name = comp.get("brand_name")
            if not comp_name:
                continue
            comp_row = _execute_row(cursor, table, id_col, market_id, source, measure, str(comp_name))
            if not comp_row:
                checks.append(BundleMartCheck(f"{view_id}/competitors/{comp_name}", "N/A", "row_existence", None, None, False, f"mart row not found: {table} {market_id} {source} {measure} {comp_name}"))
                continue
            _validate_history(
                checks,
                f"{view_id}/competitors/{comp_name}",
                (comp.get("history", {}) or {}),
                _load_history(comp_row.get("metric_history")),
                config,
            )

    matched = sum(1 for check in checks if check.match)
    mismatched = [check for check in checks if not check.match and not (check.error or "").startswith("mart row not found")]
    missing = [check for check in checks if (check.error or "").startswith("mart row not found")]
    return {
        "valid": not mismatched and not missing,
        "total_checks": len(checks),
        "matched": matched,
        "mismatched": [asdict(check) for check in mismatched],
        "missing_in_mart": [asdict(check) for check in missing],
    }
