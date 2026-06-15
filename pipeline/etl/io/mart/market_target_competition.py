from __future__ import annotations

from collections import defaultdict
from typing import Any

from .dict_ubist_translation import translate_target_ubist

FIXED_IQVIA_CHANNELS = ("KHPA", "KCPA", "KPA")


def _periods(rows: list[dict[str, Any]]) -> list[str]:
    found: set[str] = set()
    for row in rows:
        found.update((row.get("raw_value_history") or {}).keys())
    return sorted(found)


def _present(value: Any) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    return text != "" and text.lower() not in {"nan", "none", "null"}


def _metric(row: dict[str, Any], period: str) -> dict[str, Any]:
    return (row.get("metric_history") or {}).get(period, {}) or {}

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
