"""Brand Activity — IQVIA CSD keyword INTEREST 3-category monthly time series.

New brand-activity sub-EP: for the resolved brand set (selected + 5 competitors), return the
INTEREST 3-category (VERY USEFUL / SOMEWHAT USEFUL / NOT AT ALL) monthly series over a fixed
3-year window anchored on the keyword source's latest month. Percentages use the within-brand,
within-month denominator (the 3-category count sum), exposed as ``total_count``.

The request carries NO period parameter — the 3-year window is always returned in full and the
front end slices it. Data-changing inputs are brand + view + market option + visit_location +
specialty only. Reuses the existing brand-activity source/contract building blocks; the singular
resolve_csd_market / interest-rx / topics EPs are untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from pipeline.scripts.analysis.brand_activity.alias.normalize import normalize_iqvia_en
from pipeline.scripts.api import db
from pipeline.scripts.api.brand_activity_brand_resolver import BrandSetInputError, BrandSetResolution, resolve_brand_set
from pipeline.scripts.api.brand_activity_csd_shared import BrandChoice, BrandMeta, JsonMap, float_value, text
from pipeline.scripts.api.brand_activity_interest_rx_config import INTEREST_LEVELS
from pipeline.scripts.api.brand_activity_csd_presence import iqvia_product_codes_by_brand
from pipeline.scripts.api.brand_activity_interest_rx_source import _market_clause
from pipeline.scripts.api.brand_activity_topic_matrix import _alias_lookup, _keyword_filter_domain
from pipeline.scripts.api.brand_activity_interest_rx_matrix import _canonical_product
from pipeline.scripts.api.config import config
from pipeline.scripts.api.dynamic_market.types import quote_identifier


def _period_minus_months(period: str, months: int) -> str:
    """Shift a YYYY-MM period by ``months`` (negative to go forward). Pure integer math.

    Defined locally on purpose: the equivalent services._period_minus_months lives in a module
    that does a top-level ``import pyarrow`` (an ETL-only dep absent from the API image), so
    importing it crashes the API container at startup. Keeping this off the pyarrow import path
    mirrors commit e448d3b2 (which moved the market-status recent-period helper out of services).
    """

    year, month = (int(part) for part in period.split("-", 1))
    index = year * 12 + month - 1 - months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"

# 3-year window = 36 monthly points inclusive of the anchor month.
_WINDOW_MONTHS = 36
_FILTER_COLUMNS = ("visit_location", "specialty")


class InterestTimeseriesInputError(RuntimeError):
    """Raised when an interest time-series request cannot be parsed or validated."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class TimeseriesRequest:
    view: str
    market_id: str | None
    selected_brand: str
    filter_payload: JsonMap
    visit_locations: tuple[str, ...]
    specialties: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PeriodWindow:
    start: str
    end: str
    available_start: str
    available_end: str
    months: tuple[str, ...]
    full_window: bool


def get_interest_timeseries(payload: Mapping[str, Any]) -> JsonMap | None:
    """Return per-brand INTEREST 3-category monthly count + within-brand pct over a fixed 3y window."""

    request = _parse_request(payload)
    _validate_filter_domains(request)
    period = _period_window()
    try:
        brand_set = resolve_brand_set(
            view_name=request.view,
            market_id=request.market_id,
            selected_brand=request.selected_brand,
            filter_payload=request.filter_payload,
            prefilter_strategic_choices=True,
        )
    except BrandSetInputError as exc:
        raise InterestTimeseriesInputError(str(exc)) from exc
    if brand_set is None:
        return None

    aliases = _alias_lookup()
    # Keyword product_name is IQVIA-coded; strategic brand_meta carries UBIST codes, so reload
    # IQVIA codes by brand name (same reload as the CSD path) to make the brand join work across
    # every view. raw codes drive the SQL IN filter; canonicalized codes drive per-brand matching.
    iqvia = iqvia_product_codes_by_brand({key: meta.brand_name for key, meta in brand_set.brand_meta.items()})
    raw_codes = tuple(sorted({code for codes in iqvia.values() for code in codes}))
    code_sets = {key: frozenset(_canonical_product(code, aliases) for code in codes) for key, codes in iqvia.items()}
    rows = _fetch_rows(brand_set, request, period, raw_codes)
    counts = _counts_by_brand_month(brand_set, rows, code_sets, aliases)
    company_counts, company_totals = _counts_by_company_month(brand_set, rows, code_sets, aliases)
    # Deterministic order: keyword row count desc, then company name asc.
    ordered_companies = sorted(company_counts, key=lambda name: (-company_totals[name], name))
    return {
        "scope": _scope_payload(request, brand_set),
        "filters_applied": {
            "visit_location": list(request.visit_locations) or ["전체"],
            "specialty": list(request.specialties) or ["전체"],
        },
        "period": _period_payload(period),
        "levels": list(INTEREST_LEVELS),
        "brands": [_brand_payload(choice, brand_set, counts, period) for choice in brand_set.choices],
        "companies": [_company_payload(name, company_counts[name], period) for name in ordered_companies],
    }


def _parse_request(payload: Mapping[str, Any]) -> TimeseriesRequest:
    view = text(payload.get("view"))
    if view not in {"general", "strategic_ml", "strategic_cd"}:
        raise InterestTimeseriesInputError(f"unsupported view: {view}")
    selected_brand = text(payload.get("selected_brand"))
    filter_payload = _filter_payload(payload)
    market_id = (_first_filter_value(filter_payload, "atc4") or None) if view == "general" else (text(payload.get("market_id")) or None)
    if not selected_brand or (view == "general" and not market_id and not _has_market_scope(filter_payload)):
        raise InterestTimeseriesInputError("filters.atc4 and selected_brand are required")
    return TimeseriesRequest(
        view=view,
        market_id=market_id,
        selected_brand=selected_brand,
        filter_payload=filter_payload,
        visit_locations=_filter_tuple(payload.get("visit_location")),
        specialties=_filter_tuple(payload.get("specialty")),
    )


def _validate_filter_domains(request: TimeseriesRequest) -> None:
    """Reject unknown visit_location/specialty values against the live keyword domain (422)."""

    for column, requested in (("visit_location", request.visit_locations), ("specialty", request.specialties)):
        if not requested:
            continue
        allowed = _keyword_filter_domain(column)
        unknown = tuple(value for value in requested if value not in allowed)
        if unknown:
            raise InterestTimeseriesInputError(
                f"unsupported {column}: {', '.join(unknown)}", status_code=422
            )


def _period_window() -> PeriodWindow:
    """Fixed 3-year window anchored on the keyword source's latest month (deterministic; no today())."""

    row = db.fetch_one(
        f"""
        SELECT MIN(period_ym) AS available_start, MAX(period_ym) AS available_end
        FROM {quote_identifier(config.brand_activity_db_name)}.`km_keyword_event_stage`
        """
    ) or {}
    available_start = text(row.get("available_start"))
    available_end = text(row.get("available_end"))
    if not available_start or not available_end:
        raise InterestTimeseriesInputError("keyword period bounds unavailable")
    # NOTE: this mirrors the latest-month anchor of default_topic_period(bounds, lookback)
    # (commit 80a03904, not yet on develop). When 80a03904 lands on develop, replace this block
    # with default_topic_period(bounds, lookback=36) to share one anchor helper (no copy).
    window_start = _period_minus_months(available_end, _WINDOW_MONTHS - 1)
    start = max(window_start, available_start)
    full_window = window_start >= available_start
    months = tuple(_month_range(start, available_end))
    return PeriodWindow(start, available_end, available_start, available_end, months, full_window)


def _month_range(start: str, end: str) -> list[str]:
    months: list[str] = []
    cursor = start
    while cursor <= end:
        months.append(cursor)
        cursor = _period_minus_months(cursor, -1)
    return months


def _fetch_rows(brand_set: BrandSetResolution, request: TimeseriesRequest, period: PeriodWindow, product_codes: tuple[str, ...]) -> list[JsonMap]:
    market_clause, market_params = _market_clause(brand_set.view, brand_set.market_id, product_codes)
    clauses = ["period_ym BETWEEN %s AND %s", market_clause]
    params: list[Any] = [period.start, period.end, *market_params]
    for column, values in (("visit_location", request.visit_locations), ("specialty", request.specialties)):
        if values:
            placeholders = ", ".join(["%s"] * len(values))
            clauses.append(f"{column} IN ({placeholders})")
            params.extend(values)
    return db.fetch_all(
        f"""
        SELECT product_name, representing_company, period_ym, interest, COUNT(*) AS event_count
        FROM {quote_identifier(config.brand_activity_db_name)}.`km_keyword_event_stage`
        WHERE {" AND ".join(clauses)}
        GROUP BY product_name, representing_company, period_ym, interest
        """,
        tuple(params),
    )


def _counts_by_brand_month(
    brand_set: BrandSetResolution,
    rows: list[JsonMap],
    code_sets: dict[str, frozenset[str]],
    aliases: dict[str, str],
) -> dict[str, dict[str, dict[str, int]]]:
    """brand_key -> month -> interest level -> count (levels not seen stay absent until finalized)."""

    result: dict[str, dict[str, dict[str, int]]] = {choice.brand_key: {} for choice in brand_set.choices}
    for row in rows:
        level = text(row.get("interest"))
        if level not in INTEREST_LEVELS:
            continue
        product = _canonical_product(text(row.get("product_name")), aliases)
        month = text(row.get("period_ym"))
        value = int(float_value(row.get("event_count")))
        for choice in brand_set.choices:
            if product in code_sets.get(choice.brand_key, set()):
                month_counts = result[choice.brand_key].setdefault(month, {})
                month_counts[level] = month_counts.get(level, 0) + value
                break
    return result


def _series_for_counts(by_month: dict[str, dict[str, int]], period: PeriodWindow) -> dict[str, JsonMap | None]:
    """Build the 3-category monthly series shared by brand and company payloads.

    Denominator = within-series within-month 3-category count sum (``total_count``). One
    final round per level (no forced 100 correction). Missing month = null (not zero-filled).
    """

    series: dict[str, JsonMap | None] = {}
    for month in period.months:
        month_counts = by_month.get(month)
        total = sum(month_counts.values()) if month_counts else 0
        if not total:
            series[month] = None
            continue
        series[month] = {
            "total_count": total,
            **{
                level: {
                    "count": month_counts.get(level, 0),
                    "pct": round(month_counts.get(level, 0) / total * 100, 1),
                }
                for level in INTEREST_LEVELS
            },
        }
    return series


def _brand_payload(
    choice: BrandChoice,
    brand_set: BrandSetResolution,
    counts: dict[str, dict[str, dict[str, int]]],
    period: PeriodWindow,
) -> JsonMap:
    meta = brand_set.brand_meta.get(choice.brand_key, BrandMeta(choice.brand_key, choice.brand_name, (), False))
    return {
        "brand_key": choice.brand_key,
        "brand_name": meta.brand_name or choice.brand_name,
        "is_selected": choice.is_selected,
        "is_jw": meta.is_jw,
        "sales_rank": choice.sales_rank,
        "series": _series_for_counts(counts.get(choice.brand_key, {}), period),
    }


def _counts_by_company_month(
    brand_set: BrandSetResolution,
    rows: list[JsonMap],
    code_sets: dict[str, frozenset[str]],
    aliases: dict[str, str],
) -> tuple[dict[str, dict[str, dict[str, int]]], dict[str, int]]:
    """representing_company -> month -> interest -> count, plus per-company total row count.

    Companies come from the resolved brand set's keyword rows only (a row counts when its
    canonical product belongs to any of the brands). Co-promotion keeps every company: a
    product's rows split across companies are attributed to each. filters flow through the
    same fetched rows, so a company's denominator moves with the filter (brand rule parity).
    """

    brand_products: frozenset[str] = frozenset().union(*code_sets.values()) if code_sets else frozenset()
    result: dict[str, dict[str, dict[str, int]]] = {}
    totals: dict[str, int] = {}
    for row in rows:
        level = text(row.get("interest"))
        if level not in INTEREST_LEVELS:
            continue
        product = _canonical_product(text(row.get("product_name")), aliases)
        if product not in brand_products:
            continue
        company = text(row.get("representing_company")).strip()
        if not company:
            continue
        month = text(row.get("period_ym"))
        value = int(float_value(row.get("event_count")))
        month_counts = result.setdefault(company, {}).setdefault(month, {})
        month_counts[level] = month_counts.get(level, 0) + value
        totals[company] = totals.get(company, 0) + value
    return result, totals


def _company_payload(company_name: str, by_month: dict[str, dict[str, int]], period: PeriodWindow) -> JsonMap:
    return {
        "company_name": company_name,
        "series": _series_for_counts(by_month, period),
    }


def _scope_payload(request: TimeseriesRequest, brand_set: BrandSetResolution) -> JsonMap:
    market_name = str(brand_set.market_row.get(brand_set.view.market_name_column) or brand_set.market_id)
    return {
        "view": request.view,
        "market_id": brand_set.market_id,
        "market_name": market_name,
        "selected_brand": request.selected_brand,
        "ranking_quarter": brand_set.ranking_quarter,
        "applied_filter": brand_set.applied_filter,
        "applied_filters": brand_set.applied_filter,
        "resolved_market": {
            "type": request.view,
            "market_id": brand_set.market_id,
            "market_label": market_name,
            "source": "filters" if request.view == "general" else f"brand:{request.selected_brand}",
        },
    }


def _period_payload(period: PeriodWindow) -> JsonMap:
    return {
        "start": period.start,
        "end": period.end,
        "available_start": period.available_start,
        "available_end": period.available_end,
        "full_window": period.full_window,
        "window_months": _WINDOW_MONTHS,
        "months": list(period.months),
    }


def _filter_tuple(value: Any) -> tuple[str, ...]:
    """Normalize a visit_location/specialty shortcut into concrete values ('전체'/blank -> all)."""

    values = value if isinstance(value, list | tuple) else [value]
    collected: list[str] = []
    for item in values:
        cleaned = text(item).strip()
        if not cleaned or cleaned == "전체":
            return ()
        if cleaned not in collected:
            collected.append(cleaned)
    return tuple(collected)


def _filter_payload(payload: Mapping[str, Any]) -> JsonMap:
    filters = payload.get("filters")
    legacy_filter = payload.get("filter")
    if isinstance(filters, dict) and filters:
        return filters
    return legacy_filter if isinstance(legacy_filter, dict) else {}


def _first_filter_value(filter_payload: Mapping[str, Any], key: str) -> str:
    value = filter_payload.get(key)
    if isinstance(value, list):
        return text(value[0]) if value else ""
    return text(value)


def _has_market_scope(filter_payload: Mapping[str, Any]) -> bool:
    return isinstance(filter_payload.get("market_scope"), Mapping)
