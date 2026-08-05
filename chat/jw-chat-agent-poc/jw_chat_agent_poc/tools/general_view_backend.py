from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

from jw_chat_agent_poc.tool_use.market_scope_contract import BrandOutsideCompositeScopeError


class GeneralViewBackendError(RuntimeError):
    """Raised when the general-view backend is unavailable or returns an unsafe scope."""


class GeneralViewBrandMismatchError(GeneralViewBackendError):
    """Raised when an ATC4 candidate has no current-period row for the requested brand."""


@dataclass(frozen=True, slots=True)
class AtcCandidate:
    code: str
    description: str


@dataclass(frozen=True, slots=True)
class TopBrand:
    brand: str
    rank: int | None
    value: float | None
    share_pct: float | None


@dataclass(frozen=True, slots=True)
class BrandMetricPoint:
    period: str
    value: float | None
    share_pct: float | None
    rank: int | None


@dataclass(frozen=True, slots=True)
class GeneralMarket:
    view_type: str
    market_basis: str
    atc4_code: str
    atc4_description: str
    source: str
    measure: str
    unit: str
    period: str
    market_size: float | None
    brand: str | None
    brand_value: float | None
    brand_share_pct: float | None
    brand_rank: int | None
    top_brands: tuple[TopBrand, ...]
    market_size_series: tuple[tuple[str, float], ...] = ()
    member_brands: tuple[TopBrand, ...] = ()
    member_population: tuple[str, ...] | None = None
    active_members: tuple[TopBrand, ...] = ()
    display_members: tuple[TopBrand, ...] = ()
    selected_data_path: str = "backend_fallback"
    fallback_reason: str | None = None
    hhi_recent: float | None = None
    market_size_period: str | None = None
    hhi_period: str | None = None
    brand_metric_series: tuple[BrandMetricPoint, ...] = ()
    atc4_codes: tuple[str, ...] = ()
    scope_filters: tuple[tuple[str, tuple[str, ...]], ...] = ()
    dashboard_tables: tuple[dict[str, object], ...] = ()
    growth_contribution: dict[str, object] | None = None
    market_growth_series: tuple[dict[str, object], ...] = ()
    hhi_series: tuple[tuple[str, float], ...] = ()
    market_share_trajectory: tuple[dict[str, object], ...] = ()
    company_ranking_series: tuple[dict[str, object], ...] = ()
    customer_competition_trend: dict[str, object] | None = None


@dataclass(slots=True)
class _CacheEntry:
    expires_at: float
    value: object


class GeneralViewBackend:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        connect_timeout: float | None = None,
        read_timeout: float | None = None,
        ttl_seconds: int | None = None,
        max_entries: int | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self._base_url = (base_url or os.environ.get("GENERAL_VIEW_BACKEND_URL") or "http://jw-market-backend-api-service").rstrip("/")
        self._timeout = (
            connect_timeout or float(os.environ.get("GENERAL_VIEW_CONNECT_TIMEOUT_SECONDS", "3")),
            read_timeout or float(os.environ.get("GENERAL_VIEW_READ_TIMEOUT_SECONDS", "10")),
        )
        self._ttl_seconds = ttl_seconds or int(os.environ.get("GENERAL_VIEW_CACHE_TTL_SECONDS", "300"))
        self._max_entries = max_entries or int(os.environ.get("GENERAL_VIEW_CACHE_MAX_ENTRIES", "256"))
        self._session = session or requests.Session()
        self._cache: dict[tuple[object, ...], _CacheEntry] = {}
        self._lock = threading.Lock()

    def candidates(self, brand: str, source: str) -> tuple[AtcCandidate, ...]:
        key = ("candidates", source.lower(), brand.strip())
        cached = self._get_cached(key)
        if isinstance(cached, tuple):
            return cached
        payload = self._get_json(
            "/api/market-filter/atc-options",
            params={"brand_name": brand, "view": "general", "source": source},
        )
        value = parse_atc_candidates(payload)
        self._put_cached(key, value)
        return value

    def market(self, atc4: str, brand: str | None, source: str, measure: str) -> GeneralMarket:
        key = ("market", source.lower(), measure.lower(), atc4.upper(), brand or "")
        cached = self._get_cached(key)
        if isinstance(cached, GeneralMarket):
            return cached
        filters: dict[str, object] = {"atc4": [atc4.upper()]}
        if brand:
            filters["focus_brand_key"] = focus_brand_key(brand)
        payload = self._post_json(
            "/api/dynamic-market",
            json={"view": "general", "filters": filters, "source": source, "measure": measure},
        )
        value = parse_general_market_response(
            payload,
            requested_atc4=atc4,
            requested_source=source,
            requested_measure=measure,
            requested_brand=brand,
        )
        self._put_cached(key, value)
        return value

    def composite_market(
        self,
        atc4: tuple[str, ...],
        filters: tuple[tuple[str, tuple[str, ...]], ...],
        brand: str | None,
        source: str,
        measure: str,
    ) -> GeneralMarket:
        normalized_atc4 = tuple(dict.fromkeys(code.upper() for code in atc4))
        normalized_filters = tuple(sorted(filters))
        key = (
            "composite_market", source.lower(), measure.lower(), normalized_atc4,
            normalized_filters, brand or "",
        )
        cached = self._get_cached(key)
        if isinstance(cached, GeneralMarket):
            return cached
        request_filters: dict[str, object] = {
            "atc4": list(normalized_atc4),
            "analysis_level": {
                source.lower(): {
                    name: list(values) for name, values in normalized_filters
                }
            },
        }
        if brand:
            request_filters["focus_brand_key"] = focus_brand_key(brand)
        payload = self._post_json(
            "/api/dynamic-market",
            json={"view": "general", "filters": request_filters, "source": source, "measure": measure},
        )
        value = parse_composite_market_response(
            payload,
            requested_atc4=normalized_atc4,
            requested_filters=normalized_filters,
            requested_source=source,
            requested_measure=measure,
            requested_brand=brand,
        )
        self._put_cached(key, value)
        return value

    def _get_json(self, path: str, *, params: dict[str, object]) -> dict[str, Any]:
        return self._request_json("GET", path, params=params)

    def _post_json(self, path: str, *, json: dict[str, object]) -> dict[str, Any]:
        return self._request_json("POST", path, json=json)

    def _request_json(self, method: str, path: str, **kwargs: object) -> dict[str, Any]:
        try:
            response = self._session.request(method, self._base_url + path, timeout=self._timeout, **kwargs)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise GeneralViewBackendError(f"general-view backend unavailable: {type(exc).__name__}") from exc
        if not isinstance(payload, dict):
            raise GeneralViewBackendError("general-view backend returned a non-object payload")
        return payload

    def _get_cached(self, key: tuple[object, ...]) -> object | None:
        now = time.monotonic()
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._cache.pop(key, None)
                return None
            return entry.value

    def _put_cached(self, key: tuple[object, ...], value: object) -> None:
        with self._lock:
            if len(self._cache) >= self._max_entries and key not in self._cache:
                oldest = min(self._cache, key=lambda candidate: self._cache[candidate].expires_at)
                self._cache.pop(oldest, None)
            self._cache[key] = _CacheEntry(time.monotonic() + self._ttl_seconds, value)


def parse_atc_candidates(payload: dict[str, Any]) -> tuple[AtcCandidate, ...]:
    body = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    raw = body.get("flagged_atc4") or body.get("atc4") or body.get("candidates") or body.get("options") or []
    if isinstance(raw, str):
        raw = [raw]
    candidates: list[AtcCandidate] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                code, description = item, f"ATC4 {item}"
            elif isinstance(item, dict):
                code = str(item.get("code") or item.get("atc4") or item.get("value") or "")
                description = str(item.get("description") or item.get("label") or f"ATC4 {code}")
            else:
                continue
            code = code.strip().upper()
            if code and code not in {candidate.code for candidate in candidates}:
                candidates.append(AtcCandidate(code, description.strip()))
    if not candidates:
        market_id = str(body.get("market_id") or "").strip().upper()
        if market_id:
            candidates.append(AtcCandidate(market_id, f"ATC4 {market_id}"))
    return tuple(candidates)


def parse_general_market_response(
    payload: dict[str, Any],
    *,
    requested_atc4: str,
    requested_source: str,
    requested_measure: str,
    requested_brand: str | None = None,
) -> GeneralMarket:
    result = payload.get("result")
    if not isinstance(result, dict):
        raise GeneralViewBackendError("general-view backend response has no result object")
    market_meta = result.get("market_meta")
    filters = market_meta.get("filters") if isinstance(market_meta, dict) else None
    if not isinstance(filters, dict):
        raise GeneralViewBackendError("general-view scope mismatch: missing filter echo")

    echoed_atc4 = filters.get("atc4")
    if isinstance(echoed_atc4, str):
        echoed_atc4 = [echoed_atc4]
    echoed_codes = {str(value).upper() for value in echoed_atc4 or []}
    mismatches = (
        str(filters.get("view") or "").lower() != "general",
        echoed_codes != {requested_atc4.upper()},
        _normalize_source(filters.get("source")) != _normalize_source(requested_source),
        str(filters.get("measure") or "").lower() != requested_measure.lower(),
    )
    if any(mismatches):
        raise GeneralViewBackendError("general-view scope mismatch: response filter echo differs from request")

    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    kpi = data.get("kpi") if isinstance(data.get("kpi"), dict) else {}
    source_data = data.get("sources_data") if isinstance(data.get("sources_data"), dict) else {}
    series = source_data.get("market_size_series")
    period = "latest"
    if isinstance(series, list) and series:
        period = str(series[-1].get("period") or "latest") if isinstance(series[-1], dict) else "latest"
    elif isinstance(series, dict) and series:
        period = sorted(str(value) for value in series)[-1]
    hhi_recent = _hhi_for_period(data.get("hhi_series_5y"), period)
    hhi_series = _period_value_series(data.get("hhi_series_5y"), "hhi")

    matrix = data.get("ei_ms_matrix") if isinstance(data.get("ei_ms_matrix"), dict) else {}
    current_rows = matrix.get("data")
    if not isinstance(current_rows, list):
        current_rows = []
    current_brands = tuple(sorted((
        TopBrand(
            brand=str(row.get("brand") or row.get("brand_name") or ""),
            rank=_first_int(row, "rank", "rank_overall"),
            value=_first_float(row, "value_recent", "raw_value"),
            share_pct=_first_float(row, "share_pct", "ms_recent_pct", "ms_pct"),
        )
        for row in current_rows
        if isinstance(row, dict)
        if row.get("brand") or row.get("brand_name")
    ), key=lambda row: row.rank if row.rank is not None else 10_000))
    requested_row = None
    if requested_brand:
        requested_key = _normalize_brand_name(requested_brand)
        requested_row = next(
            (row for row in current_brands if _normalize_brand_name(row.brand) == requested_key),
            None,
        )
        if requested_row is None:
            raise GeneralViewBrandMismatchError(
                "general-view brand mismatch: requested brand is absent from current-period matrix"
            )
    active_members = tuple(
        row for row in current_brands if row.value is not None and row.value > 0
    )
    top_brands = active_members[:5]
    ranked_members = _latest_ranked_members(data.get("brand_ranking"))
    member_brands = (
        ranked_members if len(ranked_members) > len(current_brands) else current_brands
    )
    member_population = (
        tuple(row.brand for row in ranked_members) if ranked_members else None
    )
    description = str(
        market_meta.get("market_definition_label")
        or market_meta.get("market_name")
        or f"ATC4 {requested_atc4.upper()}"
    )
    market_growth_series = _mapping_rows(
        data.get("market_yoy_series")
        or source_data.get("market_yoy_series")
        or source_data.get("market_size_series")
    )
    market_share_trajectory = _ranking_rows(
        data.get("brand_ranking_stacked"), label_key="brand"
    )
    company_ranking_series = _ranking_rows(
        data.get("company_ranking_stacked"), label_key="company"
    )
    customer_competition = data.get("target_customer_competition_by_channel")
    customer_competition_trend = (
        dict(customer_competition) if isinstance(customer_competition, dict) else None
    )
    return GeneralMarket(
        view_type="general_view",
        market_basis="ATC4",
        atc4_code=requested_atc4.upper(),
        atc4_description=description,
        source=requested_source.upper(),
        measure=requested_measure.lower(),
        unit=str(result.get("unit_label") or ""),
        period=period,
        market_size=_as_float(kpi.get("market_size_recent")),
        brand=requested_row.brand if requested_row else str(kpi.get("target_brand") or "") or None,
        brand_value=requested_row.value if requested_row else _as_float(kpi.get("brand_value_recent")),
        brand_share_pct=(
            requested_row.share_pct
            if requested_row
            else _as_float(kpi.get("target_share_pct") or kpi.get("brand_share_pct"))
        ),
        brand_rank=requested_row.rank if requested_row else _as_int(kpi.get("target_rank")),
        top_brands=top_brands,
        market_size_series=tuple(
            (str(item.get("period")), float(item.get("value")))
            for item in source_data.get("market_size_series", [])
            if isinstance(item, dict)
            and item.get("period")
            and isinstance(item.get("value"), int | float)
        ),
        member_brands=member_brands,
        member_population=member_population,
        active_members=active_members,
        display_members=top_brands,
        hhi_recent=hhi_recent,
        market_size_period=period if period != "latest" else None,
        hhi_period=period if hhi_recent is not None and period != "latest" else None,
        market_growth_series=market_growth_series,
        hhi_series=hhi_series,
        market_share_trajectory=market_share_trajectory,
        company_ranking_series=company_ranking_series,
        customer_competition_trend=customer_competition_trend,
    )


def focus_brand_key(brand: str) -> str:
    """Normalize a display brand name to the backend's brand_key convention.

    The slimmed dynamic-market API matches focus_brand_key by exact brand_key
    equality (casefolded, whitespace/punctuation stripped), e.g.
    "휴텍스 아토르바스타틴" → "휴텍스아토르바스타틴", "휴마로그 100I.U/mL" → "휴마로그100iuml".
    """

    return re.sub(r"[^0-9a-z가-힣]", "", brand.casefold())


def canonical_hhi(values: tuple[float, ...]) -> float | None:
    total = sum(values)
    if total <= 0:
        return None
    return sum((value / total * 100) ** 2 for value in values)


def parse_composite_market_response(
    payload: dict[str, Any],
    *,
    requested_atc4: tuple[str, ...],
    requested_filters: tuple[tuple[str, tuple[str, ...]], ...],
    requested_source: str,
    requested_measure: str,
    requested_brand: str | None = None,
) -> GeneralMarket:
    result = payload.get("result")
    if not isinstance(result, dict):
        raise GeneralViewBackendError("composite backend response has no result object")
    market_meta = result.get("market_meta")
    echoed = market_meta.get("filters") if isinstance(market_meta, dict) else None
    if not isinstance(echoed, dict):
        raise GeneralViewBackendError("composite scope mismatch: missing filter echo")
    echoed_atc4 = echoed.get("atc4")
    if isinstance(echoed_atc4, str):
        echoed_atc4 = [echoed_atc4]
    if tuple(str(value).upper() for value in echoed_atc4 or ()) != requested_atc4:
        raise GeneralViewBackendError("composite scope mismatch: ATC4 echo differs")
    if (
        str(echoed.get("view") or "").lower() != "general"
        or _normalize_source(echoed.get("source")) != _normalize_source(requested_source)
        or str(echoed.get("measure") or "").lower() != requested_measure.lower()
    ):
        raise GeneralViewBackendError("composite scope mismatch: response echo differs")
    _assert_composite_filter_echo(echoed, requested_filters, requested_source)

    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    calculation = data.get("hhi_calculation_input")
    if not isinstance(calculation, dict):
        raise GeneralViewBackendError("composite response has no canonical HHI input")
    period = str(calculation.get("period") or "")
    rows = calculation.get("brand_values")
    if not period or not isinstance(rows, list):
        raise GeneralViewBackendError("composite HHI input is incomplete")
    raw_brand_values = tuple(
        (str(row.get("brand") or ""), _as_float(row.get("value")))
        for row in rows
        if isinstance(row, dict) and row.get("brand")
    )
    values = tuple(value for _brand, value in raw_brand_values if value is not None)
    market_total = _as_float(calculation.get("market_total"))
    if market_total is None or abs(sum(values) - market_total) > 1e-6:
        raise GeneralViewBackendError("composite HHI input total mismatch")
    hhi_recent = canonical_hhi(values)
    supplied_hhi = _as_float(calculation.get("hhi_raw"))
    if supplied_hhi is not None and (
        hhi_recent is None or abs(supplied_hhi - hhi_recent) > 1e-9
    ):
        raise GeneralViewBackendError("composite HHI input value mismatch")
    raw_members = tuple(
        TopBrand(
            brand=brand,
            rank=1
            + sum(
                1
                for candidate in values
                if value is not None and candidate > value
            ),
            value=value,
            share_pct=(
                value / market_total * 100
                if value is not None and market_total > 0
                else None
            ),
        )
        for brand, value in raw_brand_values
    )

    matrix = data.get("ei_ms_matrix") if isinstance(data.get("ei_ms_matrix"), dict) else {}
    matrix_rows = matrix.get("data") if isinstance(matrix.get("data"), list) else []
    display_members = tuple(
        sorted(
            (
                TopBrand(
                    brand=str(row.get("brand") or row.get("brand_name") or ""),
                    rank=_first_int(row, "rank", "rank_overall"),
                    value=_first_float(row, "value_recent", "raw_value"),
                    share_pct=_first_float(row, "share_pct", "ms_recent_pct", "ms_pct"),
                )
                for row in matrix_rows
                if isinstance(row, dict) and (row.get("brand") or row.get("brand_name"))
            ),
            key=lambda row: row.rank if row.rank is not None else 10_000,
        )
    )
    active_members = tuple(row for row in raw_members if (row.value or 0) > 0)
    member_population = tuple(brand for brand, _value in raw_brand_values)
    requested_row = None
    if requested_brand:
        requested_key = _normalize_brand_name(requested_brand)
        requested_row = next(
            (row for row in raw_members if _normalize_brand_name(row.brand) == requested_key),
            None,
        )
        if not any(
            _normalize_brand_name(value) == requested_key for value in member_population
        ) or requested_row is None:
            raise BrandOutsideCompositeScopeError(requested_brand)

    kpi = data.get("kpi") if isinstance(data.get("kpi"), dict) else {}
    series_raw = data.get("market_size_series")
    series = tuple(
        (str(item.get("period")), float(item.get("value")))
        for item in series_raw or ()
        if isinstance(item, dict) and item.get("period") and isinstance(item.get("value"), int | float)
    )
    growth = data.get("growth_contribution")
    growth_payload = growth if isinstance(growth, dict) else None
    market_size = _as_float(kpi.get("market_size_recent"))
    effective_market_size = market_total if market_size is None else market_size
    unit = str(result.get("unit_label") or "")
    kpi_rows: list[tuple[object, ...]] = [
        ("시장 규모", effective_market_size, unit, period),
        ("HHI", hhi_recent, "index", period),
    ]
    if requested_row is not None:
        kpi_rows.extend(
            (
                (f"{requested_row.brand} 매출", requested_row.value, unit, period),
                (f"{requested_row.brand} 점유율", requested_row.share_pct, "%", period),
                (f"{requested_row.brand} 순위", requested_row.rank, "rank", period),
            )
        )
    tables: list[dict[str, object]] = [
        {
            "name": "시장 KPI",
            "columns": ("항목", "값", "단위", "기간"),
            "rows": tuple(row for row in kpi_rows if row[1] is not None),
        },
        {
            "name": "시장 규모 추이",
            "columns": ("기간", "시장 규모", "단위"),
            "rows": tuple((row_period, value, unit) for row_period, value in series),
        },
        {
            "name": "브랜드 순위",
            "columns": ("순위", "브랜드", "최근 값", "점유율(%)"),
            "rows": tuple((row.rank, row.brand, row.value, row.share_pct) for row in display_members),
        }
    ]
    contributors = (
        growth_payload.get("by_brand", {}).get("top_contributors", [])
        if isinstance(growth_payload, dict) and isinstance(growth_payload.get("by_brand"), dict)
        else []
    )
    if isinstance(contributors, list) and contributors:
        tables.append(
            {
                "name": "성장 기여",
                "columns": ("브랜드", "성장 기여", "기여율(%)"),
                "rows": tuple(
                    (str(row.get("brand") or ""), _as_float(row.get("contribution")), _as_float(row.get("contribution_pct")))
                    for row in contributors if isinstance(row, dict)
                ),
            }
        )
    label = str(market_meta.get("market_definition_label") or "ATC4 " + ", ".join(requested_atc4))
    return GeneralMarket(
        view_type="general_view",
        market_basis="ATC4 composite",
        atc4_code=",".join(requested_atc4),
        atc4_description=label,
        source=requested_source.upper(),
        measure=requested_measure.lower(),
        unit=unit,
        period=period,
        market_size=effective_market_size,
        brand=requested_row.brand if requested_row else None,
        brand_value=requested_row.value if requested_row else None,
        brand_share_pct=requested_row.share_pct if requested_row else None,
        brand_rank=requested_row.rank if requested_row else None,
        top_brands=display_members[:5],
        market_size_series=series,
        member_brands=raw_members,
        member_population=member_population,
        active_members=active_members,
        display_members=display_members,
        selected_data_path="dynamic_market_composite",
        hhi_recent=hhi_recent,
        market_size_period=period,
        hhi_period=period,
        atc4_codes=requested_atc4,
        scope_filters=requested_filters,
        dashboard_tables=tuple(tables),
        growth_contribution=growth_payload,
    )


def _assert_composite_filter_echo(
    echoed: dict[str, Any],
    requested_filters: tuple[tuple[str, tuple[str, ...]], ...],
    source: str,
) -> None:
    analysis = echoed.get("analysis_level")
    channel = echoed.get("channel_axis")
    analysis = analysis if isinstance(analysis, dict) else {}
    channel = channel if isinstance(channel, dict) else {}
    aliases = {"mfr_name_kor": "mfr", "pack_desc": "pack", "nhi_type": "nhi"} if _normalize_source(source) == "iqvia" else {}
    channel_names = {"audit_code"} if _normalize_source(source) == "iqvia" else {"facility", "specialty"}
    for name, values in requested_filters:
        container = channel if name in channel_names else analysis
        raw = container.get(aliases.get(name, name))
        if isinstance(raw, str):
            raw = [raw]
        if tuple(str(value) for value in raw or ()) != values:
            raise GeneralViewBackendError(f"composite scope mismatch: filter echo differs for {name}")


def _normalize_source(value: object) -> str:
    normalized = str(value or "").lower().replace("-", "_")
    return "iqvia" if normalized in {"iqvia", "iqvia_nsa"} else normalized


def _normalize_brand_name(value: str) -> str:
    return "".join(value.lower().split())


def _as_float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _as_int(value: object) -> int | None:
    return int(value) if isinstance(value, int | float) else None


def _hhi_for_period(value: object, period: str) -> float | None:
    if isinstance(value, dict):
        return _as_float(value.get(period))
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and str(item.get("period") or "") == period:
                raw = item.get("hhi") if "hhi" in item else item.get("value")
                return _as_float(raw)
    return None


def _period_value_series(value: object, field: str) -> tuple[tuple[str, float], ...]:
    if isinstance(value, dict):
        return tuple(
            (str(period), float(item))
            for period, item in value.items()
            if isinstance(item, int | float)
        )
    if not isinstance(value, list):
        return ()
    return tuple(
        (str(item["period"]), float(item[field] if field in item else item["value"]))
        for item in value
        if isinstance(item, dict)
        and item.get("period") is not None
        and isinstance(item.get(field) if field in item else item.get("value"), int | float)
    )


def _mapping_rows(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, dict))


def _ranking_rows(
    value: object,
    *,
    label_key: str,
) -> tuple[dict[str, object], ...]:
    if isinstance(value, list):
        return _mapping_rows(value)
    if not isinstance(value, dict) or not isinstance(value.get("yearly"), list):
        return ()
    rows: list[dict[str, object]] = []
    for yearly in value["yearly"]:
        if not isinstance(yearly, dict) or not isinstance(yearly.get("rankings"), list):
            continue
        period = yearly.get("year")
        for ranking in yearly["rankings"]:
            if not isinstance(ranking, dict):
                continue
            label = ranking.get(label_key) or ranking.get(f"{label_key}_name")
            if not label:
                continue
            rows.append(
                {
                    "period": str(period) if period is not None else None,
                    label_key: label,
                    "value": ranking.get("value"),
                    "ms": ranking.get("ms_pct"),
                    "rank": ranking.get("rank"),
                }
            )
    return tuple(rows)


def _latest_ranked_members(value: object) -> tuple[TopBrand, ...]:
    if not isinstance(value, dict):
        return ()
    yearly = value.get("yearly")
    if not isinstance(yearly, list):
        return ()
    rows = [row for row in yearly if isinstance(row, dict)]
    if not rows:
        return ()
    latest = max(rows, key=lambda row: str(row.get("year") or ""))
    rankings = latest.get("rankings")
    if not isinstance(rankings, list):
        return ()
    return tuple(
        sorted(
            (
                TopBrand(
                    brand=str(row.get("brand") or row.get("brand_name") or ""),
                    rank=_first_int(row, "rank", "rank_overall"),
                    value=_first_float(row, "value", "raw_value", "value_recent"),
                    share_pct=_first_float(row, "ms_pct", "share_pct", "ms"),
                )
                for row in rankings
                if isinstance(row, dict) and (row.get("brand") or row.get("brand_name"))
            ),
            key=lambda row: (
                row.rank is None,
                row.rank if row.rank is not None else 10_000,
                row.brand,
            ),
        )
    )


def _first_float(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _as_float(row.get(key))
        if value is not None:
            return value
    return None


def _first_int(row: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = _as_int(row.get(key))
        if value is not None:
            return value
    return None
