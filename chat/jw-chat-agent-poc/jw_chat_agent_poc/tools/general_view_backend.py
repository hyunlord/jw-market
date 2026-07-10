from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests


class GeneralViewBackendError(RuntimeError):
    """Raised when the general-view backend is unavailable or returns an unsafe scope."""


class GeneralViewBrandMismatchError(GeneralViewBackendError):
    """Raised when an ATC4 candidate has no current ranking row for the requested brand."""


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
        options = {"top_n": 100 if brand else 5}
        key = ("market", source.lower(), measure.lower(), atc4.upper(), brand or "", tuple(sorted(options.items())))
        cached = self._get_cached(key)
        if isinstance(cached, GeneralMarket):
            return cached
        filters: dict[str, object] = {"atc4": [atc4.upper()]}
        if brand:
            filters["focus_brand_key"] = brand
        payload = self._post_json(
            "/api/dynamic-market",
            json={"filters": filters, "source": source, "measure": measure, "options": options},
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

    ranking_rows: list[dict[str, Any]] = []
    ranking = data.get("brand_ranking") if isinstance(data.get("brand_ranking"), dict) else {}
    yearly = ranking.get("yearly")
    if isinstance(yearly, list) and yearly:
        latest = yearly[-1]
        if isinstance(latest, dict) and isinstance(latest.get("rankings"), list):
            ranking_rows = [row for row in latest["rankings"] if isinstance(row, dict)]

    ranked_brands = tuple(sorted((
        TopBrand(
            brand=str(row.get("brand") or row.get("brand_name") or ""),
            rank=_as_int(row.get("rank")),
            value=_as_float(row.get("value") or row.get("sales")),
            share_pct=_as_float(row.get("ms_pct") or row.get("share_pct")),
        )
        for row in ranking_rows
        if row.get("brand") or row.get("brand_name")
    ), key=lambda row: row.rank if row.rank is not None else 10_000))
    requested_row = None
    if requested_brand:
        requested_key = _normalize_brand_name(requested_brand)
        requested_row = next(
            (row for row in ranked_brands if _normalize_brand_name(row.brand) == requested_key),
            None,
        )
        if requested_row is None:
            raise GeneralViewBrandMismatchError(
                "general-view brand mismatch: requested brand is absent from ranking"
            )
    top_brands = ranked_brands[:5]
    description = str(
        market_meta.get("market_definition_label")
        or market_meta.get("market_name")
        or f"ATC4 {requested_atc4.upper()}"
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
    )


def _normalize_source(value: object) -> str:
    normalized = str(value or "").lower().replace("-", "_")
    return "iqvia" if normalized in {"iqvia", "iqvia_nsa"} else normalized


def _normalize_brand_name(value: str) -> str:
    return "".join(value.lower().split())


def _as_float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _as_int(value: object) -> int | None:
    return int(value) if isinstance(value, int | float) else None
