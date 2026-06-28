from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.compose_ab_poc.questions import EvalQuestion
from scripts.fact_scoreboard.gold import GoldStore, _nested, _period_value
from scripts.fact_scoreboard.scoring import GoldFact
from scripts.fact_scoreboard.text_numbers import NumericUnit

# noqa: SIZE_OK — single trace-to-calibrated-fact mapper kept together for audit reviewability.


@dataclass(frozen=True, slots=True)
class PopulationChoice:
    """Population chosen by comparing operating trace values with independent mart aggregates."""

    population: str
    intent_aligned: bool
    note: str


@dataclass(frozen=True, slots=True)
class CalibratedQuestion:
    """Calibrated gold and trace facts for one operating answer."""

    question: EvalQuestion
    facts: tuple[GoldFact, ...]
    trace_facts: tuple[GoldFact, ...]
    schema_execution_ok: bool
    schema_intent_ok: bool
    population_notes: tuple[str, ...]


def choose_molecule_population(
    trace_segments: dict[str, float],
    whole_market: dict[str, float],
    channel_market: dict[str, float],
    *,
    requested_channel: str,
) -> PopulationChoice:
    """Choose the mart population whose shares match the operating molecule trace."""

    whole_error = _segment_error(trace_segments, whole_market)
    channel_error = _segment_error(trace_segments, channel_market)
    if whole_error <= channel_error:
        aligned = requested_channel == ""
        note = "matched whole market"
        if requested_channel:
            note = f"requested channel {requested_channel!r}, but returned values match whole market"
        return PopulationChoice("whole_market", aligned, note)
    return PopulationChoice(f"channel:{requested_channel}", True, f"matched requested channel {requested_channel!r}")


def calibrate_question(store: GoldStore, question: EvalQuestion, trace_path: Path) -> CalibratedQuestion:
    """Build calibrated facts from operating trace labels, recomputed from mart."""

    payload = json.loads(trace_path.read_text(encoding="utf-8"))
    calls = payload.get("tool_calls") if isinstance(payload, dict) else []
    facts: list[GoldFact] = []
    trace_facts: list[GoldFact] = []
    notes: list[str] = []
    for call in calls if isinstance(calls, list) else []:
        if not isinstance(call, dict):
            continue
        render_data = call.get("render_data")
        if isinstance(render_data, dict):
            _facts_from_render(store, question.qid, render_data, facts, trace_facts, notes)
    facts = _dedupe(facts)
    trace_facts = _dedupe(trace_facts)
    execution_ok = bool(trace_facts) or _has_unsupported(calls)
    intent_ok, intent_note = _intent_ok(question.intent_id, facts, trace_facts, notes, calls)
    if intent_note:
        notes.append(intent_note)
    return CalibratedQuestion(question, facts, trace_facts, execution_ok, intent_ok, tuple(notes))


def _facts_from_render(
    store: GoldStore,
    qid: str,
    data: dict[str, Any],
    facts: list[GoldFact],
    trace_facts: list[GoldFact],
    notes: list[str],
) -> None:
    period = _period_or_latest(store, data.get("period"))
    brand = str(data.get("brand") or "")
    if brand in store.by_brand:
        _add_brand_snapshot(store, qid, brand, period, data, facts, trace_facts)
        _add_brand_series(store, qid, brand, data.get("brand_value_series_10pt"), facts, trace_facts)
    _add_market_snapshot(store, qid, period, data, facts, trace_facts)
    _add_market_series(store, qid, data.get("market_size_series"), facts, trace_facts)
    _add_level_segments(store, qid, period, data, facts, trace_facts, notes)
    _add_top_trend(store, qid, data, facts, trace_facts)
    _add_sales_delta(store, qid, data, facts, trace_facts)
    _add_market_vs_brand_delta(store, qid, data, facts, trace_facts)
    _add_brand_trend_comparison(store, qid, data, facts, trace_facts)


def _add_brand_snapshot(
    store: GoldStore,
    qid: str,
    brand: str,
    period: str,
    data: dict[str, Any],
    facts: list[GoldFact],
    trace_facts: list[GoldFact],
) -> None:
    if "sales_억원" in data:
        _add_pair(qid, f"brand:{brand}:{period}:sales", f"{brand} {period} sales", store._eok(store._value(brand, period)), data.get("sales_억원"), "eok", facts, trace_facts)
    if "ms_recent_pct" in data:
        _add_pair(qid, f"brand:{brand}:{period}:share", f"{brand} {period} share", store._share(brand, period), data.get("ms_recent_pct"), "percent", facts, trace_facts)
    if "rank" in data and store._rank(brand, period) is not None:
        _add_pair(qid, f"brand:{brand}:{period}:rank", f"{brand} {period} rank", float(store._rank(brand, period) or 0), data.get("rank"), "rank", facts, trace_facts)
    if "total_brands_in_market" in data:
        facts.append(GoldFact(f"{qid}:market:{period}:brand_count", f"market {period} brand count", float(len(store._ranked(period))), "count", qid, False))
    if "brand_cagr_5y_pct" in data:
        _add_pair(qid, f"brand:{brand}:cagr5y", f"{brand} 5y CAGR", _brand_cagr(store, brand), data.get("brand_cagr_5y_pct"), "percent", facts, trace_facts)


def _add_brand_series(store: GoldStore, qid: str, brand: str, raw_series: Any, facts: list[GoldFact], trace_facts: list[GoldFact]) -> None:
    if not isinstance(raw_series, list):
        return
    for item in raw_series:
        if not isinstance(item, dict) or not isinstance(item.get("period"), str):
            continue
        period = str(item["period"])
        _add_pair(qid, f"series:{brand}:{period}:sales", f"{brand} {period} sales", store._eok(store._value(brand, period)), item.get("value_억원"), "eok", facts, trace_facts)
        _add_pair(qid, f"series:{brand}:{period}:share", f"{brand} {period} share", store._share(brand, period), item.get("ms_pct"), "percent", facts, trace_facts)
        rank = store._rank(brand, period)
        if rank is not None:
            _add_pair(qid, f"series:{brand}:{period}:rank", f"{brand} {period} rank", float(rank), item.get("rank"), "rank", facts, trace_facts)


def _add_market_snapshot(store: GoldStore, qid: str, period: str, data: dict[str, Any], facts: list[GoldFact], trace_facts: list[GoldFact]) -> None:
    if "market_size_억원" in data:
        _add_pair(qid, f"market:{period}:sales", f"market {period} sales", store._eok(store._market(period)), data.get("market_size_억원"), "eok", facts, trace_facts)
    if "market_cagr_5y_pct" in data:
        _add_pair(qid, "market:cagr5y", "market 5y CAGR", _market_cagr(store), data.get("market_cagr_5y_pct"), "percent", facts, trace_facts)
    if "hhi_recent" in data:
        _add_pair(qid, f"market:{period}:hhi", f"market {period} HHI", _hhi(store, period), data.get("hhi_recent"), "plain", facts, trace_facts)


def _add_market_series(store: GoldStore, qid: str, raw_series: Any, facts: list[GoldFact], trace_facts: list[GoldFact]) -> None:
    if not isinstance(raw_series, list):
        return
    for item in raw_series[-10:]:
        if isinstance(item, dict) and isinstance(item.get("period"), str):
            period = str(item["period"])
            _add_pair(qid, f"market:{period}:sales", f"market {period} sales", store._eok(store._market(period)), item.get("value_억원"), "eok", facts, trace_facts)


def _add_level_segments(
    store: GoldStore,
    qid: str,
    period: str,
    data: dict[str, Any],
    facts: list[GoldFact],
    trace_facts: list[GoldFact],
    notes: list[str],
) -> None:
    segments = data.get("level_segments")
    if not isinstance(segments, list):
        return
    level = str(data.get("level") or "Brand")
    if level == "Molecule":
        _add_molecule_segments(store, qid, period, data, segments, facts, trace_facts, notes)
        return
    if level != "Brand":
        _add_generic_level_segments(store, qid, period, data, segments, facts, trace_facts)
        return
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        brand = str(segment.get("brand") or segment.get("name") or "")
        if brand in store.by_brand:
            _add_pair(qid, f"level:{brand}:{period}:rank", f"{brand} latest rank", float(store._rank(brand, period) or 0), segment.get("rank"), "rank", facts, trace_facts)
            _add_pair(qid, f"level:{brand}:{period}:share", f"{brand} latest share", store._share(brand, period), segment.get("ms_recent_pct"), "percent", facts, trace_facts)
            _add_pair(qid, f"level:{brand}:{period}:sales", f"{brand} latest sales", store._eok(store._value(brand, period)), segment.get("value_억원"), "eok", facts, trace_facts)


def _add_molecule_segments(
    store: GoldStore,
    qid: str,
    period: str,
    data: dict[str, Any],
    segments: list[Any],
    facts: list[GoldFact],
    trace_facts: list[GoldFact],
    notes: list[str],
) -> None:
    trace_shares = {str(item.get("name")): float(item.get("ms_recent_pct")) for item in segments if isinstance(item, dict) and isinstance(item.get("ms_recent_pct"), int | float)}
    applied = data.get("applied_filters")
    filters = applied if isinstance(applied, dict) else {}
    requested_channel = str(filters.get("channel") or "")
    population = _filter_label(filters)
    if requested_channel:
        whole = _group_shares(store, "molecule", period, {})
        channel = _group_shares(store, "molecule", period, {"channel": requested_channel})
        choice = choose_molecule_population(trace_shares, whole, channel, requested_channel=requested_channel)
        notes.append(choice.note)
        filters = {"channel": requested_channel} if choice.population.startswith("channel:") else {}
        population = choice.population
    values = _group_values(store, "molecule", period, filters)
    total = sum(values.values())
    ranked = sorted(values.items(), key=lambda item: item[1], reverse=True)
    ranks = {name: rank for rank, (name, _value) in enumerate(ranked, start=1)}
    for item in segments:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        value = values.get(name, 0.0)
        share = value / total * 100 if total else 0.0
        _add_pair(qid, f"molecule:{population}:{name}:rank", f"Molecule {name} rank", float(ranks.get(name, 0)), item.get("rank"), "rank", facts, trace_facts)
        _add_pair(qid, f"molecule:{population}:{name}:share", f"Molecule {name} share", share, item.get("ms_recent_pct"), "percent", facts, trace_facts)
        _add_pair(qid, f"molecule:{population}:{name}:sales", f"Molecule {name} sales", store._eok(value), item.get("value_억원"), "eok", facts, trace_facts)


def _add_generic_level_segments(
    store: GoldStore,
    qid: str,
    period: str,
    data: dict[str, Any],
    segments: list[Any],
    facts: list[GoldFact],
    trace_facts: list[GoldFact],
) -> None:
    level = _level_key(data.get("level"))
    if not level:
        return
    filters = _applied_filters(data)
    values = _group_values(store, level, period, filters)
    ranks = _ranks(values)
    for item in segments:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("brand") or "")
        value = values.get(name, 0.0)
        denominator = _group_denominator(store, level, name, period, filters, values)
        share = value / denominator * 100 if denominator else 0.0
        prefix = f"level:{level}:{_filter_label(filters)}:{name}"
        _add_pair(qid, f"{prefix}:rank", f"{level} {name} rank", float(ranks.get(name, 0)), item.get("rank"), "rank", facts, trace_facts)
        _add_pair(qid, f"{prefix}:share", f"{level} {name} share", share, item.get("ms_recent_pct"), "percent", facts, trace_facts)
        _add_pair(qid, f"{prefix}:sales", f"{level} {name} sales", store._eok(value), item.get("value_억원"), "eok", facts, trace_facts)


def _add_top_trend(store: GoldStore, qid: str, data: dict[str, Any], facts: list[GoldFact], trace_facts: list[GoldFact]) -> None:
    raw_series = data.get("level_top5_trend_series")
    if not isinstance(raw_series, list):
        return
    level = _level_key(data.get("level"))
    filters = _applied_filters(data)
    for item in raw_series:
        if not isinstance(item, dict):
            continue
        brand = str(item.get("brand") or "")
        if brand not in store.by_brand:
            if level:
                _add_group_trend(store, qid, level, brand or str(item.get("name") or ""), item, filters, facts, trace_facts)
            continue
        _add_brand_series(store, qid, brand, item.get("series"), facts, trace_facts)
        series = item.get("series")
        if isinstance(series, list) and series:
            first = series[0] if isinstance(series[0], dict) else {}
            last = series[-1] if isinstance(series[-1], dict) else {}
            if isinstance(first.get("period"), str) and isinstance(last.get("period"), str):
                start = str(first["period"])
                end = str(last["period"])
                _add_pair(qid, f"trend:{brand}:sales_delta", f"{brand} sales delta", store._eok(store._value(brand, end) - store._value(brand, start)), item.get("value_delta_억원"), "eok", facts, trace_facts)
                _add_pair(qid, f"trend:{brand}:share_delta", f"{brand} share delta", store._share(brand, end) - store._share(brand, start), item.get("share_delta_pctp"), "percent", facts, trace_facts)


def _add_group_trend(
    store: GoldStore,
    qid: str,
    level: str,
    label: str,
    item: dict[str, Any],
    filters: dict[str, Any],
    facts: list[GoldFact],
    trace_facts: list[GoldFact],
) -> None:
    series = item.get("series")
    if not label or not isinstance(series, list):
        return
    prefix = f"trend:{level}:{_filter_label(filters)}:{label}"
    periods: list[str] = []
    for point in series:
        if not isinstance(point, dict) or not isinstance(point.get("period"), str):
            continue
        period = str(point["period"])
        values = _group_values(store, level, period, filters)
        value = values.get(label, 0.0)
        denominator = _group_denominator(store, level, label, period, filters, values)
        share = value / denominator * 100 if denominator else 0.0
        periods.append(period)
        _add_pair(qid, f"{prefix}:{period}:sales", f"{level} {label} {period} sales", store._eok(value), point.get("value_억원"), "eok", facts, trace_facts)
        _add_pair(qid, f"{prefix}:{period}:share", f"{level} {label} {period} share", share, point.get("ms_pct"), "percent", facts, trace_facts)
    if len(periods) >= 2:
        start, end = periods[0], periods[-1]
        start_values = _group_values(store, level, start, filters)
        end_values = _group_values(store, level, end, filters)
        start_value = start_values.get(label, 0.0)
        end_value = end_values.get(label, 0.0)
        start_share = start_value / _group_denominator(store, level, label, start, filters, start_values) * 100 if _group_denominator(store, level, label, start, filters, start_values) else 0.0
        end_share = end_value / _group_denominator(store, level, label, end, filters, end_values) * 100 if _group_denominator(store, level, label, end, filters, end_values) else 0.0
        _add_pair(qid, f"{prefix}:sales_delta", f"{level} {label} sales delta", store._eok(end_value - start_value), item.get("value_delta_억원"), "eok", facts, trace_facts)
        _add_pair(qid, f"{prefix}:share_delta", f"{level} {label} share delta", end_share - start_share, item.get("share_delta_pctp"), "percent", facts, trace_facts)


def _add_sales_delta(store: GoldStore, qid: str, data: dict[str, Any], facts: list[GoldFact], trace_facts: list[GoldFact]) -> None:
    if data.get("metric") not in {"sales_delta", "yoy_growth"}:
        return
    brand = str(data.get("brand") or "")
    period = str(data.get("period") or "")
    if brand not in store.by_brand or "→" not in period:
        return
    start, end = period.split("→", 1)
    start_value = store._value(brand, start)
    end_value = store._value(brand, end)
    delta = end_value - start_value
    pct = delta / start_value * 100 if start_value else 0.0
    _add_pair(qid, f"delta:{brand}:{period}:sales", f"{brand} sales delta {period}", store._eok(delta), data.get("sales_delta_억원"), "eok", facts, trace_facts)
    _add_pair(qid, f"delta:{brand}:{period}:pct", f"{brand} sales delta pct {period}", pct, data.get("sales_delta_pct", data.get("growth_pct")), "percent", facts, trace_facts)


def _add_market_vs_brand_delta(store: GoldStore, qid: str, data: dict[str, Any], facts: list[GoldFact], trace_facts: list[GoldFact]) -> None:
    if data.get("metric") != "market_vs_brand_delta":
        return
    brand = str(data.get("brand") or "")
    if brand not in store.by_brand:
        return
    start = str(data.get("from_period") or "")
    end = str(data.get("to_period") or "")
    if start not in store.periods or end not in store.periods:
        return
    brand_start = store._value(brand, start)
    market_start = store._market(start)
    brand_delta = store._value(brand, end) - brand_start
    market_delta = store._market(end) - market_start
    brand_pct = (store._value(brand, end) / brand_start - 1) * 100 if brand_start else 0.0
    market_pct = (store._market(end) / market_start - 1) * 100 if market_start else 0.0
    _add_pair(qid, "brand Jan-Feb sales delta", "brand Jan-Feb sales delta", store._eok(brand_delta), data.get("brand_sales_delta_억원"), "eok", facts, trace_facts)
    _add_pair(qid, "market Jan-Feb sales delta", "market Jan-Feb sales delta", store._eok(market_delta), data.get("market_sales_delta_억원"), "eok", facts, trace_facts)
    _add_pair(qid, "brand Jan-Feb sales pct change", "brand Jan-Feb sales pct change", brand_pct, data.get("brand_delta_pct"), "percent", facts, trace_facts)
    _add_pair(qid, "market Jan-Feb sales pct change", "market Jan-Feb sales pct change", market_pct, data.get("market_delta_pct"), "percent", facts, trace_facts)
    _add_pair(qid, "brand-market Jan-Feb pct gap", "brand-market Jan-Feb pct gap", brand_pct - market_pct, data.get("delta_pct_gap"), "percent", facts, trace_facts)


def _add_brand_trend_comparison(store: GoldStore, qid: str, data: dict[str, Any], facts: list[GoldFact], trace_facts: list[GoldFact]) -> None:
    if data.get("metric") != "brand_trend_comparison":
        return
    brand = str(data.get("brand") or "")
    comparison = str(data.get("comparison_brand") or "")
    start = str(data.get("from_period") or "")
    end = str(data.get("to_period") or "")
    if brand not in store.by_brand or comparison not in store.by_brand or start not in store.periods or end not in store.periods:
        return
    brand_start = store._value(brand, start)
    comparison_start = store._value(comparison, start)
    _add_pair(qid, f"trend_compare {brand} share delta", f"trend_compare {brand} share delta", store._share(brand, end) - store._share(brand, start), data.get("brand_share_delta_pctp"), "percent", facts, trace_facts)
    _add_pair(qid, f"trend_compare {comparison} share delta", f"trend_compare {comparison} share delta", store._share(comparison, end) - store._share(comparison, start), data.get("comparison_share_delta_pctp"), "percent", facts, trace_facts)
    _add_pair(qid, f"trend_compare {brand} sales pct", f"trend_compare {brand} sales pct", (store._value(brand, end) / brand_start - 1) * 100 if brand_start else 0.0, data.get("brand_sales_delta_pct"), "percent", facts, trace_facts)
    _add_pair(qid, f"trend_compare {comparison} sales pct", f"trend_compare {comparison} sales pct", (store._value(comparison, end) / comparison_start - 1) * 100 if comparison_start else 0.0, data.get("comparison_sales_delta_pct"), "percent", facts, trace_facts)


def _add_pair(qid: str, key: str, label: str, gold_value: float, trace_value: Any, unit: NumericUnit, facts: list[GoldFact], trace_facts: list[GoldFact]) -> None:
    facts.append(GoldFact(f"{qid}:{key}", label, gold_value, unit, qid, False))
    if isinstance(trace_value, int | float):
        trace_facts.append(GoldFact(f"{qid}:{key}", label, float(trace_value), unit, qid, False))


def _intent_ok(intent_id: str, facts: tuple[GoldFact, ...], trace_facts: tuple[GoldFact, ...], notes: list[str], calls: Any) -> tuple[bool, str]:
    if any("requested channel" in note and "whole market" in note for note in notes):
        return False, "population mismatch: channel filter label did not match returned molecule population"
    match intent_id:
        case "market_vs_brand_feb":
            return _check(_has_metric(calls, "market_vs_brand_delta"), "market-vs-brand February comparison requires calibrated change-rate fact")
        case "livaro_atozet_channel_diff":
            return _check(_has_two_brand_channel_queries(calls), "channel comparison requires 리바로 and 아토젯 channel query specs")
        case "ox_gx_mix":
            return _check(_has_query_level(calls, "ox_gx"), "ox_gx composition requires ox_gx query spec")
        case "top_competitor_specialty_sales":
            return _check(_has_query_level(calls, "specialty", requires_filter="brand"), "competitor specialty sales requires specialty query specs filtered by brand")
        case "class_sales_trend_12m":
            return _check(_has_query_level(calls, "dosage_form", requires_trend=True), "class/dosage trend requires dosage_form period trend")
        case "top_company_molecule":
            return _check(_has_query_level(calls, "company") and _has_query_level(calls, "Molecule", requires_filter="company"), "company top3 plus molecule requires company and company-filtered molecule query specs")
        case "nhi_mix_trend":
            return _check(_has_unsupported(calls) and not _has_metric_query(calls), "nhi_type absent should return unsupported without generic metrics")
        case "news_sales_effect":
            return _check(_has_unsupported(calls) and not _has_metric_query(calls), "causal news-sales effect should return unsupported without generic metrics")
        case "livaro_yoy_growth":
            return _check(_has_metric(calls, "yoy_growth"), "YoY growth requires year-over-year derived metric")
        case "livaro_avg_share_6m":
            return _check(_has_metric(calls, "average_share"), "six-month average requires average_share derived metric")
        case _:
            return bool(facts or trace_facts or _has_unsupported(calls)), ""


def _has_unsupported(calls: Any) -> bool:
    return isinstance(calls, list) and any(isinstance(call, dict) and call.get("tool") == "unsupported_metric" for call in calls)


def _check(ok: bool, message: str) -> tuple[bool, str]:
    return (ok, "" if ok else message)


def _has_metric_query(calls: Any) -> bool:
    return any(data.get("metric") == "query_spec" for data in _render_items(calls))


def _has_metric(calls: Any, metric: str) -> bool:
    return any(data.get("metric") == metric for data in _render_items(calls))


def _has_query_level(calls: Any, level: str, *, requires_filter: str = "", requires_trend: bool = False) -> bool:
    for data in _render_items(calls):
        if str(data.get("level") or "") != level:
            continue
        filters = data.get("applied_filters")
        if requires_filter and (not isinstance(filters, dict) or not filters.get(requires_filter)):
            continue
        if requires_trend and not isinstance(data.get("level_top5_trend_series"), list):
            continue
        return True
    return False


def _has_two_brand_channel_queries(calls: Any) -> bool:
    brands = set()
    for data in _render_items(calls):
        if str(data.get("level") or "") != "channel":
            continue
        filters = data.get("applied_filters")
        if isinstance(filters, dict) and filters.get("brand"):
            brands.add(str(filters["brand"]))
    return {"리바로", "아토젯"}.issubset(brands)


def _render_items(calls: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(calls, list):
        return ()
    items: list[dict[str, Any]] = []
    for call in calls:
        data = call.get("render_data") if isinstance(call, dict) else None
        if isinstance(data, dict):
            items.append(data)
    return tuple(items)


def _group_values(store: GoldStore, key: str, period: str, filters: dict[str, Any] | str) -> dict[str, float]:
    if isinstance(filters, str):
        filters = {"channel": filters} if filters else {}
    values: dict[str, float] = {}
    for row in store.rows:
        if not _row_matches(row, filters):
            continue
        for group, value in _row_group_values(store, row, key, period, filters):
            values[group] = values.get(group, 0.0) + value
    return values


def _group_shares(store: GoldStore, key: str, period: str, filters: dict[str, Any] | str) -> dict[str, float]:
    values = _group_values(store, key, period, filters)
    total = sum(values.values())
    return {name: value / total * 100 for name, value in values.items()} if total else {}


def _row_group_values(store: GoldStore, row, key: str, period: str, filters: dict[str, Any]) -> list[tuple[str, float]]:
    if key == "channel":
        return [(name, _period_value(history, period)) for name, history in row.channel_data.items() if isinstance(history, dict)]
    if key == "specialty":
        return [(name, _period_value(history, period)) for name, history in row.specialty_data.items() if isinstance(history, dict)]
    return [(_dimension_label(row, key), _row_value(store, row, period, filters))]


def _row_value(store: GoldStore, row, period: str, filters: dict[str, Any]) -> float:
    channel = str(filters.get("channel") or "")
    if channel:
        return _period_value(_nested(row.channel_data, channel), period)
    specialty = str(filters.get("specialty") or "")
    if specialty:
        return _period_value(_nested(row.specialty_data, specialty), period)
    return store._value(row.brand, period)


def _group_denominator(store: GoldStore, key: str, label: str, period: str, filters: dict[str, Any], grouped: dict[str, float]) -> float:
    if key == "channel" and filters.get("brand"):
        return sum(_period_value(_nested(row.channel_data, label), period) for row in store.rows)
    if key == "specialty" and filters.get("brand"):
        return sum(_period_value(_nested(row.specialty_data, label), period) for row in store.rows)
    return sum(grouped.values())


def _row_matches(row, filters: dict[str, Any]) -> bool:
    for key in ("brand", "company", "molecule", "dosage_form", "nhi_type", "ox_gx"):
        expected = str(filters.get(key) or "")
        if not expected:
            continue
        actual = row.brand if key == "brand" else _dimension_label(row, key)
        if actual != expected:
            return False
    return True


def _ranks(values: dict[str, float]) -> dict[str, int]:
    return {name: index for index, (name, _value) in enumerate(sorted(values.items(), key=lambda item: item[1], reverse=True), start=1)}


def _level_key(value: Any) -> str:
    text = str(value or "")
    return {"Molecule": "molecule", "Brand": "product"}.get(text, text)


def _dimension_label(row, key: str) -> str:
    if key == "dosage_form":
        return str(row.by_dimension.get("dosage_form") or row.by_dimension.get("class") or "unknown")
    return str(row.by_dimension.get(key) or "unknown")


def _applied_filters(data: dict[str, Any]) -> dict[str, Any]:
    filters = data.get("applied_filters")
    return dict(filters) if isinstance(filters, dict) else {}


def _filter_label(filters: dict[str, Any]) -> str:
    if not filters:
        return "all"
    return "|".join(f"{key}={value}" for key, value in sorted(filters.items()))


def _segment_error(trace_segments: dict[str, float], candidate: dict[str, float]) -> float:
    return sum(abs(value - candidate.get(name, 0.0)) for name, value in trace_segments.items())


def _brand_cagr(store: GoldStore, brand: str) -> float:
    start = f"{int(store.latest[:4]) - 5}{store.latest[4:]}"
    base = store._value(brand, start)
    return ((store._value(brand, store.latest) / base) ** (1 / 5) - 1) * 100 if base else 0.0


def _market_cagr(store: GoldStore) -> float:
    start = f"{int(store.latest[:4]) - 5}{store.latest[4:]}"
    base = store._market(start)
    return ((store._market(store.latest) / base) ** (1 / 5) - 1) * 100 if base else 0.0


def _hhi(store: GoldStore, period: str) -> float:
    return sum(float(row["share"]) ** 2 for row in store._ranked(period))


def _period_or_latest(store: GoldStore, value: Any) -> str:
    text = str(value or "")
    return text if text in store.periods else store.latest


def _dedupe(facts: list[GoldFact]) -> tuple[GoldFact, ...]:
    deduped: dict[str, GoldFact] = {}
    for fact in facts:
        deduped[fact.fact_id] = fact
    return tuple(deduped.values())
