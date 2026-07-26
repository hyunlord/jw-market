from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
import os
from threading import Lock
import time
from typing import Any, Protocol
from urllib.parse import quote

import requests


DEFAULT_CAUSE_BACKEND_URL = "http://jw-market-backend-api-service"


class HttpSession(Protocol):
    def request(self, method: str, url: str, **kwargs: object) -> Any: ...


@dataclass(frozen=True, slots=True)
class CauseBackendTrace:
    endpoint: str
    status: str
    latency_ms: float
    http_status: int | None = None
    source_epoch: str | None = None
    built_at: str | None = None
    cache_hit: bool = False
    fallback_from_source: str | None = None
    fallback_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        data = {
            "endpoint": self.endpoint,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "http_status": self.http_status,
            "source_epoch": self.source_epoch,
            "built_at": self.built_at,
            "cache_hit": self.cache_hit,
        }
        if self.fallback_from_source is not None:
            data["fallback_from_source"] = self.fallback_from_source
        if self.fallback_reason is not None:
            data["fallback_reason"] = self.fallback_reason
        return data


class CauseBackendError(LookupError):
    def __init__(
        self,
        message: str,
        *,
        endpoint: str,
        status: str,
        latency_ms: float,
        http_status: int | None = None,
        source_epoch: str | None = None,
        built_at: str | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.status = status
        self.latency_ms = latency_ms
        self.http_status = http_status
        self.source_epoch = source_epoch
        self.built_at = built_at
        self.reason = reason

    def trace_fields(self) -> dict[str, Any]:
        return CauseBackendTrace(
            endpoint=self.endpoint,
            status=self.status,
            latency_ms=self.latency_ms,
            http_status=self.http_status,
            source_epoch=self.source_epoch,
            built_at=self.built_at,
        ).as_dict()


@dataclass(frozen=True, slots=True)
class CauseBrandRow:
    brand: str
    rank: int | None
    value: float | None
    share_pct: float | None
    company: str | None
    is_jw: bool
    periods: tuple[str, ...]
    values: tuple[float | None, ...]
    shares: tuple[float | None, ...]
    ranks: tuple[int | None, ...]

    def segment(self) -> dict[str, Any]:
        return {
            "brand": self.brand,
            "name": self.brand,
            "rank": self.rank,
            "value": self.value,
            "value_krw": self.value,
            "value_억원": _eok(self.value),
            "ms_recent_pct": self.share_pct,
            "company": self.company,
            "is_jw": self.is_jw,
        }

    def trend(self) -> dict[str, Any]:
        series = [
            {
                "period": period,
                "value_krw": value,
                "value_억원": _eok(value),
                "ms_pct": share,
                "rank": rank,
                "source_status": "ok" if value is not None else "missing",
            }
            for period, value, share, rank in zip(
                self.periods,
                self.values,
                self.shares,
                self.ranks,
                strict=False,
            )
        ]
        first = series[0] if series else {}
        latest = series[-1] if series else {}
        return {
            "brand": self.brand,
            "rank": self.rank,
            "ms_recent_pct": self.share_pct,
            "from_period": first.get("period"),
            "from_ms_pct": first.get("ms_pct"),
            "to_period": latest.get("period"),
            "to_ms_pct": latest.get("ms_pct"),
            "share_delta_pctp": _difference(latest.get("ms_pct"), first.get("ms_pct")),
            "value_recent": self.value,
            "value_recent_억원": _eok(self.value),
            "value_delta_krw": _difference(latest.get("value_krw"), first.get("value_krw")),
            "series": series,
            "company": self.company,
        }


@dataclass(frozen=True, slots=True)
class CauseMarket:
    brand: str
    market_name: str
    source: str
    measure: str
    period: str
    market_size: float
    market_cagr_pct: float | None
    top3_share_pct: float | None
    hhi_recent: float | None
    direct_competition_count: int | None
    brand_value: float | None
    brand_share_pct: float | None
    brand_rank: int | None
    brand_cagr_pct: float | None
    market_series: tuple[dict[str, Any], ...]
    hhi_series: tuple[dict[str, Any], ...]
    brand_rows: tuple[CauseBrandRow, ...]
    trace: CauseBackendTrace

    def render_market_scope(self, *, limit: int = 10) -> dict[str, Any]:
        bounded_limit = max(1, min(int(limit), 20))
        ranked = sorted(
            (row for row in self.brand_rows if row.rank is not None),
            key=lambda row: int(row.rank or 0),
        )
        selected = ranked[:bounded_limit]
        target = next((row for row in ranked if row.brand == self.brand), None)
        trend_rows = list(selected)
        if target is not None and all(row.brand != target.brand for row in trend_rows):
            trend_rows.append(target)
        return {
            "brand": self.brand,
            "metric": "market_top_brands",
            "market_name": self.market_name,
            "scope": "market",
            "scope_label": "시장 전체",
            "level": "Brand",
            "view_type": "market_landscape",
            "period": self.period,
            "anchor_brand": self.brand,
            "member_brands": tuple(row.brand for row in ranked),
            "total_brands_in_market": max(len(ranked), self.direct_competition_count or 0),
            "market_size_recent_krw": self.market_size,
            "market_size_억원": _eok(self.market_size),
            "market_cagr_5y_pct": self.market_cagr_pct,
            "top3_share_pct": self.top3_share_pct,
            "hhi_recent": self.hhi_recent,
            "direct_competition_count": self.direct_competition_count,
            "brand_sales_krw": self.brand_value,
            "ms_recent_pct": self.brand_share_pct,
            "rank": self.brand_rank,
            "level_segments": [row.segment() for row in selected],
            "level_top5_trend_series": [row.trend() for row in trend_rows],
            "market_size_series": list(self.market_series),
            "hhi_series_5y": list(self.hhi_series),
            "source_label": _source_label(self.source),
            "query_spec": {
                "source": _source_label(self.source),
                "view": "market_landscape",
                "filters": {"brand": self.brand, "period": self.period},
                "group_by": ["product"],
                "sort": "sales_desc",
                "limit": bounded_limit,
            },
        }

    def render_brand_metric(self, metric: str) -> dict[str, Any]:
        if metric.casefold() != "hhi":
            raise LookupError(f"cause backend metric is outside E-1: {metric}")
        data = self.render_market_scope(limit=10)
        data.update(
            {
                "metric": "hhi",
                "sales_krw": self.brand_value,
                "sales_억원": _eok(self.brand_value),
                "brand_value_series_10pt": _target_series(self.brand_rows, self.brand),
                "source_status": "ok",
            }
        )
        return data


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    expires_at: float
    market: CauseMarket


class CauseBackend:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        session: HttpSession | None = None,
        ttl_seconds: int | None = None,
        max_entries: int | None = None,
        connect_timeout_seconds: float | None = None,
        read_timeout_seconds: float | None = None,
    ) -> None:
        self._base_url = (
            base_url
            or os.environ.get("CAUSE_BACKEND_URL")
            or os.environ.get("GENERAL_VIEW_BACKEND_URL")
            or DEFAULT_CAUSE_BACKEND_URL
        ).rstrip("/")
        self._session = session or requests.Session()
        self._ttl_seconds = max(0, int(ttl_seconds if ttl_seconds is not None else os.environ.get("CAUSE_BACKEND_TTL_SECONDS", "120")))
        self._max_entries = max(
            1,
            int(max_entries if max_entries is not None else os.environ.get("CAUSE_BACKEND_CACHE_MAX_ENTRIES", "256")),
        )
        self._connect_timeout = float(
            connect_timeout_seconds
            if connect_timeout_seconds is not None
            else os.environ.get("CAUSE_BACKEND_CONNECT_TIMEOUT_S", "3")
        )
        self._read_timeout = float(
            read_timeout_seconds
            if read_timeout_seconds is not None
            else os.environ.get("CAUSE_BACKEND_READ_TIMEOUT_S", "10")
        )
        self._cache: dict[tuple[str, str, str, str], _CacheEntry] = {}
        self._lock = Lock()

    def market(
        self,
        brand: str,
        *,
        source: str = "",
        measure: str = "sales",
        view: str = "market_landscape",
    ) -> CauseMarket:
        normalized_brand = str(brand).strip()
        if not normalized_brand:
            raise ValueError("brand is required")
        selected_sources = (_api_source(source),) if source else ("UBIST", "IQVIA")
        cache_source = _api_source(source) if source else "AUTO"
        cache_key = (normalized_brand.casefold(), cache_source, measure, view)
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        first_no_data: CauseBackendError | None = None
        for index, selected_source in enumerate(selected_sources):
            try:
                market = self._request_market(
                    normalized_brand,
                    source=selected_source,
                    measure=measure,
                    view=view,
                )
            except CauseBackendError as exc:
                if not source and index == 0 and exc.status == "no_data" and exc.reason == "brand_not_in_source":
                    first_no_data = exc
                    continue
                raise
            if first_no_data is not None:
                market = replace(
                    market,
                    trace=replace(
                        market.trace,
                        fallback_from_source=selected_sources[0],
                        fallback_reason=first_no_data.reason or first_no_data.status,
                    ),
                )
            self._remember(cache_key, market)
            return market
        assert first_no_data is not None
        raise first_no_data

    def _request_market(self, brand: str, *, source: str, measure: str, view: str) -> CauseMarket:
        endpoint = f"/api/cause/{quote(brand, safe='')}"
        started = time.monotonic()
        try:
            response = self._session.request(
                "GET",
                f"{self._base_url}{endpoint}",
                params={"view": view, "source": source, "measure": measure},
                timeout=(self._connect_timeout, self._read_timeout),
            )
            response.raise_for_status()
            payload = response.json()
        except requests.Timeout as exc:
            raise self._error(endpoint, "timeout", started, str(exc)) from exc
        except requests.HTTPError as exc:
            http_status = getattr(exc.response, "status_code", None)
            status = "no_data" if http_status == 404 else "query_failed"
            raise self._error(endpoint, status, started, str(exc), http_status=http_status) from exc
        except (requests.RequestException, TypeError, ValueError) as exc:
            raise self._error(endpoint, "query_failed", started, str(exc)) from exc

        latency_ms = _elapsed_ms(started)
        if not isinstance(payload, Mapping):
            raise CauseBackendError(
                "cause backend returned a non-object response",
                endpoint=endpoint,
                status="query_failed",
                latency_ms=latency_ms,
                http_status=getattr(response, "status_code", None),
            )
        if payload.get("data") is None:
            reason = str(payload.get("reason") or "no_data")
            raise CauseBackendError(
                f"cause backend returned no data: {reason}",
                endpoint=endpoint,
                status="no_data",
                latency_ms=latency_ms,
                http_status=getattr(response, "status_code", None),
                source_epoch=_text(payload.get("source_epoch")),
                built_at=_text(payload.get("built_at")),
                reason=reason,
            )
        trace = CauseBackendTrace(
            endpoint=endpoint,
            status="ok",
            latency_ms=latency_ms,
            http_status=getattr(response, "status_code", None),
            source_epoch=_text(payload.get("source_epoch")),
            built_at=_text(payload.get("built_at")),
        )
        try:
            return parse_cause_market_response(payload, trace=trace)
        except (LookupError, TypeError, ValueError) as exc:
            raise CauseBackendError(
                f"cause backend response contract failed: {exc}",
                endpoint=endpoint,
                status="query_failed",
                latency_ms=latency_ms,
                http_status=getattr(response, "status_code", None),
                source_epoch=trace.source_epoch,
                built_at=trace.built_at,
            ) from exc

    def _cached(self, key: tuple[str, str, str, str]) -> CauseMarket | None:
        now = time.monotonic()
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._cache.pop(key, None)
                return None
            return replace(entry.market, trace=replace(entry.market.trace, cache_hit=True, latency_ms=0.0))

    def _remember(self, key: tuple[str, str, str, str], market: CauseMarket) -> None:
        if self._ttl_seconds <= 0:
            return
        with self._lock:
            if len(self._cache) >= self._max_entries and key not in self._cache:
                oldest = min(self._cache, key=lambda item: self._cache[item].expires_at)
                self._cache.pop(oldest, None)
            self._cache[key] = _CacheEntry(time.monotonic() + self._ttl_seconds, market)

    @staticmethod
    def _error(
        endpoint: str,
        status: str,
        started: float,
        message: str,
        *,
        http_status: int | None = None,
    ) -> CauseBackendError:
        return CauseBackendError(
            f"cause backend {status}: {message}",
            endpoint=endpoint,
            status=status,
            latency_ms=_elapsed_ms(started),
            http_status=http_status,
        )


def parse_cause_market_response(payload: Mapping[str, Any], *, trace: CauseBackendTrace) -> CauseMarket:
    data = _mapping(payload.get("data"), "data")
    kpi = _mapping(data.get("kpi"), "data.kpi")
    meta = _mapping(payload.get("market_meta"), "market_meta")
    brand = _text(payload.get("brand_name") or payload.get("brand") or kpi.get("target_brand"))
    market_name = _text(
        meta.get("market_definition_label")
        or meta.get("market_label_kor")
        or meta.get("market_name_short")
        or meta.get("market_name")
    )
    if not brand or not market_name:
        raise LookupError("brand or market label is missing")
    market_size = _number(kpi.get("market_size_recent"))
    if market_size is None:
        raise LookupError("market_size_recent is missing")
    market_series = _market_series(data)
    period = _latest_period(market_series)
    trend_periods, brand_rows = _brand_rows(data)
    if not period and trend_periods:
        period = trend_periods[-1]
    if not period:
        raise LookupError("latest period is missing")
    source_epoch = _text(payload.get("source_epoch") or data.get("source_epoch"))
    built_at = _text(payload.get("built_at") or data.get("built_at"))
    resolved_trace = replace(
        trace,
        source_epoch=source_epoch or trace.source_epoch,
        built_at=built_at or trace.built_at,
    )
    return CauseMarket(
        brand=brand,
        market_name=market_name,
        source=_source_label(_text(payload.get("source")) or "UBIST"),
        measure=_text(payload.get("measure")) or "sales",
        period=period,
        market_size=market_size,
        market_cagr_pct=_number(kpi.get("market_cagr_5y_pct")),
        top3_share_pct=_number(kpi.get("top3_share_pct")),
        hhi_recent=_number(kpi.get("hhi_recent")),
        direct_competition_count=_integer(kpi.get("direct_competition_count")),
        brand_value=_number(kpi.get("brand_value_recent")),
        brand_share_pct=_first_number(kpi, "target_share_pct", "brand_share_pct"),
        brand_rank=_integer(kpi.get("target_rank")),
        brand_cagr_pct=_number(kpi.get("brand_cagr_pct")),
        market_series=tuple(market_series),
        hhi_series=tuple(_hhi_series(data)),
        brand_rows=tuple(brand_rows),
        trace=resolved_trace,
    )


def _brand_rows(data: Mapping[str, Any]) -> tuple[tuple[str, ...], list[CauseBrandRow]]:
    trend = data.get("level_top5_trend")
    by_level = trend.get("by_level") if isinstance(trend, Mapping) else None
    brand_level = by_level.get("Brand") if isinstance(by_level, Mapping) else None
    periods = tuple(str(item) for item in brand_level.get("periods_10pt", ()) if str(item)) if isinstance(brand_level, Mapping) else ()
    values = brand_level.get("values") if isinstance(brand_level, Mapping) else None
    overall = next(
        (item for item in values if isinstance(item, Mapping) and item.get("is_overall")),
        None,
    ) if isinstance(values, list) else None
    items = overall.get("brands_in_value") if isinstance(overall, Mapping) else None
    rows: list[CauseBrandRow] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, Mapping) or item.get("is_others"):
                continue
            brand = _text(item.get("brand"))
            if not brand:
                continue
            values_10pt = _numeric_tuple(item.get("value_series_10pt"), len(periods))
            shares_10pt = _numeric_tuple(item.get("ms_series_10pt"), len(periods))
            ranks_10pt = _integer_tuple(item.get("rank_series_10pt"), len(periods))
            rows.append(
                CauseBrandRow(
                    brand=brand,
                    rank=_integer(item.get("rank")),
                    value=_first_number(item, "value_recent", "raw_value"),
                    share_pct=_number(item.get("ms_recent_pct")),
                    company=_text(item.get("company")),
                    is_jw=bool(item.get("is_jw")),
                    periods=periods,
                    values=values_10pt,
                    shares=shares_10pt,
                    ranks=ranks_10pt,
                )
            )
    matrix = data.get("ei_ms_matrix")
    matrix_rows = matrix.get("data") if isinstance(matrix, Mapping) else None
    if isinstance(matrix_rows, list):
        known_brands = {row.brand for row in rows}
        for item in matrix_rows:
            if not isinstance(item, Mapping) or item.get("is_others"):
                continue
            brand = _text(item.get("brand"))
            if not brand or brand in known_brands:
                continue
            rows.append(
                CauseBrandRow(
                    brand=brand,
                    rank=_integer(item.get("rank")),
                    value=_first_number(item, "value_recent", "raw_value"),
                    share_pct=_first_number(item, "share_pct", "ms_recent_pct"),
                    company=_text(item.get("company")),
                    is_jw=bool(item.get("is_jw")),
                    periods=(),
                    values=(),
                    shares=(),
                    ranks=(),
                )
            )
            known_brands.add(brand)
    if not rows:
        raise LookupError("brand rows are missing")
    return periods, rows


def _market_series(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    sources_data = data.get("sources_data")
    raw = sources_data.get("market_size_series") if isinstance(sources_data, Mapping) else None
    rows: list[dict[str, Any]] = []
    if isinstance(raw, Mapping):
        iterable = ({"period": period, **item} for period, item in raw.items() if isinstance(item, Mapping))
    elif isinstance(raw, list):
        iterable = (item for item in raw if isinstance(item, Mapping))
    else:
        iterable = ()
    for item in iterable:
        period = _text(item.get("period"))
        value = _first_number(item, "value", "value_krw")
        if not period or value is None:
            continue
        rows.append(
            {
                "period": period,
                "value": value,
                "value_krw": value,
                "value_억원": _eok(value),
                "yoy_growth_pct": _number(item.get("yoy_growth_pct")),
            }
        )
    rows.sort(key=lambda item: str(item["period"]))
    return rows


def _hhi_series(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = data.get("hhi_series_5y")
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        period = _text(item.get("period") or item.get("period_full") or item.get("year"))
        hhi = _number(item.get("hhi"))
        if not period or hhi is None:
            continue
        rows.append({"period": period, "period_full": _text(item.get("period_full")) or period, "year": _integer(item.get("year")), "hhi": hhi})
    return rows


def _target_series(rows: tuple[CauseBrandRow, ...], brand: str) -> list[dict[str, Any]]:
    target = next((row for row in rows if row.brand == brand), None)
    if target is None:
        return []
    return target.trend()["series"]


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LookupError(f"{name} is missing")
    return value


def _latest_period(rows: list[dict[str, Any]]) -> str:
    return str(rows[-1]["period"]) if rows else ""


def _api_source(source: str) -> str:
    return "IQVIA" if str(source).casefold().startswith("iqvia") else "UBIST"


def _source_label(source: str) -> str:
    return _api_source(source)


def _text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _first_number(items: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _number(items.get(key))
        if value is not None:
            return value
    return None


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def _numeric_tuple(value: object, size: int) -> tuple[float | None, ...]:
    items = value if isinstance(value, list) else []
    out = tuple(_number(item) for item in items[:size])
    return out + (None,) * max(0, size - len(out))


def _integer_tuple(value: object, size: int) -> tuple[int | None, ...]:
    items = value if isinstance(value, list) else []
    out = tuple(_integer(item) for item in items[:size])
    return out + (None,) * max(0, size - len(out))


def _eok(value: object) -> float | None:
    numeric = _number(value)
    return round(numeric / 100_000_000, 4) if numeric is not None else None


def _difference(left: object, right: object) -> float | None:
    left_number = _number(left)
    right_number = _number(right)
    if left_number is None or right_number is None:
        return None
    return round(left_number - right_number, 4)


def _elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000.0, 3)
