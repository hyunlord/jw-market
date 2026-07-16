from __future__ import annotations

from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlencode

from pipeline.scripts.gates.latency_matrix_required import required_general_scenarios
from pipeline.scripts.gates.latency_matrix_types import MatrixCase


LATENCY_MATRIX_PROVENANCE = {
    "population_rule": "default brands plus required edge brands; all discovered contexts and listed sources",
    "reference": "live-production",
}


def _brand_name(item: Mapping[str, Any]) -> str:
    return str(item.get("brand") or item.get("name") or item.get("brand_name") or "").strip()


def _items(payload: object) -> list[Mapping[str, Any]]:
    raw = payload.get("brands") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _exact_search_item(payload: object, brand: str) -> Mapping[str, Any] | None:
    return next((item for item in _items(payload) if _brand_name(item) == brand), None)


def resolved_brand_names(
    search_payloads: Mapping[str, object], requested_brands: Sequence[str]
) -> tuple[str, ...]:
    return tuple(
        brand for brand in requested_brands if _exact_search_item(search_payloads.get(brand), brand) is not None
    )


def _sources(item: Mapping[str, Any], fallback: Mapping[str, Any], view: str) -> tuple[str, ...]:
    field = "general_sources" if view == "general" else "strategic_sources"
    raw = item.get(field) or fallback.get(field) or []
    if not isinstance(raw, list | tuple):
        return ()
    return tuple(sorted({str(source).strip().lower() for source in raw if str(source).strip()}))


def _contexts(item: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    raw = item.get("contexts")
    if not isinstance(raw, list):
        return ()
    contexts = {
        (str(context.get("view_kind") or "").strip(), str(context.get("market_id") or "").strip())
        for context in raw
        if isinstance(context, dict) and context.get("has_market_data") is not False
    }
    return tuple(sorted(context for context in contexts if all(context)))


def _atc4_values(meta: Mapping[str, Any], contexts: Sequence[tuple[str, str]]) -> list[str]:
    raw = meta.get("atc_codes") or []
    values = [str(value).strip() for value in raw if str(value).strip()] if isinstance(raw, list) else []
    if values:
        return sorted(set(values))
    return sorted({market_id for view, market_id in contexts if view == "general"})


def _dynamic_case(brand: str, view: str, source: str, atc4: Sequence[str]) -> MatrixCase:
    return MatrixCase(
        identifier=f"dynamic:{brand}:{view}:{source}:sales",
        method="POST",
        path="/api/dynamic-market",
        body={
            "filters": {"atc4": list(atc4), "focus_brand_key": brand},
            "measure": "sales",
            "options": {"period_range": {"start": "2025-06", "end": "2026-05"}},
            "source": source,
            "view": view,
        },
    )


def _filter_options_case(brand: str, view: str, source: str) -> MatrixCase:
    query = urlencode({"view": view, "source": source, "measure": "sales", "brand": brand})
    return MatrixCase(
        identifier=f"filter_options:{brand}:{view}:{source}:sales",
        method="GET",
        path=f"/api/dynamic-market/filter-options?{query}",
    )


def _activity_filters(view: str, market_id: str) -> dict[str, Any]:
    return {"atc": {"atc4": [market_id]}} if view == "general" else {}


def _activity_cases(brand: str, view: str, market_id: str) -> tuple[MatrixCase, ...]:
    base: dict[str, Any] = {
        "filters": _activity_filters(view, market_id),
        "selected_brand": brand,
        "view": view,
    }
    if view != "general":
        base["market_id"] = market_id
    suffix = f"{brand}:{view}:{market_id}"
    return (
        MatrixCase(
            identifier=f"brand_activity:topics:{suffix}",
            method="POST",
            path="/api/brand-activity/topics",
            body={**base, "period_start": "2025-04", "period_end": "2026-03", "top_n": 7},
        ),
        MatrixCase(
            identifier=f"brand_activity:csd_timeseries:{suffix}",
            method="POST",
            path="/api/brand-activity/csd-timeseries",
            body={**base, "mode": "absolute", "window": {"start": "2024-Q1", "end": "2025-Q4"}},
        ),
        MatrixCase(
            identifier=f"brand_activity:interest_rx:{suffix}",
            method="POST",
            path="/api/brand-activity/interest-rx-matrix",
            body={**base, "period_start": "2025-04", "period_end": "2026-03"},
        ),
    )


def _group_activity_cases(option_id: str, member: str) -> tuple[MatrixCase, ...]:
    filters = {"market_scope": {"option_id": option_id, "member": member}}
    base = {"filters": filters, "selected_brand": member, "view": "general"}
    suffix = f"{option_id}:{member}"
    return (
        MatrixCase(
            identifier=f"brand_activity_group:topics:{suffix}",
            method="POST",
            path="/api/brand-activity/topics",
            body={**base, "period_start": "2025-04", "period_end": "2026-03", "top_n": 7},
        ),
        MatrixCase(
            identifier=f"brand_activity_group:csd_timeseries:{suffix}",
            method="POST",
            path="/api/brand-activity/csd-timeseries",
            body={**base, "mode": "absolute", "window": {"start": "2024-Q1", "end": "2025-Q4"}},
        ),
        MatrixCase(
            identifier=f"brand_activity_group:csd_activity:{suffix}",
            method="POST",
            path="/api/brand-activity/csd-activity-series",
            body=base,
        ),
        MatrixCase(
            identifier=f"brand_activity_group:interest_rx:{suffix}",
            method="POST",
            path="/api/brand-activity/interest-rx-matrix",
            body={**base, "period_start": "2025-04", "period_end": "2026-03"},
        ),
    )


def _context_cases(brand: str, view: str, market_id: str, sources: Sequence[str]) -> list[MatrixCase]:
    cases = list(_activity_cases(brand, view, market_id))
    for source in sources:
        query = urlencode({"view_kind": view, "market_id": market_id, "source": source})
        cases.append(
            MatrixCase(
                identifier=f"deep:{brand}:{view}:{market_id}:{source}",
                method="GET",
                path=f"/api/deep-analysis/{quote(brand)}?{query}",
                mask_generated_at=True,
            )
        )
        if view in {"strategic_ml", "strategic_cd"}:
            cause_view = "market_landscape" if view == "strategic_ml" else "competitive_dynamics"
            cause_query = urlencode(
                {"view": cause_view, "source": source.upper(), "measure": "sales", "market_id": market_id}
            )
            cases.append(
                MatrixCase(
                    identifier=f"cause:{brand}:{cause_view}:{market_id}:{source}:sales",
                    method="GET",
                    path=f"/api/cause/{quote(brand)}?{cause_query}",
                )
            )
    return cases


def build_latency_matrix_cases(
    default_brands: object,
    search_payloads: Mapping[str, object],
    *,
    requested_brands: Sequence[str],
    required_cd_brands: Sequence[str] = (),
    group_scopes: Sequence[tuple[str, str]] = (),
) -> tuple[MatrixCase, ...]:
    defaults = {_brand_name(item): item for item in _items(default_brands) if _brand_name(item)}
    all_requested = tuple(
        dict.fromkeys((*requested_brands, *required_cd_brands, *(member for _option_id, member in group_scopes)))
    )
    missing_cd = []
    for brand in required_cd_brands:
        item = _exact_search_item(search_payloads.get(brand), brand)
        if item is None or not any(view == "strategic_cd" for view, _market_id in _contexts(item)):
            missing_cd.append(brand)
    if missing_cd:
        raise ValueError("required strategic_cd brands unresolved: " + ",".join(sorted(missing_cd)))
    cases = [
        MatrixCase(identifier=scenario.identifier, method="POST", path="/api/dynamic-market", body=scenario.body)
        for scenario in required_general_scenarios()
    ]
    for brand in resolved_brand_names(search_payloads, all_requested):
        item = _exact_search_item(search_payloads[brand], brand)
        if item is None:
            continue
        contexts = _contexts(item)
        atc4 = _atc4_values(defaults.get(brand, {}), contexts)
        cases.append(
            MatrixCase(
                identifier=f"brand_activity:presence:{brand}",
                method="GET",
                path=f"/api/brand-activity/csd-presence?{urlencode({'brand': brand})}",
            )
        )
        views = sorted({view for view, _market_id in contexts})
        for view in views:
            view_market_id = next(market_id for context_view, market_id in contexts if context_view == view)
            sources = _sources(item, defaults.get(brand, {}), view)
            for source in sources:
                cases.extend((_dynamic_case(brand, view, source, atc4), _filter_options_case(brand, view, source)))
            cases.append(
                MatrixCase(
                    identifier=f"brand_activity:csd_activity:{brand}:{view}",
                    method="POST",
                    path="/api/brand-activity/csd-activity-series",
                    body={"filters": _activity_filters(view, view_market_id), "selected_brand": brand, "view": view},
                )
            )
            for context_view, market_id in contexts:
                if context_view == view:
                    cases.extend(_context_cases(brand, view, market_id, sources))
    for option_id, member in group_scopes:
        if member not in resolved_brand_names(search_payloads, (member,)):
            raise ValueError(f"required brand activity group member unresolved: {option_id}:{member}")
        cases.extend(_group_activity_cases(option_id, member))
    indexed = {case.identifier: case for case in cases}
    if len(indexed) != len(cases):
        raise ValueError("latency matrix case identities must be unique")
    return tuple(indexed[identifier] for identifier in sorted(indexed))
