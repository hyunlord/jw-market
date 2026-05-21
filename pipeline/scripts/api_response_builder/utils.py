from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


BRAND_MARTS = {
    "general": {
        "brand_mart": "mart_general_brand_metric",
        "market_mart": "mart_general_market_metric",
        "market_id_col": "atc4_code",
        "market_name_col": "atc4_desc",
    },
    "strategic_ml": {
        "brand_mart": "mart_strategic_ml_brand_metric",
        "market_mart": "mart_strategic_ml_market_metric",
        "market_id_col": "ml_id",
        "market_name_col": "ml_name",
    },
    "strategic_cd": {
        "brand_mart": "mart_strategic_cd_brand_metric",
        "market_mart": "mart_strategic_cd_market_metric",
        "market_id_col": "cd_market_id",
        "market_name_col": "cd_market_name",
    },
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return value


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    return value


def json_dumps(value: Any) -> str:
    return json.dumps(to_jsonable(value), ensure_ascii=False, separators=(",", ":"))


def clean_json_scalar(value: Any) -> Any:
    parsed = parse_json(value)
    if isinstance(parsed, str):
        return parsed
    return parsed


def period_sort_key(period: str) -> tuple[int, int]:
    text = str(period)
    if "-Q" in text:
        year, quarter = text.split("-Q", 1)
        return int(year), int(quarter) * 3
    if "-" in text:
        year, month = text.split("-", 1)
        return int(year), int(month)
    return int(text[:4]), int(text[4:6] or 0)


def latest_period(history: dict[str, Any] | None) -> str | None:
    if not history:
        return None
    return sorted(history.keys(), key=period_sort_key)[-1]


def view_for_mart(mart_name: str) -> str:
    if "strategic_ml" in mart_name:
        return "strategic_ml"
    if "strategic_cd" in mart_name:
        return "strategic_cd"
    return "general"


def market_id_for_brand_row(view_type: str, brand_row: dict[str, Any]) -> str | None:
    return brand_row.get(BRAND_MARTS[view_type]["market_id_col"])


def market_key(view_type: str, market_id: str | None, source: str | None, measure: str | None) -> tuple[str, str | None, str | None, str | None]:
    return (view_type, market_id, source, measure)


def normalise_market_row(view_type: str, row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {}
    cfg = BRAND_MARTS[view_type]
    market_id = row.get(cfg["market_id_col"])
    market_name = row.get(cfg["market_name_col"]) or row.get("atc4_desc")
    hhi = row.get("hhi_series_5y", row.get("hhi_series"))
    brand_ranking = row.get("brand_ranking_stacked", row.get("brand_ranking"))
    return {
        "market_id": market_id,
        "market_name": market_name,
        "view": view_type,
        "source": row.get("source"),
        "measure": row.get("measure"),
        "unit_label": row.get("unit_label"),
        "market_size_series": parse_json(row.get("market_size_series")),
        "hhi_series_5y": parse_json(hhi),
        "brand_ranking_stacked": parse_json(brand_ranking),
        "company_ranking_stacked": parse_json(row.get("company_ranking_stacked")),
        "company_concentration_trend": parse_json(row.get("company_concentration_trend")),
        "ei_ms_matrix": parse_json(row.get("ei_ms_matrix")),
        "growth_contribution_ms_matrix": parse_json(row.get("growth_contribution_ms_matrix")),
        "growth_contribution": parse_json(row.get("growth_contribution")),
        "analysis_levels": parse_json(row.get("analysis_levels")),
        "level_top5_trend": parse_json(row.get("level_top5_trend")),
        "target_customer_competition": parse_json(row.get("target_customer_competition")),
        "payload": parse_json(row.get("payload")),
    }


def market_chart_fields(market_payload: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "market_size_series",
        "hhi_series_5y",
        "brand_ranking_stacked",
        "company_ranking_stacked",
        "company_concentration_trend",
        "ei_ms_matrix",
        "growth_contribution_ms_matrix",
        "growth_contribution",
        "analysis_levels",
        "level_top5_trend",
        "target_customer_competition",
    ]
    return {key: market_payload.get(key) for key in keys}
