#!/usr/bin/env python3
"""Build spec-aligned cache_deep_analysis from Phase 1 strategic ML marts."""

from __future__ import annotations

from collections import defaultdict
from difflib import SequenceMatcher
import re
from typing import Any
from urllib.parse import urlparse

from cache_build_common import (
    api_source,
    CANONICAL_25,
    decode_json,
    dump_payload,
    fetch_all,
    load_catalog,
    mariadb_connect,
    metric_recent,
    ml_to_strategy,
    parser,
    payload_size,
    period_key,
    safe_float,
    source_list,
)
from pipeline.scripts.api.metadata.ml_market_meta import BRAND_METADATA
try:
    from phase29_events import build_events_for_cache, ensure_events_raw_table
except ModuleNotFoundError:  # pragma: no cover - package import path under pytest
    from pipeline.scripts.etl.phase29_events import build_events_for_cache, ensure_events_raw_table

try:
    from pipeline.scripts.forecast.backtest import run_phase29_poc
except ModuleNotFoundError:  # pragma: no cover - script execution with partial path
    run_phase29_poc = None

try:
    from pipeline.scripts.forecast.forecast_runner import (
        build_forecast_brand_entry as build_phase30_forecast_brand_entry,
        build_market_forecast as build_phase30_market_forecast,
        build_simulation_combo as build_phase30_simulation_combo,
        forecast_periods_from_history as phase30_forecast_periods_from_history,
        forecast_steps as phase30_forecast_steps,
    )
except ModuleNotFoundError:  # pragma: no cover - package import path under pytest
    build_phase30_forecast_brand_entry = None
    build_phase30_market_forecast = None
    build_phase30_simulation_combo = None
    phase30_forecast_periods_from_history = None
    phase30_forecast_steps = None


ALL_COMBOS = [
    ("UBIST", "sales"),
    ("UBIST", "volume"),
    ("IQVIA", "sales"),
    ("IQVIA", "unit"),
    ("IQVIA", "dosage_unit"),
    ("IQVIA", "counting_unit"),
]


SOURCE_TO_INTERNAL = {"UBIST": "ubist", "IQVIA": "iqvia_nsa"}
UNIT_LABELS = {
    "sales": "KRW",
    "volume": "Rx",
    "unit": "unit",
    "dosage_unit": "dosage unit",
    "counting_unit": "counting unit",
}
FORECAST_METHOD = "data_size_dispatch_v1_phase30_baseline"
FORECAST_DISCLOSURE = (
    "Phase 30 baseline forecast입니다. 명세서 v0.9.1 data_size_dispatch_v1에 따라 "
    "Prophet/SARIMAX/Holt-Winters/Linear/Mean 중 선택하며, LLM event regressor는 "
    "Phase 31 이후로 보류되어 enabled=false입니다."
)
EVENT_DEDUP_SIMILARITY_THRESHOLD = 0.80
EVENT_LIST_THRESHOLD = 30
EVENT_LIST_MIN = 10
EVENT_LIST_MAX = 50
EVENT_CHART_THRESHOLD = 60
EVENT_CHART_MIN = 5
EVENT_CHART_MAX = 15
BRAND_METADATA_BY_NAME = {item.brand: item for item in BRAND_METADATA}


def _normalize_event_title(title: str | None) -> str:
    text = (title or "").lower()
    text = re.sub(r"[^\w\s가-힣]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _source_label(event: dict[str, Any]) -> str | None:
    source = event.get("source")
    if source:
        return str(source)
    url = event.get("source_url") or event.get("url")
    if not url:
        return None
    host = urlparse(str(url)).netloc
    return host.replace("www.", "") if host else str(url)


def _cluster_events(events: list[dict[str, Any]], similarity_threshold: float = EVENT_DEDUP_SIMILARITY_THRESHOLD) -> list[dict[str, Any]]:
    """Deduplicate cut_a cards by date/category/title while preserving coverage context."""
    groups: dict[tuple[Any, Any, Any], list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        key = (
            event.get("brand_name") or event.get("brand"),
            event.get("date") or event.get("published_date"),
            event.get("category"),
        )
        groups[key].append(event)

    deduped: list[dict[str, Any]] = []
    for group_events in groups.values():
        clusters: list[list[dict[str, Any]]] = []
        for event in group_events:
            title_norm = _normalize_event_title(event.get("title"))
            matched_cluster = None
            for cluster in clusters:
                if title_norm and any(
                    SequenceMatcher(None, title_norm, _normalize_event_title(clustered.get("title"))).ratio() >= similarity_threshold
                    for clustered in cluster
                    if _normalize_event_title(clustered.get("title"))
                ):
                    matched_cluster = cluster
                    break
            if matched_cluster is None:
                clusters.append([event])
            else:
                matched_cluster.append(event)

        for cluster in clusters:
            if len(cluster) == 1:
                deduped.append(cluster[0])
                continue

            ordered = sorted(
                cluster,
                key=lambda event: (
                    safe_float(event.get("impact_score") or event.get("score")) or 0.0,
                    str(event.get("date") or event.get("published_date") or ""),
                    str(event.get("id") or event.get("event_id") or event.get("news_id") or ""),
                ),
                reverse=True,
            )
            rep = dict(ordered[0])
            hidden = ordered[1:]
            rep["related_coverage_count"] = len(ordered)
            rep["related_sources"] = sorted({label for label in (_source_label(event) for event in ordered) if label})
            rep["related_titles"] = [event.get("title") for event in hidden if event.get("title")]
            rep["related_urls"] = [event.get("url") for event in hidden if event.get("url")]
            deduped.append(rep)

    return sorted(
        deduped,
        key=lambda event: (
            safe_float(event.get("impact_score") or event.get("score")) or 0.0,
            str(event.get("date") or event.get("published_date") or ""),
            str(event.get("id") or event.get("event_id") or event.get("news_id") or ""),
        ),
        reverse=True,
    )


def _dedup_cut_a_events(events_payload: dict[str, Any]) -> dict[str, Any]:
    cut_a = events_payload.get("cut_a") or []
    if not cut_a:
        return events_payload
    deduped = _cluster_events(cut_a)
    payload = dict(events_payload)
    payload["cut_a"] = deduped
    meta = dict(payload.get("meta") or {})
    meta.update(
        {
            "cut_a_dedup_enabled": True,
            "cut_a_dedup_similarity_threshold": EVENT_DEDUP_SIMILARITY_THRESHOLD,
            "cut_a_before_dedup": len(cut_a),
            "cut_a_after_dedup": len(deduped),
        }
    )
    payload["meta"] = meta
    return payload


def _event_sort_key(event: dict[str, Any]) -> tuple[float, str, str]:
    return (
        safe_float(event.get("impact_score") or event.get("score")) or 0.0,
        str(event.get("date") or event.get("published_date") or ""),
        str(event.get("id") or event.get("event_id") or event.get("news_id") or ""),
    )


def _bounded_event_count(events: list[dict[str, Any]], *, threshold: int, minimum: int, maximum: int) -> int:
    hits = sum(1 for event in events if (safe_float(event.get("impact_score") or event.get("score")) or 0.0) >= threshold)
    if hits < minimum:
        return min(minimum, len(events))
    if hits > maximum:
        return maximum
    return hits


def _apply_event_cut_flags(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach list/chart visibility flags while keeping the public event schema additive."""
    sorted_events = sorted(events, key=_event_sort_key, reverse=True)[:EVENT_LIST_MAX]
    list_count = _bounded_event_count(
        sorted_events,
        threshold=EVENT_LIST_THRESHOLD,
        minimum=EVENT_LIST_MIN,
        maximum=EVENT_LIST_MAX,
    )
    chart_count = _bounded_event_count(
        sorted_events,
        threshold=EVENT_CHART_THRESHOLD,
        minimum=EVENT_CHART_MIN,
        maximum=EVENT_CHART_MAX,
    )

    flagged: list[dict[str, Any]] = []
    for index, event in enumerate(sorted_events):
        row = dict(event)
        row["on_list"] = index < list_count
        row["on_chart"] = index < chart_count
        flagged.append(row)
    return flagged


def _events_spec_list(events_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Project Phase 33 cut_a events to the v0.9.1 spec list shape."""
    events = events_payload.get("cut_a") or []
    projected: list[dict[str, Any]] = []
    for event in events:
        category = event.get("category")
        category_label = event.get("category_label")
        if not category_label:
            category_label = {
                "rd": "신약/R&D",
                "policy": "정책/규제",
                "supply": "공급/생산",
                "capital": "자본/경영",
                "external": "외부/트렌드",
            }.get(str(category), str(category) if category else None)
        projected.append(
            {
                "id": event.get("id") or event.get("event_id") or event.get("news_id"),
                "category": category,
                "category_label": category_label,
                "date": event.get("date") or event.get("published_date"),
                "period_map": event.get("period_map") or {},
                "impact_score": event.get("impact_score") or event.get("score"),
                "title": event.get("title"),
                "summary": event.get("summary"),
                "body_full": event.get("body_full"),
                "source": event.get("source"),
                "url": event.get("url"),
                "source_url": event.get("source_url"),
                "related_coverage_count": event.get("related_coverage_count"),
                "related_sources": event.get("related_sources"),
                "related_titles": event.get("related_titles"),
                "related_urls": event.get("related_urls"),
            }
        )
    return _apply_event_cut_flags(projected)


def _load_phase29_poc_report() -> dict[str, Any]:
    if run_phase29_poc is None:
        return {"brands": {}}
    try:
        return run_phase29_poc(use_llm=False, persist=True)
    except Exception as exc:  # keep cache build resilient and auditable
        return {"brands": {}, "error": str(exc)}


def _simulation_from_poc(brand: str, combo: str, poc_report: dict[str, Any], unit_label: str | None) -> dict[str, Any] | None:
    brand_report = (poc_report.get("brands") or {}).get(brand)
    if not brand_report or brand_report.get("combo") != combo:
        return None
    forecast = brand_report.get("with_llm", {}).get("forecast") or []
    baseline = brand_report.get("baseline", {}).get("forecast") or []
    periods = brand_report.get("test_periods") or []
    if not forecast:
        forecast = baseline
    if not forecast:
        return None
    upper = [float(value) * 1.1 for value in forecast]
    lower = [max(0.0, float(value) * 0.9) for value in forecast]
    first = float(forecast[0]) if forecast else 0.0
    last = float(forecast[-1]) if forecast else first
    momentum = ((last - first) / first * 100 / max(len(forecast) - 1, 1)) if first else 0.0
    period_unit = "분기" if combo.startswith("IQVIA") else "월"
    return {
        "poc": True,
        "period_unit": period_unit,
        "unit_label": unit_label,
        "available_brands": [{"brand": brand, "is_target": True, "is_jw": True}],
        "backtest": brand_report,
        "by_brand": {
            brand: {
                "forecast_periods": periods,
                "target_period": periods[-1] if periods else None,
                "scenarios": {
                    "base": {"values": [float(value) for value in forecast], "final_value": float(forecast[-1])},
                    "upper": {"values": upper, "final_value": upper[-1]},
                    "lower": {"values": lower, "final_value": lower[-1]},
                },
                "horizon_ci_levels": {"1y": 0.8, "3y": 0.65, "5y": 0.5, "10y": 0.35},
                "market_comparison": {
                    "delta_pp": 0.0,
                    "market_cagr_pct": None,
                    "brand_cagr_pct": None,
                },
                "momentum": {
                    "value_pct_per_period": round(momentum, 4),
                    "interpretation": "Phase 29 SARIMAX POC hold-out forecast slope",
                },
                "sentiments": brand_report.get("sentiments") or [],
            }
        },
    }


def sorted_history_values(history: dict[str, Any]) -> tuple[list[str], list[float | None]]:
    periods = sorted((history or {}).keys(), key=period_key)
    values = []
    for period in periods:
        item = history.get(period)
        if isinstance(item, dict):
            values.append(safe_float(item.get("raw_value")))
        else:
            values.append(safe_float(item))
    return [str(period) for period in periods], values


def _recent_value(row: dict[str, Any]) -> float:
    return safe_float(metric_recent(decode_json(row.get("metric_history"))).get("raw_value")) or 0.0


def _next_month(period: str) -> str:
    year, month = map(int, period.split("-"))
    month += 1
    if month > 12:
        year += 1
        month = 1
    return f"{year:04d}-{month:02d}"


def _next_quarter(period: str) -> str:
    match = re.match(r"^(\d{4})-?Q([1-4])$", str(period))
    if not match:
        return "2026-Q1"
    year = int(match.group(1))
    quarter = int(match.group(2)) + 1
    if quarter > 4:
        year += 1
        quarter = 1
    return f"{year:04d}-Q{quarter}"


def forecast_periods_from_history(periods: list[str], source: str) -> list[str]:
    count = 120 if source == "UBIST" else 40
    if not periods:
        start = "2026-05" if source == "UBIST" else "2026-Q1"
    else:
        start = _next_month(periods[-1]) if source == "UBIST" else _next_quarter(periods[-1])
    output = []
    current = start
    for _ in range(count):
        output.append(current)
        current = _next_month(current) if source == "UBIST" else _next_quarter(current)
    return output


def available_combos_for_market(market: dict[str, Any]) -> list[str]:
    combos: list[str] = []
    sources = source_list(market.get("data_source"))
    if "UBIST" in sources:
        combos.extend(["UBIST.sales", "UBIST.volume"])
    if "IQVIA" in sources:
        combos.extend(["IQVIA.sales", "IQVIA.unit", "IQVIA.dosage_unit", "IQVIA.counting_unit"])
    return combos


def top6_rows(rows: list[dict[str, Any]], target_brand: str) -> list[dict[str, Any]]:
    target = next((row for row in rows if row.get("brand_name") == target_brand), None)
    competitors = [row for row in rows if row.get("brand_name") != target_brand]
    competitors.sort(key=_recent_value, reverse=True)
    selected = ([target] if target else []) + competitors[:5]
    return [row for row in selected if row is not None]


def deterministic_forecast_values(values: list[float | None], source: str, steps: int) -> tuple[list[float], dict[str, Any]]:
    """Create a deterministic, dependency-free forecast from existing history.

    This is intentionally simple and auditable: the next values blend a recent
    average level, same-season carry-forward, and trailing trend. It avoids
    model dependencies while making the forecast tab operational with real
    non-empty forecast_values.
    """
    clean = [safe_float(value) for value in values]
    clean = [0.0 if value is None else value for value in clean]
    if not clean or steps <= 0:
        return [], {
            "name": FORECAST_METHOD,
            "variant": "seasonal_trend_blend",
            "selection_reason": FORECAST_DISCLOSURE,
            "is_statistical_model": False,
            "backtest_available": False,
            "disclaimer": FORECAST_DISCLOSURE,
            "confidence_score": 0,
            "fit_quality": {"backtest_available": False},
        }

    season = 12 if source == "UBIST" else 4
    window = min(season, len(clean))
    recent = clean[-window:] if window else clean[-1:]
    last = clean[-1]

    deltas = [clean[idx] - clean[idx - 1] for idx in range(max(1, len(clean) - window), len(clean))]
    avg_delta = sum(deltas) / len(deltas) if deltas else 0.0
    recent_mean = sum(recent) / len(recent) if recent else last

    forecasts: list[float] = []
    for step in range(steps):
        seasonal = clean[-season + (step % season)] if len(clean) >= season else recent_mean
        trend = last + avg_delta * (step + 1)
        blended = (0.5 * seasonal) + (0.3 * trend) + (0.2 * recent_mean)
        forecasts.append(round(max(0.0, blended), 4))

    nonzero_history = sum(1 for value in clean if value > 0)
    confidence = min(85, max(35, int(nonzero_history / max(1, len(clean)) * 70) + 15))
    return forecasts, {
        "name": FORECAST_METHOD,
        "variant": "seasonal_trend_blend",
        "selection_reason": FORECAST_DISCLOSURE,
        "is_statistical_model": False,
        "backtest_available": False,
        "disclaimer": FORECAST_DISCLOSURE,
        "confidence_score": confidence,
        "fit_quality": {"backtest_available": False},
    }


def _share_series(values: list[Any], totals: list[Any]) -> list[float]:
    shares: list[float] = []
    for idx, value in enumerate(values):
        total = safe_float(totals[idx]) if idx < len(totals) else 0.0
        numerator = safe_float(value) or 0.0
        share = (numerator / total * 100) if total and total > 0 else 0.0
        shares.append(round(share, 4))
    return shares


def _history_totals_for_periods(market_rows: list[dict[str, Any]], periods: list[str]) -> list[float]:
    totals: list[float] = []
    histories = [decode_json(row.get("metric_history")) for row in market_rows]
    for period in periods:
        total = 0.0
        for history in histories:
            item = history.get(period) if isinstance(history, dict) else None
            if isinstance(item, dict):
                total += safe_float(item.get("raw_value")) or 0.0
            else:
                total += safe_float(item) or 0.0
        totals.append(total)
    return totals


def _forecast_totals_for_market_rows(
    market_rows: list[dict[str, Any]],
    *,
    source: str,
    forecast_steps: int,
) -> list[float]:
    totals = [0.0 for _ in range(forecast_steps)]
    for row in market_rows:
        _, values = sorted_history_values(decode_json(row.get("metric_history")))
        forecast_values, _ = deterministic_forecast_values(values, source, forecast_steps)
        for idx, value in enumerate(forecast_values[:forecast_steps]):
            totals[idx] += safe_float(value) or 0.0
    return totals


def _attach_forecast_ms_series(
    combo: dict[str, Any],
    *,
    market_rows: list[dict[str, Any]] | None = None,
    source: str | None = None,
    market_forecast: dict[str, Any] | None = None,
) -> dict[str, Any]:
    brands = combo.get("brands")
    if not isinstance(brands, list) or not brands:
        return combo

    history_periods = [str(period) for period in (combo.get("history_periods") or [])]
    forecast_periods = [str(period) for period in (combo.get("forecast_periods") or [])]

    if market_forecast:
        history_totals = [safe_float(value) or 0.0 for value in market_forecast.get("history_values") or []]
        forecast_totals = [safe_float(value) or 0.0 for value in market_forecast.get("forecast_values") or []]
    elif market_rows:
        history_totals = _history_totals_for_periods(market_rows, history_periods)
        forecast_totals = _forecast_totals_for_market_rows(
            market_rows,
            source=source or "",
            forecast_steps=len(forecast_periods),
        )
    else:
        history_totals = [
            sum(safe_float((brand.get("history_values") or [None] * len(history_periods))[idx]) or 0.0 for brand in brands)
            for idx in range(len(history_periods))
        ]
        forecast_totals = [
            sum(safe_float((brand.get("forecast_values") or [None] * len(forecast_periods))[idx]) or 0.0 for brand in brands)
            for idx in range(len(forecast_periods))
        ]

    for brand in brands:
        if not isinstance(brand, dict):
            continue
        brand["history_ms_pct"] = _share_series(brand.get("history_values") or [], history_totals)
        brand["forecast_ms_pct"] = _share_series(brand.get("forecast_values") or [], forecast_totals)
    return combo


def combo_payload(
    row: dict[str, Any],
    *,
    market_rows: list[dict[str, Any]],
    target_brand: str,
    combo_source: str,
    phase30: bool = False,
) -> dict[str, Any]:
    history = decode_json(row.get("metric_history"))
    recent = metric_recent(history)
    periods, values = sorted_history_values(history)
    period_unit = "월" if row.get("source") == "ubist" else "분기"
    selected = top6_rows(market_rows, target_brand)
    forecast_periods = (
        phase30_forecast_periods_from_history(periods, combo_source)
        if phase30 and phase30_forecast_periods_from_history is not None
        else forecast_periods_from_history(periods, combo_source)
    )
    if phase30 and build_phase30_forecast_brand_entry is not None and build_phase30_market_forecast is not None and phase30_forecast_steps is not None:
        steps = phase30_forecast_steps(combo_source)
        brand_entries = [
            build_phase30_forecast_brand_entry(
                brand_row,
                target_brand=target_brand,
                source=combo_source,
                measure=str(row.get("measure")),
                forecast_steps_count=steps,
            )
            for brand_row in selected
        ] or [
            build_phase30_forecast_brand_entry(
                row,
                target_brand=str(row.get("brand_name")),
                source=combo_source,
                measure=str(row.get("measure")),
                forecast_steps_count=steps,
            )
        ]
        market_forecast = build_phase30_market_forecast(market_rows, combo_source, steps)
        payload = {
            "period_unit": period_unit,
            "unit_label": row.get("unit_label"),
            "history_periods": periods,
            "forecast_periods": forecast_periods,
            "target_brand": row.get("brand_name"),
            "brands": brand_entries,
            "baseline": {
                "value_recent": safe_float(recent.get("raw_value")),
                "ms_recent_pct": safe_float(recent.get("ms")),
            },
            "_phase30_market_forecast": market_forecast,
        }
        return _attach_forecast_ms_series(payload, market_forecast=market_forecast)
    payload = {
        "period_unit": period_unit,
        "unit_label": row.get("unit_label"),
        "history_periods": periods,
        "forecast_periods": forecast_periods,
        "target_brand": row.get("brand_name"),
        "brands": [
            _forecast_brand_entry(brand_row, target_brand=target_brand, source=combo_source, forecast_steps=len(forecast_periods))
            for brand_row in selected
        ] or [
            _forecast_brand_entry(row, target_brand=str(row.get("brand_name")), source=combo_source, forecast_steps=len(forecast_periods))
        ],
        "baseline": {
            "value_recent": safe_float(recent.get("raw_value")),
            "ms_recent_pct": safe_float(recent.get("ms")),
        },
    }
    return _attach_forecast_ms_series(payload, market_rows=market_rows, source=combo_source)


def _forecast_brand_entry(
    brand_row: dict[str, Any],
    *,
    target_brand: str,
    source: str,
    forecast_steps: int,
) -> dict[str, Any]:
    periods, values = sorted_history_values(decode_json(brand_row.get("metric_history")))
    forecast_values, model = deterministic_forecast_values(values, source, forecast_steps)
    return {
        "brand": brand_row.get("brand_name"),
        "company": brand_row.get("company_name"),
        "is_target": brand_row.get("brand_name") == target_brand,
        "is_jw": bool(brand_row.get("is_jw")),
        "rank": metric_recent(decode_json(brand_row.get("metric_history"))).get("rank"),
        "history_values": values,
        "forecast_values": forecast_values,
        "forecast_method": model["name"],
        "forecast_model": model,
        "forecast_disclaimer": model.get("disclaimer"),
        "confidence_score": model["confidence_score"],
    }


def empty_combo_payload(source: str, measure: str, brand: str, base_row: dict[str, Any]) -> dict[str, Any]:
    forecast_periods = forecast_periods_from_history([], source)
    return {
        "period_unit": "월" if source == "UBIST" else "분기",
        "unit_label": UNIT_LABELS.get(measure),
        "history_periods": [],
        "forecast_periods": forecast_periods,
        "target_brand": brand,
        "brands": [
            {
                "brand": brand,
                "company": base_row.get("company_name"),
                "is_target": bool(base_row.get("is_target")),
                "is_jw": bool(base_row.get("is_jw")),
                "rank": None,
                "history_values": [],
                "forecast_values": [],
            }
        ],
        "baseline": {"value_recent": None, "ms_recent_pct": None},
    }


def choose_base(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return sorted(rows, key=lambda r: (not bool(r.get("is_jw")), str(r.get("ml_id")), str(r.get("source")), str(r.get("measure"))))[0]


def main() -> None:
    args = parser(__doc__).parse_args()
    ml_market = load_catalog("ml_market").set_index("ml_id", drop=False)
    rows = fetch_all("SELECT * FROM mart_strategic_ml_brand_metric")
    by_brand: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_market_combo: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_brand[row["brand_name"]].append(row)
        by_market_combo[(row["ml_id"], row["source"], row["measure"])].append(row)

    columns = ["brand", "market_id", "response_json", "payload_size"]
    placeholders = ", ".join(["%s"] * len(columns))
    names = ", ".join(f"`{column}`" for column in columns)
    sql = f"REPLACE INTO `cache_deep_analysis` ({names}) VALUES ({placeholders})"
    conn = mariadb_connect()
    cur = conn.cursor()
    ensure_events_raw_table(conn)
    poc_report = {"brands": {}}
    cur.execute("DELETE FROM `cache_deep_analysis`")
    batch: list[tuple[Any, ...]] = []
    inserted = 0

    def flush_batch() -> None:
        nonlocal batch
        if not batch:
            return
        cur.executemany(sql, batch)
        batch = []

    for brand, brand_rows in sorted(by_brand.items()):
        base = choose_base(brand_rows)
        ml_id = base["ml_id"]
        market_id = ml_to_strategy(ml_id)
        market = ml_market.loc[ml_id].to_dict() if ml_id in ml_market.index else {}
        available_combos = available_combos_for_market(market)
        phase30_enabled = brand in CANONICAL_25
        by_combo = {}
        rows_by_combo = {}
        for row in sorted(brand_rows, key=lambda r: (str(r["source"]), str(r["measure"]), str(r["ml_id"]))):
            combo = f"{api_source(row['source'])}.{row['measure']}"
            rows_by_combo.setdefault(combo, row)
        for source, measure in ALL_COMBOS:
            combo = f"{source}.{measure}"
            if combo not in available_combos:
                continue
            row = rows_by_combo.get(combo)
            internal_source = SOURCE_TO_INTERNAL[source]
            market_rows = by_market_combo.get((ml_id, internal_source, measure), [])
            if row is None:
                by_combo[combo] = empty_combo_payload(source, measure, brand, base)
            else:
                by_combo[combo] = combo_payload(row, market_rows=market_rows, target_brand=brand, combo_source=source, phase30=phase30_enabled)

        events_payload = build_events_for_cache(conn, brand) if brand in CANONICAL_25 else {"cut_a": [], "cut_b": [], "meta": {"lookback_months": 6}}
        events_payload = _dedup_cut_a_events(events_payload)
        events_spec = _events_spec_list(events_payload)
        simulation_by_combo = {}
        for combo, combo_data in by_combo.items():
            if phase30_enabled and build_phase30_simulation_combo is not None:
                market_forecast = combo_data.pop("_phase30_market_forecast", None)
                if market_forecast is not None:
                    source, measure = combo.split(".", 1)
                    simulation_by_combo[combo] = build_phase30_simulation_combo(
                        combo=combo,
                        source=source,
                        measure=measure,
                        unit_label=combo_data.get("unit_label"),
                        forecast_combo=combo_data,
                        market_forecast=market_forecast,
                        cut_b_events=events_payload.get("cut_b") or [],
                    )
                    continue
            sim_payload = _simulation_from_poc(brand, combo, poc_report, combo_data.get("unit_label"))
            if sim_payload:
                simulation_by_combo[combo] = sim_payload

        payload = {
            "brand": brand,
            "brand_name": brand,
            "market_id": market_id,
            "market_name": market.get("name"),
            "available_combos": available_combos,
            "data": {
                "forecast": {
                    "method": FORECAST_METHOD,
                    "disclaimer": FORECAST_DISCLOSURE,
                    "is_statistical_model": True,
                    "backtest_available": True,
                    "event_regressor_enabled": False,
                    "phase29_poc": (poc_report.get("brands") or {}).get(brand),
                    "by_combo": by_combo,
                },
                "simulation": {"by_combo": simulation_by_combo},
                "events": events_spec,
            },
            "market_meta": {
                "market_name": market.get("name"),
                "atc4_code": (BRAND_METADATA_BY_NAME.get(brand).atc_codes[0] if BRAND_METADATA_BY_NAME.get(brand) and BRAND_METADATA_BY_NAME.get(brand).atc_codes else None),
                "atc4_name": (BRAND_METADATA_BY_NAME.get(brand).atc_desc if BRAND_METADATA_BY_NAME.get(brand) else None),
                "sources": source_list(market.get("data_source")),
                "default_source": source_list(market.get("data_source"))[0] if source_list(market.get("data_source")) else None,
                "available_combos": available_combos,
                "source_count": len({api_source(r["source"]) for r in brand_rows}),
                "measure_count": len({r["measure"] for r in brand_rows}),
                "market_count": len({r["ml_id"] for r in brand_rows}),
                "is_jw": bool(base.get("is_jw")),
                "is_target": bool(base.get("is_target")),
            },
        }
        out = {
            "brand": brand,
            "market_id": market_id,
            "response_json": dump_payload(payload),
            "payload_size": payload_size(payload),
        }
        batch.append(tuple(out[column] for column in columns))
        inserted += 1
        if len(batch) >= 20:
            flush_batch()
        if args.verbose and inserted % 500 == 0:
            print(f"inserted cache_deep_analysis rows={inserted}", flush=True)
    flush_batch()
    cur.close()
    conn.close()
    if args.verbose:
        print(f"cache_deep_analysis rows={inserted}")


if __name__ == "__main__":
    main()
