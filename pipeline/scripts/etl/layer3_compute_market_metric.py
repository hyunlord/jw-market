"""Market-level JSON aggregation helpers for Layer 3 v3.1 load."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import math
from typing import Any

from dict_ubist_translation import translate_target_ubist


FIXED_IQVIA_CHANNELS = ("KHPA", "KCPA", "KPA")


def _periods(rows: list[dict[str, Any]]) -> list[str]:
    found: set[str] = set()
    for row in rows:
        found.update((row.get("raw_value_history") or {}).keys())
    return sorted(found)


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    text = str(value).strip()
    return text != "" and text.lower() not in {"nan", "none", "null"}


def _metric(row: dict[str, Any], period: str) -> dict[str, Any]:
    return (row.get("metric_history") or {}).get(period, {}) or {}


def compute_market_size_series(rows: list[dict[str, Any]]) -> dict[str, float]:
    series: dict[str, float] = defaultdict(float)
    for row in rows:
        for period, value in (row.get("raw_value_history") or {}).items():
            series[period] += float(value or 0)
    return dict(sorted(series.items()))


def compute_hhi_series(rows: list[dict[str, Any]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for period in _periods(rows):
        values = [float(_metric(row, period).get("raw_value") or 0) for row in rows]
        total = sum(values)
        if total <= 0:
            result[period] = 0.0
            continue
        result[period] = sum((value / total * 100) ** 2 for value in values if value > 0)
    return result


def compute_brand_ranking_stacked(rows: list[dict[str, Any]], top_n: int = 20) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for period in _periods(rows):
        items = []
        total = sum(float(_metric(row, period).get("raw_value") or 0) for row in rows)
        for row in rows:
            value = float(_metric(row, period).get("raw_value") or 0)
            if value <= 0:
                continue
            items.append(
                {
                    "brand_key": row.get("brand_key"),
                    "brand": row.get("brand_name"),
                    "rank": 0,
                    "raw_value": value,
                    "ms": value / total * 100 if total else 0.0,
                }
            )
        items.sort(key=lambda item: item["raw_value"], reverse=True)
        for idx, item in enumerate(items, start=1):
            item["rank"] = idx
        result[period] = items[:top_n]
    return result


def compute_company_ranking_stacked(rows: list[dict[str, Any]], top_n: int = 20) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for period in _periods(rows):
        company_values: dict[str, float] = defaultdict(float)
        for row in rows:
            dim = row.get("by_dimension") or {}
            company = dim.get("company") or dim.get("manufacturer") or "Unknown"
            company_values[str(company)] += float(_metric(row, period).get("raw_value") or 0)
        total = sum(company_values.values())
        ranked = []
        for idx, (company, value) in enumerate(sorted(company_values.items(), key=lambda kv: kv[1], reverse=True), start=1):
            if value <= 0:
                continue
            ranked.append({"company": company, "rank": idx, "raw_value": value, "ms": value / total * 100 if total else 0.0})
        result[period] = ranked[:top_n]
    return result


def compute_company_concentration_trend(company_ranking: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for period, ranking in company_ranking.items():
        shares = [float(item.get("ms") or 0) for item in ranking]
        result[period] = {
            "cr4": sum(shares[:4]),
            "cr10": sum(shares[:10]),
            "company_count": len(ranking),
        }
    return result


def compute_ei_ms_matrix(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        periods = sorted((row.get("metric_history") or {}).keys())
        if not periods:
            continue
        period = periods[-1]
        metric = _metric(row, period)
        ext = (row.get("extended_metric_history") or {}).get(period, {}) or {}
        result.append(
            {
                "brand_key": row.get("brand_key"),
                "brand": row.get("brand_name"),
                "period": period,
                "ms": metric.get("ms"),
                "ei_5y": ext.get("ei_5y"),
                "momentum_score": ext.get("momentum_score"),
            }
        )
    return result


def compute_growth_contribution_ms_matrix(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        periods = sorted((row.get("metric_history") or {}).keys())
        if not periods:
            continue
        period = periods[-1]
        metric = _metric(row, period)
        ext = (row.get("extended_metric_history") or {}).get(period, {}) or {}
        result.append(
            {
                "brand_key": row.get("brand_key"),
                "brand": row.get("brand_name"),
                "period": period,
                "ms": metric.get("ms"),
                "growth_contribution": ext.get("growth_contribution"),
                "growth_contribution_pct": ext.get("growth_contribution_pct"),
            }
        )
    return result


def compute_growth_contribution(rows: list[dict[str, Any]], top_n: int = 20) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for period in _periods(rows):
        items = []
        for row in rows:
            ext = (row.get("extended_metric_history") or {}).get(period, {}) or {}
            value = ext.get("growth_contribution")
            if value is None:
                continue
            items.append({"brand_key": row.get("brand_key"), "brand": row.get("brand_name"), "growth_contribution": value})
        items.sort(key=lambda item: abs(float(item["growth_contribution"] or 0)), reverse=True)
        result[period] = items[:top_n]
    return result


def compute_analysis_levels(rows: list[dict[str, Any]], catalog_market_row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not catalog_market_row:
        return None
    levels = {}
    for key in ("class", "molecule", "dosage_form", "strength_pack", "nhi_type", "ox_gx", "fish_oil"):
        flag = catalog_market_row.get(f"analyze_{key}")
        if not flag:
            continue
        values: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for row in rows:
            dim = row.get("by_dimension") or {}
            label = dim.get(key) or (row.get("overlay_data") or {}).get(key)
            if not label:
                continue
            for period, value in (row.get("raw_value_history") or {}).items():
                values[str(label)][period] += float(value or 0)
        levels[key] = {label: dict(sorted(series.items())) for label, series in values.items()}
    return levels


def compute_level_top5_trend(analysis_levels: dict[str, Any] | None) -> dict[str, Any] | None:
    if not analysis_levels:
        return None
    result = {}
    for level, labels in analysis_levels.items():
        periods = sorted({period for series in labels.values() for period in series.keys()})
        result[level] = {}
        for period in periods:
            ranked = sorted(
                ({"label": label, "raw_value": float(series.get(period) or 0)} for label, series in labels.items()),
                key=lambda item: item["raw_value"],
                reverse=True,
            )
            result[level][period] = ranked[:5]
    return result


def parse_target_label(label: str | None) -> tuple[str, str]:
    """Split ``종별 진료과`` into channel and specialty labels."""

    if not label:
        return "", ""
    if " " not in label:
        return label, ""
    channel, specialty = label.split(" ", 1)
    return channel, specialty


def aggregate_combo_for_period(rows: list[dict[str, Any]], channel: str, specialty: str, period: str) -> float:
    total = 0.0
    for row in rows:
        matrix = row.get("channel_specialty_matrix") or {}
        total += float(((matrix.get(channel) or {}).get(specialty) or {}).get(period) or 0)
    return total


def combo_total(rows: list[dict[str, Any]], channel: str, specialty: str) -> float:
    total = 0.0
    for row in rows:
        matrix = row.get("channel_specialty_matrix") or {}
        series = ((matrix.get(channel) or {}).get(specialty) or {})
        total += sum(float(value or 0) for value in series.values())
    return total


def combo_values_for_period(rows: list[dict[str, Any]], period: str) -> dict[tuple[str, str], float]:
    combo_values: dict[tuple[str, str], float] = defaultdict(float)
    for row in rows:
        matrix = row.get("channel_specialty_matrix") or {}
        for channel, specialties in matrix.items():
            for specialty, series in (specialties or {}).items():
                combo_values[(str(channel), str(specialty))] += float((series or {}).get(period) or 0)
    return combo_values


def pick_largest_label_among(raw_labels: list[str], rows: list[dict[str, Any]]) -> str | None:
    if not raw_labels:
        return None
    return max(raw_labels, key=lambda label: combo_total(rows, *parse_target_label(label)))


def compute_target_competition_ubist(rows: list[dict[str, Any]], catalog_market_row: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compute UBIST target customer competition with catalog rank priority.

    Catalog ``target_ubist_1`` through ``target_ubist_4`` are human decisions
    and keep their rank. Auto-computed combos only fill empty or
    untranslatable catalog slots, excluding every raw label candidate already
    represented by a catalog code such as ``CL IGF``.
    """

    catalog_fixed: dict[int, dict[str, Any]] = {}
    used_labels: set[str] = set()
    if catalog_market_row:
        for rank in range(1, 5):
            code_label = catalog_market_row.get(f"target_ubist_{rank}")
            if not _present(code_label):
                continue
            raw_candidates = translate_target_ubist(str(code_label))
            chosen = pick_largest_label_among(raw_candidates, rows)
            if raw_candidates:
                used_labels.update(raw_candidates)
            catalog_fixed[rank] = {
                "rank": rank,
                "label": chosen,
                "code_label": str(code_label),
                "source": "catalog",
                "raw_label_candidates": raw_candidates,
                "status": "ok" if chosen else "untranslatable",
            }

    history: dict[str, Any] = {}
    for period in _periods(rows):
        combo_values = combo_values_for_period(rows, period)
        period_total = sum(combo_values.values())
        top4_by_rank: dict[int, dict[str, Any]] = {}

        for rank, fixed in catalog_fixed.items():
            label = fixed.get("label")
            if not label:
                continue
            channel, specialty = parse_target_label(label)
            raw_value = aggregate_combo_for_period(rows, channel, specialty, period)
            top4_by_rank[rank] = {
                "rank": rank,
                "channel": channel,
                "specialty": specialty,
                "label": label,
                "source": "catalog",
                "code_label": fixed.get("code_label"),
                "raw_label_candidates": fixed.get("raw_label_candidates", []),
                "raw_value": raw_value,
                "ms": raw_value / period_total * 100 if period_total else 0.0,
            }

        auto_candidates = []
        for (channel, specialty), raw_value in sorted(combo_values.items(), key=lambda kv: kv[1], reverse=True):
            label = f"{channel} {specialty}"
            if label in used_labels:
                continue
            auto_candidates.append((channel, specialty, label, raw_value))

        for rank in range(1, 5):
            if rank in top4_by_rank:
                continue
            if not auto_candidates:
                break
            channel, specialty, label, raw_value = auto_candidates.pop(0)
            top4_by_rank[rank] = {
                "rank": rank,
                "channel": channel,
                "specialty": specialty,
                "label": label,
                "source": "computed",
                "raw_value": raw_value,
                "ms": raw_value / period_total * 100 if period_total else 0.0,
            }

        history[period] = {"top4": [top4_by_rank[rank] for rank in sorted(top4_by_rank)][:4]}

    latest_period = max(history) if history else None
    translated_catalog_count = sum(1 for item in catalog_fixed.values() if item.get("label"))
    if not catalog_fixed:
        source_type = "computed"
    elif translated_catalog_count == 4:
        source_type = "catalog"
    else:
        source_type = "mixed"
    return {
        "source_type": source_type,
        "history": history,
        "latest": {"period": latest_period, "top4": history.get(latest_period, {}).get("top4", [])} if latest_period else None,
        "catalog_definition": [
            {
                "rank": item["rank"],
                "code_label": item.get("code_label"),
                "raw_label_chosen": item.get("label"),
                "raw_label_candidates": item.get("raw_label_candidates", []),
                "status": item.get("status", "ok"),
            }
            for item in sorted(catalog_fixed.values(), key=lambda entry: entry["rank"])
        ],
    }


def compute_target_competition_iqvia(rows: list[dict[str, Any]], catalog_market_row: dict[str, Any] | None = None) -> dict[str, Any]:
    catalog_order: list[str] = []
    if catalog_market_row:
        for rank in range(1, 4):
            channel = catalog_market_row.get(f"target_iqvia_{rank}")
            if _present(channel) and str(channel) in FIXED_IQVIA_CHANNELS:
                catalog_order.append(str(channel))
    channels = catalog_order or list(FIXED_IQVIA_CHANNELS)
    history: dict[str, Any] = {}
    for period in _periods(rows):
        channel_values = {channel: 0.0 for channel in FIXED_IQVIA_CHANNELS}
        for row in rows:
            channel_data = row.get("channel_data") or {}
            for channel in FIXED_IQVIA_CHANNELS:
                channel_values[channel] += float(((channel_data.get(channel) or {}).get(period) or {}).get("raw_value") or 0)
        total = sum(channel_values.values())
        history[period] = {
            "distributions": [
                {
                    "rank": rank,
                    "channel": channel,
                    "raw_value": channel_values.get(channel, 0.0),
                    "ms": channel_values.get(channel, 0.0) / total * 100 if total else 0.0,
                    "source": "catalog" if catalog_order else "computed_fixed",
                }
                for rank, channel in enumerate(channels, start=1)
            ]
        }
    latest_period = max(history) if history else None
    source_type = "catalog" if catalog_order else "computed_fixed"
    return {
        "source_type": source_type,
        "channels": channels,
        "history": history,
        "latest": {"period": latest_period, "distributions": history.get(latest_period, {}).get("distributions", [])}
        if latest_period
        else None,
        "catalog_definition": [
            {"rank": rank, "channel": channel, "source": source_type}
            for rank, channel in enumerate(channels, start=1)
        ],
    }


def compute_market_mart_payload(
    rows: list[dict[str, Any]],
    source: str,
    measure: str,
    view_type: str,
    catalog_market_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    company_ranking = compute_company_ranking_stacked(rows)
    analysis_levels = compute_analysis_levels(rows, catalog_market_row) if view_type != "general" else None
    level_top5 = compute_level_top5_trend(analysis_levels) if analysis_levels else None
    payload = {
        "view_type": view_type,
        "etl_version": "v3.1",
        "computed_at": datetime.now().isoformat(timespec="seconds"),
        "brand_rows": len(rows),
    }
    if view_type == "general":
        payload["analysis_levels_status"] = "not_applicable"
        payload["level_top5_trend_status"] = "not_applicable"
    target = (
        compute_target_competition_ubist(rows, catalog_market_row)
        if source == "ubist"
        else compute_target_competition_iqvia(rows, catalog_market_row)
    )
    return {
        "market_size_series": compute_market_size_series(rows),
        "hhi_series_5y": compute_hhi_series(rows),
        "brand_ranking_stacked": compute_brand_ranking_stacked(rows),
        "company_ranking_stacked": company_ranking,
        "company_concentration_trend": compute_company_concentration_trend(company_ranking),
        "ei_ms_matrix": compute_ei_ms_matrix(rows),
        "growth_contribution_ms_matrix": compute_growth_contribution_ms_matrix(rows),
        "growth_contribution": compute_growth_contribution(rows),
        "analysis_levels": analysis_levels,
        "level_top5_trend": level_top5,
        "target_customer_competition": target,
        "payload": payload,
    }
