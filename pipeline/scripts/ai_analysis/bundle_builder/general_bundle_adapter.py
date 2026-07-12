from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any, Callable

from .event_bundle_builder import build_event_bundle
from .hash_util import compute_bundle_hash, deterministic_json_dumps
from .kpi_provider import GeneralViewKpiProvider


def _rank_latest_period(kpi: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], int | None]:
    target_history = deepcopy(kpi.get("target_history") or {})
    competitors = deepcopy(kpi.get("competitors_top5") or [])
    if not target_history:
        return target_history, competitors, kpi.get("target_rank")

    latest_period = max(target_history)
    candidates: list[tuple[str, float, dict[str, Any] | None]] = []
    target_point = target_history.get(latest_period) or {}
    target_value = target_point.get("raw_value")
    if target_value is not None:
        candidates.append((str(kpi.get("brand_key") or ""), float(target_value), None))
    for competitor in competitors:
        point = (competitor.get("history") or {}).get(latest_period) or {}
        raw_value = point.get("raw_value")
        if raw_value is not None:
            candidates.append((str(competitor.get("brand_key") or ""), float(raw_value), competitor))

    target_rank: int | None = None
    for rank, (_brand_key, _raw_value, competitor) in enumerate(
        sorted(candidates, key=lambda item: (-item[1], item[0])),
        start=1,
    ):
        if competitor is None:
            target_point["rank"] = rank
            target_rank = rank
            continue
        competitor["rank_in_market"] = rank
        competitor["history"][latest_period]["rank"] = rank
    competitors.sort(key=lambda item: (item.get("rank_in_market") or 10**9, str(item.get("brand_key") or "")))
    return target_history, competitors, target_rank


def _market_view(kpi: dict[str, Any], snapshot_at: datetime) -> dict[str, Any]:
    source = str(kpi["source"]).upper()
    measure = str(kpi["measure"])
    target_history, competitors, target_rank = _rank_latest_period(kpi)
    return {
        "view_id": f"GENERAL.{source}.{measure}",
        "view": "general_view",
        "source": source,
        "measure": measure,
        "market_meta": {
            "market_basis": "ATC4",
            "atc4_codes": list(kpi.get("atc4_codes") or []),
            "unit_label": kpi.get("unit_label"),
            "snapshot_at": snapshot_at.isoformat(),
        },
        "market_size": {
            "history": dict(kpi.get("market_size_history") or {}),
            "hhi_5y": list(kpi.get("hhi_series_5y") or []),
        },
        "target_brand_metric": {
            "brand_key": kpi.get("brand_key"),
            "history": target_history,
            "kpi_extras": {
                key: target_rank if key == "target_rank" and target_rank is not None else kpi.get(key)
                for key in (
                    "market_size_recent",
                    "market_cagr_5y_pct",
                    "hhi_recent",
                    "direct_competition_count",
                    "target_rank",
                    "brand_value_recent",
                    "brand_share_pct",
                )
            },
        },
        "competitors_top5": competitors,
    }


def build_general_brand_bundle(
    brand_key: str,
    snapshot_at: datetime,
    config: Any,
    db_conn: Any,
    *,
    mart_db: str,
    bridge_db: str,
    provider_factory: Callable[..., GeneralViewKpiProvider] = GeneralViewKpiProvider,
) -> dict[str, Any]:
    views: list[dict[str, Any]] = []
    identity: dict[str, Any] | None = None
    for source in ("ubist", "iqvia"):
        kpi = provider_factory(
            db_conn=db_conn,
            mart_db=mart_db,
            bridge_db=bridge_db,
            source=source,
            measure="sales",
        ).get_kpi(brand_key)
        if not kpi.get("available"):
            continue
        identity = identity or kpi
        views.append(_market_view(kpi, snapshot_at))
    if not views or identity is None:
        raise ValueError(f"general bundle evidence unavailable for brand_key={brand_key}")

    brand_name = str(identity.get("brand_name") or identity.get("target_brand") or "").strip()
    if not brand_name:
        raise ValueError(f"general bundle brand name unavailable for brand_key={brand_key}")
    brand_context = {
        "name": brand_name,
        "brand_key": brand_key,
        "market_scope": "ATC4",
        "atc4_codes": sorted({code for view in views for code in view["market_meta"]["atc4_codes"]}),
        "available_sources": [view["source"] for view in views],
    }
    event_bundle = build_event_bundle(brand_context, snapshot_at, config, db_conn)
    bundle: dict[str, Any] = {
        "bundle_meta": {
            "brand": brand_name,
            "brand_key": brand_key,
            "snapshot_at": snapshot_at.isoformat(),
            "config_version": config.config_version,
            "builder_version": config.builder_version,
            "bundle_kind": "general_atc4",
            "data_sources_used": {
                "market_metrics": "mart_general_brand_metric via GeneralViewKpiProvider",
                "events": "event_brand_scores",
                "forecast_simulation": "unavailable",
            },
            "available_view_count": len(views),
            "stats": {},
            "bundle_hash": None,
        },
        "brand_context": brand_context,
        "market_views": views,
        "event_bundle": event_bundle,
        "competitor_events": {"by_source": {}, "by_view": {}},
        "forecast_simulation": {
            "available": False,
            "by_view": {},
            "schema_version": "general_view_no_forecast_v1",
            "note": "General ATC4 evidence has no audited 1y/3y/5y forecast; do not invent horizon values.",
        },
    }
    rough = {key: value for key, value in bundle.items() if key != "bundle_meta"}
    bundle["bundle_meta"]["stats"] = {
        "event_count_brand_centric": len(event_bundle.get("events_brand_centric") or []),
        "event_count_market_trend": len(event_bundle.get("events_market_trend") or []),
        "event_count_cross": len(event_bundle.get("cross_match_events") or []),
        "estimated_tokens": int(len(deterministic_json_dumps(rough)) / 3.5),
    }
    bundle["bundle_meta"]["bundle_hash"] = compute_bundle_hash(bundle)
    return bundle
