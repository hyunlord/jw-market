"""Mart-direct strategic deep-analysis section assembly."""

from __future__ import annotations

from datetime import datetime
import json
import logging
from typing import Any

import pymysql

from pipeline.scripts.api import db
from pipeline.scripts.api.deep_analysis_serving import ForecastBlock, load_forecast_block_by_key
from pipeline.scripts.etl import build_cache_deep_analysis as builder
from pipeline.scripts.etl.phase29_events import build_events_for_cache
from pipeline.scripts.utils.brand_name_normalize import compact_brand_name


logger = logging.getLogger(__name__)

_STRATEGIC_BRAND_RUNTIME_COLUMNS = (
    "brand_key, brand_name, ml_id, source, measure, is_jw, is_target, computed_at"
)
_STRATEGIC_MARKET_RUNTIME_COLUMNS = "name, data_source, atc_codes_json"


def _compact_sql(column: str) -> str:
    return f"REPLACE(REPLACE(REPLACE(REPLACE({column}, ' ', ''), CHAR(9), ''), CHAR(10), ''), CHAR(13), '')"


def _brand_rows(brand: str) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        f"""
        SELECT {_STRATEGIC_BRAND_RUNTIME_COLUMNS}
        FROM mart_strategic_ml_brand_metric
        WHERE brand_name = %s
        ORDER BY ml_id, source, measure
        """,
        [brand],
    )
    if rows:
        return rows
    compact = compact_brand_name(brand)
    if not compact:
        return []
    candidates = db.fetch_all(
        f"""
        SELECT {_STRATEGIC_BRAND_RUNTIME_COLUMNS}
        FROM mart_strategic_ml_brand_metric
        WHERE {_compact_sql('brand_name')} = %s
        ORDER BY brand_name, ml_id, source, measure
        """,
        [compact],
    )
    names = {str(row.get("brand_name") or "") for row in candidates}
    return candidates if len(names) == 1 else []


def _market_catalog(ml_id: str) -> dict[str, Any]:
    return db.fetch_one(
        f"SELECT {_STRATEGIC_MARKET_RUNTIME_COLUMNS} FROM catalog_ml_market WHERE ml_id = %s LIMIT 1",
        [ml_id],
    ) or {}


def build_strategic_row(brand: str) -> dict[str, Any] | None:
    brand_rows = _brand_rows(brand)
    if not brand_rows:
        return None
    base = builder.choose_base(brand_rows)
    matched_brand = str(base.get("brand_name") or brand)
    ml_id = str(base.get("ml_id") or "")
    selected_brand_rows = [row for row in brand_rows if str(row.get("ml_id") or "") == ml_id]
    market = _market_catalog(ml_id)
    market_atc_codes = builder.atc_codes_from_market_catalog(market)
    events_payload = _event_payload(matched_brand)
    events = builder._events_spec_list(builder._dedup_cut_a_events(events_payload))
    brand_key = str(base.get("brand_key") or matched_brand)
    blocks: list[ForecastBlock] = []
    missing_combos: list[str] = []
    for source in sorted({str(row.get("source") or "") for row in selected_brand_rows}):
        if not source:
            continue
        block = load_forecast_block_by_key(
            brand_key=brand_key,
            source=source,
            market_id=ml_id,
        )
        if block is not None:
            blocks.append(block)
            continue
        missing_combos.extend(
            f"{builder.api_source(source)}.{row.get('measure')}"
            for row in selected_brand_rows
            if str(row.get("source") or "") == source and row.get("measure")
        )
    forecast, simulation = _merge_block_payloads(blocks, tuple(sorted(set(missing_combos))))
    available_combos = sorted(_section_by_combo(forecast))

    sources = builder.source_list(market.get("data_source"))
    brand_metadata = builder.BRAND_METADATA_BY_NAME.get(matched_brand)
    payload = {
        "brand": matched_brand,
        "brand_name": matched_brand,
        "market_id": builder.ml_to_strategy(ml_id),
        "market_name": market.get("name"),
        "available_combos": available_combos,
        "data": {
            "forecast": forecast,
            "simulation": simulation,
            "events": events,
        },
        "market_meta": {
            "market_name": market.get("name"),
            "atc4_code": market_atc_codes[0] if market_atc_codes else None,
            "atc4_name": brand_metadata.atc_desc if brand_metadata else None,
            "sources": sources,
            "default_source": sources[0] if sources else None,
            "available_combos": available_combos,
            "source_count": len({builder.api_source(row["source"]) for row in selected_brand_rows}),
            "measure_count": len({row["measure"] for row in selected_brand_rows}),
            "market_count": len({row["ml_id"] for row in brand_rows}),
            "is_jw": bool(base.get("is_jw")),
            "is_target": bool(base.get("is_target")),
        },
    }
    computed_values = [
        row.get("computed_at")
        for row in selected_brand_rows
        if isinstance(row.get("computed_at"), datetime)
    ]
    return {
        "brand": matched_brand,
        "brand_key": brand_key,
        "market_id": builder.ml_to_strategy(ml_id),
        "response_json": json.dumps(payload, ensure_ascii=False),
        "brand_factors": json.dumps({"atc": market_atc_codes, "ubist": {}, "iqvia": {}}, ensure_ascii=False),
        "updated_at": max(computed_values, default=None),
        "_events": events,
    }


def _merge_block_payloads(
    blocks: list[ForecastBlock],
    missing_combos: tuple[str, ...] = (),
) -> tuple[object, object]:
    forecast = _merge_block_sections(blocks, "forecast")
    simulation = _merge_block_sections(blocks, "simulation")
    if not blocks:
        return forecast, simulation

    if missing_combos:
        merged_forecast = dict(forecast) if isinstance(forecast, dict) else {}
        merged_forecast_by_combo = dict(_section_by_combo(forecast))
        for combo in missing_combos:
            merged_forecast_by_combo[combo] = {"available": False, "reason": "not_generated"}
        merged_forecast["by_combo"] = merged_forecast_by_combo
        forecast = merged_forecast

    if len(blocks) == 1 and not missing_combos:
        return forecast, simulation

    available_simulations = [
        block.simulation for block in blocks if _section_by_combo(block.simulation)
    ]
    merged_simulation = dict(available_simulations[0]) if available_simulations else {}
    merged_simulation_by_combo = {
        combo: value
        for section in available_simulations
        for combo, value in _section_by_combo(section).items()
    }
    for block in blocks:
        unavailable = block.simulation
        if not isinstance(unavailable, dict) or unavailable.get("available") is not False:
            continue
        for combo in _section_by_combo(block.forecast):
            merged_simulation_by_combo.setdefault(combo, dict(unavailable))
    for combo in missing_combos:
        merged_simulation_by_combo[combo] = {"available": False, "reason": "not_generated"}
    merged_simulation["by_combo"] = merged_simulation_by_combo
    return forecast, merged_simulation


def _merge_block_sections(blocks: list[ForecastBlock], name: str) -> object:
    if not blocks:
        return {"available": False, "reason": "not_generated"}
    sections = [getattr(block, name) for block in blocks]
    available = [section for section in sections if _section_by_combo(section)]
    if not available:
        return sections[0]
    merged = dict(available[0])
    merged["by_combo"] = {
        combo: value
        for section in available
        for combo, value in _section_by_combo(section).items()
    }
    return merged


def _section_by_combo(section: object) -> dict[str, Any]:
    if not isinstance(section, dict):
        return {}
    by_combo = section.get("by_combo")
    return by_combo if isinstance(by_combo, dict) else {}


def _event_payload(brand: str) -> dict[str, Any]:
    try:
        with db.borrow_read_connection() as conn:
            return build_events_for_cache(conn, brand)
    except pymysql.MySQLError:
        logger.warning("deep_analysis_events_unavailable brand=%s", brand, exc_info=True)
        return {"cut_a": [], "cut_b": []}


def load_events(brand: str) -> list[dict[str, Any]]:
    return builder._events_spec_list(builder._dedup_cut_a_events(_event_payload(brand)))
