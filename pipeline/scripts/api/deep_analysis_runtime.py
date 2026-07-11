"""Mart-direct strategic deep-analysis section assembly."""

from __future__ import annotations

from datetime import datetime
import json
import logging
from typing import Any

import pymysql

from pipeline.scripts.api import db
from pipeline.scripts.api.deep_analysis_section_cache import deep_section_cache
from pipeline.scripts.api.dynamic_market.response_cache import DynamicResponseCacheUnavailable
from pipeline.scripts.etl import build_cache_deep_analysis as builder
from pipeline.scripts.etl.phase29_events import build_events_for_cache
from pipeline.scripts.utils.brand_name_normalize import compact_brand_name


logger = logging.getLogger(__name__)


def _compact_sql(column: str) -> str:
    return f"REPLACE(REPLACE(REPLACE(REPLACE({column}, ' ', ''), CHAR(9), ''), CHAR(10), ''), CHAR(13), '')"


def _brand_rows(brand: str) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        """
        SELECT *
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
        SELECT *
        FROM mart_strategic_ml_brand_metric
        WHERE {_compact_sql('brand_name')} = %s
        ORDER BY brand_name, ml_id, source, measure
        """,
        [compact],
    )
    names = {str(row.get("brand_name") or "") for row in candidates}
    return candidates if len(names) == 1 else []


def _market_rows(ml_id: str) -> list[dict[str, Any]]:
    return db.fetch_all(
        """
        SELECT *
        FROM mart_strategic_ml_brand_metric
        WHERE ml_id = %s
        ORDER BY source, measure, brand_name
        """,
        [ml_id],
    )


def _market_catalog(ml_id: str) -> dict[str, Any]:
    return db.fetch_one("SELECT * FROM catalog_ml_market WHERE ml_id = %s LIMIT 1", [ml_id]) or {}


def build_strategic_row(brand: str) -> dict[str, Any] | None:
    brand_rows = _brand_rows(brand)
    if not brand_rows:
        return None
    base = builder.choose_base(brand_rows)
    matched_brand = str(base.get("brand_name") or brand)
    ml_id = str(base.get("ml_id") or "")
    selected_brand_rows = [row for row in brand_rows if str(row.get("ml_id") or "") == ml_id]
    market_rows = _market_rows(ml_id)
    market = _market_catalog(ml_id)
    market_atc_codes = builder.atc_codes_from_market_catalog(market)
    available_combos = builder.available_combos_for_market(market)
    phase30_enabled = matched_brand in builder.CANONICAL_25
    events_payload = _event_payload(matched_brand)
    events = builder._events_spec_list(builder._dedup_cut_a_events(events_payload))

    rows_by_combo = {
        f"{builder.api_source(row['source'])}.{row['measure']}": row
        for row in selected_brand_rows
    }
    market_rows_by_combo: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in market_rows:
        key = (str(row.get("source") or ""), str(row.get("measure") or ""))
        market_rows_by_combo.setdefault(key, []).append(row)

    def build_expensive_sections() -> dict[str, Any]:
        by_combo: dict[str, dict[str, Any]] = {}
        simulation_by_combo: dict[str, dict[str, Any]] = {}
        for source, measure in builder.ALL_COMBOS:
            combo = f"{source}.{measure}"
            if combo not in available_combos:
                continue
            row = rows_by_combo.get(combo)
            if row is None:
                by_combo[combo] = builder.empty_combo_payload(source, measure, matched_brand, base)
                continue
            internal_source = builder.SOURCE_TO_INTERNAL[source]
            combo_market_rows = market_rows_by_combo.get((internal_source, measure), [])
            combo_payload = builder.combo_payload(
                row,
                market_rows=combo_market_rows,
                target_brand=matched_brand,
                combo_source=source,
                phase30=phase30_enabled,
            )
            if phase30_enabled and builder.build_phase30_simulation_combo is not None:
                market_forecast = combo_payload.pop("_phase30_market_forecast", None)
                if market_forecast is not None:
                    simulation_by_combo[combo] = builder.build_phase30_simulation_combo(
                        combo=combo,
                        source=source,
                        measure=measure,
                        unit_label=combo_payload.get("unit_label"),
                        forecast_combo=combo_payload,
                        market_forecast=market_forecast,
                        cut_b_events=events_payload.get("cut_b") or [],
                    )
            by_combo[combo] = combo_payload
        return {"forecast_by_combo": by_combo, "simulation_by_combo": simulation_by_combo}

    cache_request = {
        "contract": "deep-expensive-sections-v1",
        "view": "strategic",
        "brand": matched_brand,
        "market_id": ml_id,
    }
    try:
        expensive = deep_section_cache.get_or_build(cache_request, build_expensive_sections)
    except DynamicResponseCacheUnavailable:
        expensive = build_expensive_sections()
    by_combo = expensive.get("forecast_by_combo") if isinstance(expensive.get("forecast_by_combo"), dict) else {}
    simulation_by_combo = expensive.get("simulation_by_combo") if isinstance(expensive.get("simulation_by_combo"), dict) else {}

    sources = builder.source_list(market.get("data_source"))
    brand_metadata = builder.BRAND_METADATA_BY_NAME.get(matched_brand)
    payload = {
        "brand": matched_brand,
        "brand_name": matched_brand,
        "market_id": builder.ml_to_strategy(ml_id),
        "market_name": market.get("name"),
        "available_combos": available_combos,
        "data": {
            "forecast": {
                "method": builder.FORECAST_METHOD,
                "disclaimer": builder.FORECAST_DISCLOSURE,
                "is_statistical_model": True,
                "backtest_available": True,
                "event_regressor_enabled": False,
                "phase29_poc": None,
                "by_combo": by_combo,
            },
            "simulation": {"by_combo": simulation_by_combo},
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
    computed_values = [row.get("computed_at") for row in selected_brand_rows if isinstance(row.get("computed_at"), datetime)]
    return {
        "brand": matched_brand,
        "brand_key": matched_brand,
        "market_id": builder.ml_to_strategy(ml_id),
        "response_json": json.dumps(payload, ensure_ascii=False),
        "brand_factors": json.dumps({"atc": market_atc_codes, "ubist": {}, "iqvia": {}}, ensure_ascii=False),
        "updated_at": max(computed_values, default=None),
        "_events": events,
    }


def _event_payload(brand: str) -> dict[str, Any]:
    try:
        with db.connect() as conn:
            return build_events_for_cache(conn, brand)
    except pymysql.MySQLError:
        logger.warning("deep_analysis_events_unavailable brand=%s", brand, exc_info=True)
        return {"cut_a": [], "cut_b": []}


def load_events(brand: str) -> list[dict[str, Any]]:
    return builder._events_spec_list(builder._dedup_cut_a_events(_event_payload(brand)))
