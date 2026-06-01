from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
import pyarrow.parquet as pq

from pipeline.scripts.api import db
from pipeline.scripts.api.catalog import (
    DISPLAY_BRANDS,
    DisplayBrand,
    get_display_brand,
    validate_source_measure,
)
from pipeline.scripts.api.drivers import compute_drivers
from pipeline.scripts.api.market_id import to_ml_id, to_strategy_id
from pipeline.scripts.api.metadata import BRAND_METADATA
from pipeline.scripts.api.utils import loads_json_maybe, now_iso, to_jsonable


FORM_BOUNDARY = re.compile(r"(?:$|\\s|정|캡슐|주|액|서방|시럽|현탁|구강|SR|CR|OD)", re.IGNORECASE)
DEFAULT_MARKET_STATUS_TOP_N = 20
MAX_MARKET_STATUS_TOP_N = 100
PROJECT_ROOT = Path(__file__).resolve().parents[3]
ML_MARKET_CATALOG_PATH = PROJECT_ROOT / "output" / "catalog" / "ml_market" / "ml_market.parquet"


MARKET_STATUS_COMPANY_BY_BRAND: dict[str, str] = {
    "라베칸": "녹십자",
    "라베칸듀오": "JW중외제약",
    "제이클": "한미약품",
    "가드렛": "엘지화학",
    "가드메트": "유한양행",
    "타발리스": "유한양행",
    "시그마트": "대웅제약",
    "리바로": "일동제약",
    "리바로젯": "종근당",
    "리바로페노": "종근당",
    "리바로하이": "동아에스티",
    "리바로브이": "유한양행",
    "트루패스": "엘지화학",
    "피나스타": "셀트리온제약",
    "제이다트": "한미약품",
    "뉴트로진": "한독",
    "모빌리아": "한미약품",
    "악템라": "한미약품",
    "페린젝트": "일동제약",
    "베노훼럼": "한미약품",
    "헴리브라": "대웅제약",
    "위너프": "한미약품",
    "위너프A+": "한독",
    "엔커버": "삼성바이오에피스",
    "플라주오피": "한독",
}


@dataclass(frozen=True)
class BrandResolution:
    display: DisplayBrand
    brand_id: str
    brand_name: str
    period_yyyymm: str
    snapshot: dict[str, Any]


def latest_period() -> str:
    row = db.fetch_one("SELECT MAX(period_yyyymm) AS period FROM mart_core_brand_metric")
    if not row or not row["period"]:
        raise RuntimeError("mart_core_brand_metric has no rows")
    return str(row["period"])


def _brand_match(display_name: str, stored_name: str) -> bool:
    display = display_name.replace(" ", "").lower()
    stored = stored_name.replace(" ", "").lower()
    if stored == display:
        return True
    if not stored.startswith(display):
        return False
    remainder = stored[len(display) :]
    if not remainder:
        return True
    return bool(FORM_BOUNDARY.match(remainder))


def resolve_brand(brand_name: str) -> BrandResolution:
    display = get_display_brand(brand_name)
    if not display:
        raise HTTPException(status_code=404, detail=f"Brand not found: {brand_name}")

    rows = db.fetch_all(
        """
        SELECT brand_id, brand_name, period_yyyymm, market_share, rank_in_market,
               cagr_1y, cagr_3y, cagr_5y, ei_5y, momentum_score,
               growth_contribution, hhi, market_cagr_5y, raw_value
        FROM mart_core_brand_metric
        WHERE ml_id = %s
          AND is_jw = TRUE
          AND channel IS NULL
          AND specialty IS NULL
          AND period_yyyymm = (
            SELECT MAX(period_yyyymm)
            FROM mart_core_brand_metric
            WHERE ml_id = %s AND is_jw = TRUE AND channel IS NULL AND specialty IS NULL
          )
        ORDER BY raw_value DESC
        """,
        (display.ml_id, display.ml_id),
    )
    match_names = (display.brand_name, *display.layer3_aliases)
    candidates = [
        row
        for row in rows
        if any(_brand_match(match_name, str(row["brand_name"])) for match_name in match_names)
    ]
    if not candidates:
        candidates = [row for row in rows if str(row["brand_name"]).replace(" ", "").lower().startswith(display.brand_name.replace(" ", "").lower())]
    if not candidates:
        raise HTTPException(status_code=404, detail=f"Layer 3 brand row not found: {brand_name}")

    chosen = to_jsonable(candidates[0])
    return BrandResolution(
        display=display,
        brand_id=str(chosen["brand_id"]),
        brand_name=str(chosen["brand_name"]),
        period_yyyymm=str(chosen["period_yyyymm"]),
        snapshot=chosen,
    )


def latest_period_for_brand(brand_id: str) -> str:
    row = db.fetch_one(
        """
        SELECT MAX(period_yyyymm) AS period
        FROM mart_core_brand_metric
        WHERE brand_id = %s AND channel IS NULL AND specialty IS NULL
        """,
        (brand_id,),
    )
    if not row or not row["period"]:
        raise HTTPException(status_code=404, detail=f"No Layer 3 rows for brand_id={brand_id}")
    return str(row["period"])


def normalize_market_status_top_n(top_n: int | None) -> int:
    if top_n is None:
        return DEFAULT_MARKET_STATUS_TOP_N
    return max(1, min(int(top_n), MAX_MARKET_STATUS_TOP_N))


def build_brands_response(
    q: str | None = None,
    market_id: str | None = None,
    include_snapshot: bool = False,
) -> list[dict[str, Any]]:
    del include_snapshot  # Kept for old callers; v0.9.0 brands is catalog metadata only.
    normalized_market_id = to_strategy_id(market_id) if market_id else None
    query = q.casefold() if q else None

    data = [brand.to_response() for brand in BRAND_METADATA]
    if query:
        data = [brand for brand in data if query in str(brand["brand"]).casefold()]
    if normalized_market_id:
        data = [brand for brand in data if brand["market_id"] == normalized_market_id]
    return data


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _round_or_none(value: float | None, ndigits: int = 2) -> float | None:
    if value is None:
        return None
    return round(value, ndigits)


def _pct(value: Any, ndigits: int = 2) -> float | None:
    concrete = _float_or_none(value)
    if concrete is None:
        return None
    return round(concrete * 100, ndigits)


def _growth_pct(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return round((current / previous - 1) * 100, 2)


def _period_minus_months(period: str, months: int) -> str:
    year, month = (int(part) for part in period.split("-", 1))
    index = year * 12 + month - 1 - months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _period_range(start: str, end: str) -> list[str]:
    periods: list[str] = []
    cursor = start
    while cursor <= end:
        periods.append(cursor)
        cursor = _period_minus_months(cursor, -1)
    return periods


def _format_quarter(period: str | None) -> str | None:
    if not period:
        return None
    year, month = (int(part) for part in period.split("-", 1))
    return f"{year}-Q{((month - 1) // 3) + 1}"


def _sum_raw_value_for_periods(brand_id: str, periods: list[str]) -> float | None:
    if not periods:
        return None
    placeholders = ", ".join(["%s"] * len(periods))
    row = db.fetch_one(
        f"""
        SELECT SUM(raw_value) AS total_value
        FROM mart_core_brand_metric
        WHERE brand_id = %s
          AND period_yyyymm IN ({placeholders})
          AND channel_norm = '__ALL__'
          AND specialty_norm = '__ALL__'
        """,
        (brand_id, *periods),
    )
    return _float_or_none(row["total_value"]) if row else None


def _mat_growth_pct(brand_id: str, latest_period: str) -> float | None:
    current_start = _period_minus_months(latest_period, 11)
    previous_start = _period_minus_months(latest_period, 23)
    previous_end = _period_minus_months(latest_period, 12)
    current_total = _sum_raw_value_for_periods(brand_id, _period_range(current_start, latest_period))
    previous_total = _sum_raw_value_for_periods(brand_id, _period_range(previous_start, previous_end))
    return _growth_pct(current_total, previous_total)


def _ym_growth_pct(brand_id: str, latest_period: str) -> float | None:
    year, month = latest_period.split("-", 1)
    current_start = f"{year}-01"
    previous_start = f"{int(year) - 1:04d}-01"
    previous_end = f"{int(year) - 1:04d}-{month}"
    current_total = _sum_raw_value_for_periods(brand_id, _period_range(current_start, latest_period))
    previous_total = _sum_raw_value_for_periods(brand_id, _period_range(previous_start, previous_end))
    return _growth_pct(current_total, previous_total)


def _ms_change_yoy_pct(brand_id: str, latest_period: str, current_market_share: Any) -> float | None:
    current_ms = _float_or_none(current_market_share)
    if current_ms is None:
        return None
    previous_period = _period_minus_months(latest_period, 12)
    row = db.fetch_one(
        """
        SELECT market_share
        FROM mart_core_brand_metric
        WHERE brand_id = %s
          AND period_yyyymm = %s
          AND channel_norm = '__ALL__'
          AND specialty_norm = '__ALL__'
        """,
        (brand_id, previous_period),
    )
    if not row or row["market_share"] is None:
        return None
    return round((current_ms - float(row["market_share"])) * 100, 2)


def _first_period_snapshot(brand_id: str, latest_period: str) -> dict[str, Any] | None:
    five_year_start = _period_minus_months(latest_period, 60)
    row = db.fetch_one(
        """
        SELECT period_yyyymm, raw_value, market_share
        FROM mart_core_brand_metric
        WHERE brand_id = %s
          AND period_yyyymm >= %s
          AND period_yyyymm <= %s
          AND channel_norm = '__ALL__'
          AND specialty_norm = '__ALL__'
        ORDER BY period_yyyymm
        LIMIT 1
        """,
        (brand_id, five_year_start, latest_period),
    )
    if row:
        return to_jsonable(row)
    row = db.fetch_one(
        """
        SELECT period_yyyymm, raw_value, market_share
        FROM mart_core_brand_metric
        WHERE brand_id = %s
          AND period_yyyymm <= %s
          AND channel_norm = '__ALL__'
          AND specialty_norm = '__ALL__'
        ORDER BY period_yyyymm
        LIMIT 1
        """,
        (brand_id, latest_period),
    )
    return to_jsonable(row) if row else None


def _market_context_snapshot(ml_id: str, period: str, cache: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    key = (ml_id, period)
    if key not in cache:
        row = db.fetch_one(
            """
            SELECT SUM(raw_value) AS market_size_recent,
                   COUNT(*) AS direct_competition_count
            FROM mart_core_brand_metric
            WHERE ml_id = %s
              AND period_yyyymm = %s
              AND channel_norm = '__ALL__'
              AND specialty_norm = '__ALL__'
            """,
            (ml_id, period),
        )
        cache[key] = to_jsonable(row or {})
    return cache[key]


def _default_source(sources: list[str]) -> str:
    if "IQVIA" in sources:
        return "IQVIA"
    return sources[0] if sources else "UBIST"


def _source_metrics(front: dict[str, Any], sources: list[str]) -> dict[str, dict[str, Any]]:
    source_metric = {
        "value_recent": front["value_recent"],
        "ms_recent_pct": front["ms_recent_pct"],
        "gr_mom_pct": front["gr_mom_pct"],
        "gr_qoq_pct": front["gr_qoq_pct"],
        "gr_yoy_pct": front["gr_yoy_pct"],
        "gr_yoy_mat_pct": front["gr_yoy_mat_pct"],
        "gr_yoy_ym_pct": front["gr_yoy_ym_pct"],
    }
    return {source: dict(source_metric) for source in sources}


def _empty_front(sources: list[str]) -> dict[str, Any]:
    front = {
        "value_recent": None,
        "ms_recent_pct": None,
        "gr_qoq_pct": None,
        "gr_yoy_pct": None,
        "ms_change_yoy_pct": None,
        "gr_mom_pct": None,
        "gr_yoy_mat_pct": None,
        "gr_yoy_ym_pct": None,
    }
    return {**front, "sources_data": _source_metrics(front, sources), "default_source": _default_source(sources)}


def _market_definition_label(atc_codes: list[str]) -> str:
    return "1 ATC" if len(atc_codes) == 1 else f"{len(atc_codes)} ATC 통합"


def _parse_atc_codes(raw_value: Any) -> list[str]:
    raw_codes = loads_json_maybe(raw_value)
    if not isinstance(raw_codes, list):
        return []
    return [str(code).strip() for code in raw_codes if str(code).strip()]


@lru_cache(maxsize=1)
def _ml_market_atc_codes() -> dict[str, list[str]]:
    if not ML_MARKET_CATALOG_PATH.exists():
        return {}
    try:
        table = pq.read_table(ML_MARKET_CATALOG_PATH, columns=["ml_id", "atc_codes_json"])
    except Exception:
        return {}
    return {
        str(row["ml_id"]): _parse_atc_codes(row.get("atc_codes_json"))
        for row in table.to_pylist()
    }


def _catalog_atc_codes_for_ml(ml_id: str) -> list[str]:
    return list(_ml_market_atc_codes().get(ml_id, []))


def _build_market_status_card(
    meta: Any,
    *,
    market_context_cache: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    ml_id = to_ml_id(meta.market_id)
    sources = list(meta.sources)
    atc_codes = _catalog_atc_codes_for_ml(ml_id)
    try:
        resolved = resolve_brand(meta.brand)
        snapshot = resolved.snapshot
        latest = resolved.period_yyyymm
    except HTTPException:
        resolved = None
        snapshot = {}
        latest = latest_period()

    front = _empty_front(sources)
    back = {
        "cagr_5y_pct": None,
        "sales_first_period_krw": None,
        "ms_first_period_pct": None,
        "period_first": None,
    }
    back_extended = {
        "market_size_recent": None,
        "market_cagr_5y_pct": None,
        "brand_cagr_5y_pct": None,
        "excess_growth_pct": None,
        "source_label": _default_source(sources),
        "is_dual_source": bool(meta.is_dual_source),
        "sources": sources,
        "market_definition_label": _market_definition_label(atc_codes),
        "market_definition_full": f"{meta.market_name} 경쟁 시장 ({', '.join(atc_codes)})",
        "atc_count": len(atc_codes),
        "direct_competition_count": None,
        "market_label_kor": meta.market_label_kor,
    }

    if resolved:
        mat_growth = _mat_growth_pct(resolved.brand_id, latest)
        ym_growth = _ym_growth_pct(resolved.brand_id, latest)
        front = {
            "value_recent": _round_or_none(_float_or_none(snapshot.get("raw_value")), 2),
            "ms_recent_pct": _pct(snapshot.get("market_share")),
            "gr_qoq_pct": _pct(snapshot.get("qoq")),
            "gr_yoy_pct": _pct(snapshot.get("yoy")),
            "ms_change_yoy_pct": _ms_change_yoy_pct(resolved.brand_id, latest, snapshot.get("market_share")),
            "gr_mom_pct": _pct(snapshot.get("mom")),
            "gr_yoy_mat_pct": mat_growth,
            "gr_yoy_ym_pct": ym_growth,
        }
        front["sources_data"] = _source_metrics(front, sources)
        front["default_source"] = _default_source(sources)

        first = _first_period_snapshot(resolved.brand_id, latest)
        if first:
            back = {
                "cagr_5y_pct": _pct(snapshot.get("cagr_5y")),
                "sales_first_period_krw": _round_or_none(_float_or_none(first.get("raw_value")), 2),
                "ms_first_period_pct": _pct(first.get("market_share")),
                "period_first": _format_quarter(str(first.get("period_yyyymm"))),
            }

        market_context = _market_context_snapshot(ml_id, latest, market_context_cache)
        brand_cagr = _float_or_none(snapshot.get("cagr_5y"))
        market_cagr = _float_or_none(snapshot.get("market_cagr_5y"))
        back_extended.update(
            {
                "market_size_recent": _round_or_none(_float_or_none(market_context.get("market_size_recent")), 2),
                "market_cagr_5y_pct": _pct(market_cagr),
                "brand_cagr_5y_pct": _pct(brand_cagr),
                "excess_growth_pct": _round_or_none((brand_cagr - market_cagr) * 100, 2)
                if brand_cagr is not None and market_cagr is not None
                else None,
                "direct_competition_count": int(market_context["direct_competition_count"])
                if market_context.get("direct_competition_count") is not None
                else None,
            }
        )

    return {
        "rank": int(meta.rank),
        "brand": meta.brand,
        "company": MARKET_STATUS_COMPANY_BY_BRAND.get(meta.brand, "JW중외제약"),
        "is_jw": bool(meta.is_jw),
        "is_target": bool(meta.is_target),
        "market_id": to_strategy_id(meta.market_id),
        "market_name": meta.market_name,
        "market_name_short": meta.market_name_short,
        "market_label_kor": meta.market_label_kor,
        "mkt_team": meta.mkt_team,
        "atc_codes": atc_codes,
        "atc_desc": meta.atc_desc,
        "sources": sources,
        "nhi_type": "NHI",
        "front": front,
        "back": back,
        "back_extended": back_extended,
    }


def filter_market_status_cards(cards: list[dict[str, Any]], market_id: str | None = None) -> list[dict[str, Any]]:
    if not market_id:
        return cards
    normalized_market_id = to_strategy_id(market_id)
    return [card for card in cards if card["market_id"] == normalized_market_id]


def build_market_status_cards(market_id: str | None = None) -> list[dict[str, Any]]:
    market_context_cache: dict[tuple[str, str], dict[str, Any]] = {}
    cards = [
        _build_market_status_card(meta, market_context_cache=market_context_cache)
        for meta in sorted(BRAND_METADATA, key=lambda item: item.rank)
    ]
    return filter_market_status_cards(cards, market_id=market_id)


def build_market_status_response(
    period: str | None = None,
    top_n: int = DEFAULT_MARKET_STATUS_TOP_N,
    market_id: str | None = None,
) -> list[dict[str, Any]]:
    del period, top_n  # v0.9.0 market-status is a 25 JW brand card list.
    return build_market_status_cards(market_id=market_id)


def _warnings_from_row(row: dict[str, Any]) -> list[str]:
    warnings = loads_json_maybe(row.get("warnings"))
    if isinstance(warnings, list):
        return [str(item) for item in warnings]
    return []


def _extended(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "metric_basis": "canonical_value",
        "cagr_1y": row.get("cagr_1y"),
        "cagr_3y": row.get("cagr_3y"),
        "cagr_5y": row.get("cagr_5y"),
        "ei_5y": row.get("ei_5y"),
        "momentum_score": row.get("momentum_score"),
        "growth_contribution": row.get("growth_contribution"),
    }


def _market_context(row: dict[str, Any]) -> dict[str, Any]:
    return {"hhi": row.get("hhi"), "market_cagr_5y": row.get("market_cagr_5y")}


def build_cause_response(
    brand_name: str,
    *,
    view: str,
    source: str | None,
    measure: str,
    period: str | None,
) -> dict[str, Any]:
    resolved = resolve_brand(brand_name)
    source = source or resolved.display.default_source
    is_valid, reason = validate_source_measure(resolved.display, source, measure)
    concrete_period = period or latest_period_for_brand(resolved.brand_id)

    if not is_valid:
        return {
            "brand": resolved.display.brand_name,
            "resolved_brand_id": resolved.brand_id,
            "resolved_brand_name": resolved.brand_name,
            "market_id": resolved.display.ml_id,
            "view": view,
            "source": source,
            "measure": measure,
            "unit_label": measure,
            "period_yyyymm": concrete_period,
            "summary": None,
            "monthly": [],
            "drivers": [],
            "market_context": {},
            "data": None,
            "reason": reason,
            "generated_at": now_iso(),
        }

    rows = db.fetch_all(
        """
        SELECT period_yyyymm, channel, specialty,
               market_share, mom, qoq, yoy, mat, growth_abs, rank_in_market,
               cagr_1y, cagr_3y, cagr_5y, ei_5y, momentum_score,
               growth_contribution, hhi, market_cagr_5y,
               JSON_EXTRACT(payload, '$.warnings') AS warnings
        FROM mart_core_brand_metric
        WHERE ml_id = %s
          AND brand_id = %s
          AND channel IS NULL AND specialty IS NULL
          AND period_yyyymm <= %s
        ORDER BY period_yyyymm
        """,
        (resolved.display.ml_id, resolved.brand_id, concrete_period),
    )
    rows = [to_jsonable(row) for row in rows]
    if not rows:
        raise HTTPException(status_code=404, detail=f"No cause rows for {brand_name}")
    summary_row = rows[-1]
    summary = {
        "market_share": summary_row.get("market_share"),
        "rank_in_market": summary_row.get("rank_in_market"),
        "mom": summary_row.get("mom"),
        "qoq": summary_row.get("qoq"),
        "yoy": summary_row.get("yoy"),
        "mat": summary_row.get("mat"),
        "growth_abs": summary_row.get("growth_abs"),
        "extended": _extended(summary_row),
        "market_context": _market_context(summary_row),
    }
    monthly = [
        {
            "period_yyyymm": row["period_yyyymm"],
            "market_share": row.get("market_share"),
            "mom": row.get("mom"),
            "qoq": row.get("qoq"),
            "yoy": row.get("yoy"),
            "mat": row.get("mat"),
            "growth_abs": row.get("growth_abs"),
            "rank_in_market": row.get("rank_in_market"),
            "extended": _extended(row),
            "market_context": _market_context(row),
            "warnings": _warnings_from_row(row),
        }
        for row in rows
    ]
    return {
        "brand": resolved.display.brand_name,
        "resolved_brand_id": resolved.brand_id,
        "resolved_brand_name": resolved.brand_name,
        "market_id": resolved.display.ml_id,
        "view": view,
        "source": source,
        "measure": measure,
        "unit_label": "KRW" if measure == "sales" else measure,
        "period_yyyymm": concrete_period,
        "summary": summary,
        "monthly": monthly,
        "drivers": compute_drivers(summary_row, view=view),
        "market_context": _market_context(summary_row),
        "data": {"metric_basis": "canonical_value"},
        "reason": None,
        "generated_at": now_iso(),
    }


def build_deep_analysis_response(brand_name: str, period: str | None = None) -> dict[str, Any]:
    resolved = resolve_brand(brand_name)
    concrete_period = period or latest_period_for_brand(resolved.brand_id)
    rows = db.fetch_all(
        """
        SELECT period_yyyymm, channel, specialty,
               market_share, rank_in_market,
               cagr_5y, ei_5y, momentum_score, growth_contribution,
               hhi, market_cagr_5y
        FROM mart_core_brand_metric
        WHERE ml_id = %s
          AND brand_id = %s
          AND channel IS NOT NULL
          AND specialty IS NOT NULL
          AND period_yyyymm = %s
        ORDER BY channel, specialty
        """,
        (resolved.display.ml_id, resolved.brand_id, concrete_period),
    )
    breakdown = [
        {
            "channel": str(row["channel"]),
            "specialty": str(row["specialty"]),
            "market_share": to_jsonable(row.get("market_share")),
            "rank": row.get("rank_in_market"),
            "cagr_5y": to_jsonable(row.get("cagr_5y")),
            "ei_5y": to_jsonable(row.get("ei_5y")),
            "momentum_score": to_jsonable(row.get("momentum_score")),
            "growth_contribution": to_jsonable(row.get("growth_contribution")),
            "hhi": to_jsonable(row.get("hhi")),
            "market_cagr_5y": to_jsonable(row.get("market_cagr_5y")),
        }
        for row in rows
    ]
    return {
        "brand": resolved.display.brand_name,
        "resolved_brand_id": resolved.brand_id,
        "resolved_brand_name": resolved.brand_name,
        "market_id": resolved.display.ml_id,
        "period_yyyymm": concrete_period,
        "breakdown": breakdown,
        "data": {"metric_basis": "canonical_value"},
        "generated_at": now_iso(),
    }
