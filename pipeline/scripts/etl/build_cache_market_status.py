#!/usr/bin/env python3
"""Build spec-aligned cache_market_status with separated UBIST/IQVIA KPI."""

from __future__ import annotations

from collections import defaultdict
import sys
from typing import Any

from cache_build_common import (
    API_TO_SOURCE,
    active_catalog_member_rows,
    catalog_input_manifest,
    CANONICAL_25,
    current_build_sha,
    display_ukrw,
    dump_payload,
    fetch_all,
    first_pair,
    load_catalog,
    metric_first,
    metric_recent,
    ml_to_strategy,
    numeric_mean,
    parser,
    payload_size,
    period_key,
    optional_float,
    replace_rows,
    series_latest_number,
    source_list,
    decode_json,
    safe_float,
    series_cagr,
    PROJECT_ROOT,
)

sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.scripts.api.metadata import BRAND_METADATA
from pipeline.etl.io.cache.archive_services_shim import MARKET_STATUS_COMPANY_BY_BRAND


BRAND_META_BY_NAME = {meta.brand: meta for meta in BRAND_METADATA}


def _history_number(item: object) -> float | None:
    if isinstance(item, dict):
        value = item.get("raw_value")
    else:
        value = item
    if value is None:
        return None
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def movement_pct_from_history(metric_history: dict | None) -> float | None:
    """Return latest-vs-previous movement pct from a period keyed history."""
    data = metric_history or {}
    if len(data) < 2:
        return None
    keys = sorted(data.keys(), key=period_key)
    previous = _history_number(data.get(keys[-2]))
    recent = _history_number(data.get(keys[-1]))
    if previous is None or recent is None or previous == 0:
        return None
    return (recent - previous) / previous * 100


def yoy_pct_from_history(metric_history: dict | None, source: str) -> float | None:
    data = metric_history or {}
    step = 12 if source == "ubist" else 4
    if len(data) <= step:
        return None
    keys = sorted(data.keys(), key=period_key)
    previous = _history_number(data.get(keys[-1 - step]))
    recent = _history_number(data.get(keys[-1]))
    if previous is None or recent is None or previous == 0:
        return None
    return (recent - previous) / previous * 100


def mat_yoy_pct_from_history(metric_history: dict | None, source: str) -> float | None:
    """Moving annual total YoY over 12 monthly or 4 quarterly periods."""
    data = metric_history or {}
    step = 12 if source == "ubist" else 4
    if len(data) < step * 2:
        return None
    keys = sorted(data.keys(), key=period_key)
    recent_values = [_history_number(data.get(key)) for key in keys[-step:]]
    previous_values = [_history_number(data.get(key)) for key in keys[-2 * step : -step]]
    if any(value is None for value in recent_values + previous_values):
        return None
    recent_total = sum(value for value in recent_values if value is not None)
    previous_total = sum(value for value in previous_values if value is not None)
    if previous_total == 0:
        return None
    return (recent_total - previous_total) / previous_total * 100


def ms_change_yoy_from_history(metric_history: dict | None, source: str) -> float | None:
    """Market-share YoY as percentage-point delta, not relative percent."""
    data = metric_history or {}
    step = 12 if source == "ubist" else 4
    if len(data) <= step:
        return None
    keys = sorted(data.keys(), key=period_key)
    recent = metric_recent(data).get("ms")
    previous_item = data.get(keys[-1 - step])
    previous = previous_item.get("ms") if isinstance(previous_item, dict) else None
    if recent is None or previous is None:
        return None
    return safe_float(recent) - safe_float(previous)


def source_card_payload(row: dict, market_recent: float | None = None) -> dict:
    history = decode_json(row.get("metric_history"))
    recent = metric_recent(history)
    movement = movement_pct_from_history(history)
    source = row.get("source")
    value_recent = safe_float(recent.get("raw_value"))
    if market_recent and market_recent > 0 and value_recent is not None:
        ms_recent = value_recent / market_recent * 100
    else:
        ms_recent = safe_float(recent.get("ms"))
    ym_yoy = yoy_pct_from_history(history, source)
    mat_yoy = safe_float(recent.get("yoy_mat"))
    if mat_yoy in (None, 0.0):
        mat_yoy = safe_float(recent.get("mat"))
    if mat_yoy in (None, 0.0):
        mat_yoy = mat_yoy_pct_from_history(history, source)
    ms_change_yoy = safe_float(recent.get("ms_change_yoy"))
    if ms_change_yoy in (None, 0.0):
        ms_change_yoy = ms_change_yoy_from_history(history, source)
    return {
        "value_recent": value_recent,
        "ms_recent_pct": ms_recent,
        "gr_mom_pct": movement if row.get("source") == "ubist" else safe_float(recent.get("mom")),
        "gr_qoq_pct": movement if row.get("source") == "iqvia_nsa" else safe_float(recent.get("qoq")),
        "gr_yoy_pct": safe_float(recent.get("yoy")) if safe_float(recent.get("yoy")) is not None else ym_yoy,
        "gr_yoy_mat_pct": mat_yoy,
        "gr_yoy_ym_pct": safe_float(recent.get("yoy_ym")) if safe_float(recent.get("yoy_ym")) not in (None, 0.0) else ym_yoy,
        "ms_change_yoy_pct": ms_change_yoy,
        "unit_label": row.get("unit_label"),
        "measure": row.get("measure"),
    }


def _valid_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    return text


def _period_first(metric_history: dict | None) -> str | None:
    period, _ = first_pair(metric_history)
    return str(period) if period is not None else None


def _source_label(sources: list[str]) -> str:
    if not sources:
        return ""
    return " + ".join(sources)


def _ordered_sources(sources: list[str]) -> list[str]:
    order = {"UBIST": 0, "IQVIA": 1}
    return sorted(dict.fromkeys(sources), key=lambda source: order.get(source, 99))


def _ratio_to_pct(value: Any) -> float | None:
    number = optional_float(value)
    return number * 100 if number is not None else None


def _ratio_to_pct_5y_then_3y(metric: dict[str, Any]) -> float | None:
    # 브랜드 CAGR 표시는 cause와 같은 5년→3년 fallback 계약을 따른다.
    # 리바로젯처럼 5년 시작점 매출이 0이면 cagr_5y는 계산 불가지만, 이미
    # extended_metric_history에 저장된 cagr_3y는 동일 endpoint 계산 산물이다.
    # 새 계산식을 만들지 않고 기존 ratio→percent 변환만 공유해 N/A 불일치를 줄인다.
    five_year = _ratio_to_pct(metric.get("cagr_5y"))
    return five_year if five_year is not None else _ratio_to_pct(metric.get("cagr_3y"))


def _market_definition_label(atc_codes: list[str]) -> str:
    return "1 ATC" if len(atc_codes) == 1 else f"{len(atc_codes)} ATCs"


def _catalog_atc_codes(market: dict) -> list[str]:
    raw_codes = decode_json(market.get("atc_codes_json"))
    if not isinstance(raw_codes, list):
        return []
    return [str(code).strip() for code in raw_codes if str(code).strip()]


def _catalog_company(catalog_row: dict) -> str | None:
    return _valid_text(catalog_row.get("판매사")) or _valid_text(catalog_row.get("제조사"))


def _direct_competition_count(strategic_brand: Any, cd_id: Any) -> int:
    cd = _valid_text(cd_id)
    if not cd or strategic_brand is None:
        return 0
    return len(active_catalog_member_rows(strategic_brand, "cd_id", cd))


def _market_brand_count(strategic_brand: Any, ml_id: Any) -> int:
    market = _valid_text(ml_id)
    if not market or strategic_brand is None:
        return 0
    return len(active_catalog_member_rows(strategic_brand, "ml_id", market))


def _first_existing(*values: Any) -> Any:
    for value in values:
        if isinstance(value, list):
            if value:
                return value
        elif _valid_text(value) is not None:
            return value
    return None


def build_brand_card(
    brand_row: dict,
    market: dict,
    sales_rows: list[dict],
    market_rows: dict,
    strategic_brand: Any,
) -> dict:
    preferred = next((r for r in sales_rows if r["source"] == "ubist"), None) or (sales_rows[0] if sales_rows else {})
    metric_history = decode_json(preferred.get("metric_history"))
    extended = decode_json(preferred.get("extended_metric_history"))
    recent = metric_recent(metric_history)
    first = metric_first(metric_history)
    ext_recent = metric_recent(extended)
    market_metric = market_rows.get((preferred.get("ml_id"), preferred.get("source"), "sales"), {})
    market_series = decode_json(market_metric.get("market_size_series"))
    market_recent = series_latest_number(market_series)
    source_payloads: dict[str, dict] = {}
    for row in sales_rows:
        row_market_metric = market_rows.get((row.get("ml_id"), row.get("source"), "sales"), {})
        row_market_series = decode_json(row_market_metric.get("market_size_series"))
        row_market_recent = series_latest_number(row_market_series)
        source_payloads["UBIST" if row["source"] == "ubist" else "IQVIA"] = source_card_payload(row, row_market_recent)
    sources_data = source_payloads
    default_source = "UBIST" if "UBIST" in sources_data else (next(iter(sources_data.keys()), None))
    preferred_payload = source_card_payload(preferred, market_recent) if preferred else {}
    brand_name = brand_row["brand"]
    meta = BRAND_META_BY_NAME.get(brand_name)
    meta_sources = list(meta.sources) if meta else []
    sources = _ordered_sources(meta_sources or brand_row["sources"])
    atc_codes = _catalog_atc_codes(market)
    brand_cagr = _ratio_to_pct_5y_then_3y(ext_recent)
    # 헤드라인 market CAGR은 "시장 자체" endpoint 기준이다.
    # cause payload의 per-brand EI는 브랜드 시작값이 0이면 3년으로 fallback될 수
    # 있으나, market-status 헤더는 브랜드별 fallback에 오염되면 안 된다.
    # 따라서 공유 helper의 시장 series endpoint(5y 가능 시 5y, 없으면 3y)를
    # 그대로 사용한다. per-brand 기준에 맞추는 대안은 헤드라인 의미를 깨서 기각했다.
    market_cagr = series_cagr(market_series)
    excess_growth = brand_cagr - market_cagr if brand_cagr is not None and market_cagr is not None else None
    company = (
        MARKET_STATUS_COMPANY_BY_BRAND.get(brand_name)
        or _catalog_company(brand_row.get("catalog_row", {}))
        or "JW중외제약"
    )
    market_name = _first_existing(meta.market_name if meta else None, market.get("name"))
    market_name_short = _first_existing(meta.market_name_short if meta else None, market_name, brand_name)
    atc_desc = _first_existing(meta.atc_desc if meta else None, "")
    market_label_kor = _first_existing(meta.market_label_kor if meta else None, market_name_short)
    mkt_team = _valid_text(meta.mkt_team if meta else None)
    nhi_type = _valid_text(brand_row.get("catalog_row", {}).get("nhi_type")) or "NHI"
    direct_competition_count = _direct_competition_count(strategic_brand, brand_row.get("catalog_row", {}).get("cd_id"))
    total_brands_in_market = _market_brand_count(strategic_brand, brand_row.get("catalog_row", {}).get("ml_id"))
    recent_rank_raw = recent.get("rank")
    recent_rank = safe_float(recent_rank_raw) if recent_rank_raw not in (None, "") else None

    return {
        "rank": int(recent_rank) if recent_rank is not None else (int(meta.rank) if meta else None),
        "total_brands_in_market": total_brands_in_market,
        "brand": brand_name,
        "company": company,
        "market_id": brand_row["market_id"],
        "market_name": market_name,
        "market_name_short": market_name_short,
        "mkt_team": mkt_team,
        "atc_codes": atc_codes,
        "atc_desc": atc_desc,
        "nhi_type": nhi_type,
        "is_jw": True,
        "is_target": brand_row["is_target"],
        "sources": sources,
        "front": {
            "value_recent": safe_float(recent.get("raw_value")),
            "ms_recent_pct": preferred_payload.get("ms_recent_pct"),
            "gr_mom_pct": preferred_payload.get("gr_mom_pct") if preferred else None,
            "gr_qoq_pct": preferred_payload.get("gr_qoq_pct") if preferred else safe_float(recent.get("qoq")),
            "gr_yoy_pct": preferred_payload.get("gr_yoy_pct") if preferred else safe_float(recent.get("yoy")),
            "gr_yoy_mat_pct": preferred_payload.get("gr_yoy_mat_pct") if preferred else None,
            "gr_yoy_ym_pct": preferred_payload.get("gr_yoy_ym_pct") if preferred else None,
            "ms_change_yoy_pct": preferred_payload.get("ms_change_yoy_pct") if preferred else None,
            "sources_data": sources_data,
            "default_source": default_source,
        },
        "back": {
            "cagr_5y_pct": brand_cagr,
            "sales_first_period_krw": safe_float(first.get("raw_value")),
            "ms_first_period_pct": safe_float(first.get("ms")),
            "period_first": _period_first(metric_history),
        },
        "back_extended": {
            "market_size_recent": market_recent,
            "market_cagr_5y_pct": market_cagr,
            "brand_cagr_5y_pct": brand_cagr,
            "excess_growth_pct": excess_growth,
            "source_label": _source_label(sources),
            "is_dual_source": len(sources) > 1,
            "sources": sources,
            "market_definition_label": _market_definition_label(atc_codes),
            "market_definition_full": ", ".join(atc_codes),
            "atc_count": len(atc_codes),
            "direct_competition_count": direct_competition_count,
            "market_label_kor": market_label_kor,
        },
    }


def history_period_totals(rows: list[dict]) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        for period, item in (decode_json(row.get("metric_history")) or {}).items():
            value = _history_number(item)
            if value is not None:
                totals[str(period)] += value
    return totals


def cagr_from_source_rows(source: str, rows: list[dict]) -> float | None:
    totals = history_period_totals(rows)
    if len(totals) < 2:
        return None
    periods = sorted(totals.keys(), key=period_key)
    first = totals[periods[0]]
    last = totals[periods[-1]]
    if first <= 0 or last <= 0:
        return None
    periods_per_year = 12.0 if source == "UBIST" else 4.0
    years = (len(periods) - 1) / periods_per_year
    if years <= 0:
        return None
    return ((last / first) ** (1 / years) - 1) * 100


def period_recent_from_rows(rows: list[dict]) -> str | None:
    periods = set()
    for row in rows:
        periods.update(str(period) for period in (decode_json(row.get("metric_history")) or {}).keys())
    if not periods:
        return None
    return sorted(periods, key=period_key)[-1]


def build_kpi(source: str, rows: list[dict]) -> dict:
    source_rows = [r for r in rows if r["source"] == API_TO_SOURCE[source] and r["measure"] == "sales"]
    latest_values = []
    ms_values = []
    movement_values = []
    yoy_values = []
    mat_yoy_values = []
    ym_yoy_values = []
    ms_change_yoy_values = []
    for row in source_rows:
        history = decode_json(row["metric_history"])
        recent = metric_recent(history)
        latest_values.append(safe_float(recent.get("raw_value")))
        ms_values.append(safe_float(recent.get("ms")))
        movement = movement_pct_from_history(history)
        if movement is not None:
            movement_values.append(movement)
        yoy = yoy_pct_from_history(history, row["source"])
        if yoy is not None:
            yoy_values.append(yoy)
        mat_yoy = safe_float(recent.get("yoy_mat"))
        if mat_yoy in (None, 0.0):
            mat_yoy = safe_float(recent.get("mat"))
        if mat_yoy in (None, 0.0):
            mat_yoy = mat_yoy_pct_from_history(history, row["source"])
        if mat_yoy is not None:
            mat_yoy_values.append(mat_yoy)
        ym_yoy = safe_float(recent.get("yoy_ym"))
        if ym_yoy in (None, 0.0):
            ym_yoy = yoy
        if ym_yoy is not None:
            ym_yoy_values.append(ym_yoy)
        ms_change_yoy = safe_float(recent.get("ms_change_yoy"))
        if ms_change_yoy in (None, 0.0):
            ms_change_yoy = ms_change_yoy_from_history(history, row["source"])
        if ms_change_yoy is not None:
            ms_change_yoy_values.append(ms_change_yoy)
    total = sum(latest_values)
    rising = sum(1 for value in movement_values if value >= 0)
    declining = sum(1 for value in movement_values if value < 0)
    return {
        "total_sales_recent_krw": total,
        "avg_ms_per_brand_pct": numeric_mean(ms_values),
        "sales_up_count": rising,
        "sales_down_count": declining,
        "avg_cagr_5y_pct": cagr_from_source_rows(source, source_rows),
        "avg_yoy_pct": numeric_mean(yoy_values),
        "gr_yoy_pct": numeric_mean(yoy_values),
        "gr_yoy_mat_pct": numeric_mean(mat_yoy_values),
        "gr_yoy_ym_pct": numeric_mean(ym_yoy_values),
        "ms_change_yoy_pct": numeric_mean(ms_change_yoy_values),
        "period_recent": period_recent_from_rows(source_rows),
        "brand_count": rising + declining,
    }


def main() -> None:
    cli = parser(__doc__)
    cli.add_argument(
        "--target-table",
        default="cache_market_status",
        help="Destination table. Use schema.table for test cache refreshes.",
    )
    args = cli.parse_args()
    strategic_brand = load_catalog("strategic_brand")
    ml_market = load_catalog("ml_market").set_index("ml_id", drop=False)

    jw = strategic_brand[strategic_brand["is_jw"].astype(bool)].copy()
    actual = set(jw["canonical_name"].fillna(jw["name"]).astype(str))
    if actual != CANONICAL_25:
        raise SystemExit(f"canonical brand mismatch: missing={sorted(CANONICAL_25 - actual)}, extra={sorted(actual - CANONICAL_25)}")

    mart_rows = fetch_all(
        """
        SELECT *
        FROM mart_strategic_ml_brand_metric
        WHERE is_jw = 1
        """
    )
    sales_by_brand: dict[str, list[dict]] = defaultdict(list)
    for row in mart_rows:
        if row["measure"] == "sales":
            sales_by_brand[row["brand_name"]].append(row)

    market_rows = {
        (r["ml_id"], r["source"], r["measure"]): r
        for r in fetch_all("SELECT * FROM mart_strategic_ml_market_metric")
    }

    cards = []
    for _, row in jw.sort_values(["ml_id", "is_target", "brand_id"], ascending=[True, False, True]).iterrows():
        ml_id = str(row["ml_id"])
        market = ml_market.loc[ml_id].to_dict() if ml_id in ml_market.index else {}
        brand = str(row.get("canonical_name") or row.get("name"))
        cards.append(
            build_brand_card(
                {
                    "brand": brand,
                    "brand_key": str(row.get("brand_key") or brand),
                    "market_id": ml_to_strategy(ml_id),
                    "is_target": bool(row.get("is_target")),
                    "sources": source_list(market.get("data_source")),
                    "catalog_row": row.to_dict(),
                },
                market,
                sales_by_brand.get(brand, []),
                market_rows,
                strategic_brand,
            )
        )

    payload = {
        "kpi_summary": {
            "UBIST": build_kpi("UBIST", mart_rows),
            "IQVIA": build_kpi("IQVIA", mart_rows),
        },
        "brand_cards": cards,
    }
    replace_rows(
        args.target_table,
        ["query_key", "response_json", "payload_size", "build_sha", "input_manifest_json"],
        [{
            "query_key": "default",
            "response_json": dump_payload(payload),
            "payload_size": payload_size(payload),
            "build_sha": current_build_sha(),
            "input_manifest_json": catalog_input_manifest({
                "ml_market": ml_market.reset_index().to_dict("records"),
                "strategic_brand": strategic_brand,
            }),
        }],
    )
    if args.verbose:
        print(f"cache_market_status default brand_cards={len(cards)} payload_size={payload_size(payload)}")


if __name__ == "__main__":
    main()
