"""Validation helpers for Keyword/Meeting overlap and isolated load evidence."""

from __future__ import annotations

from collections import Counter, defaultdict
from itertools import combinations
import json
from typing import Sequence

from pipeline.scripts.etl.brand_activity.km_core import (
    JsonValue,
    KeywordEvent,
    MeetingEvent,
    MessageCountCell,
    ProductPeriodEvent,
    language_bucket,
    normalize_key,
    text_sha256,
)


KmEvent = KeywordEvent | MeetingEvent


def _counter_to_json(counter: Counter[str]) -> dict[str, int]:
    """Convert a Counter to a stable JSON-friendly descending distribution."""
    return dict(sorted(counter.items(), key=lambda item: (-item[1], item[0])))


def _cells_by_comparison_key(cells: Sequence[MessageCountCell]) -> dict[tuple[str, str], MessageCountCell]:
    """Index Message Count cells by normalized product and month."""
    return {cell.comparison_key(): cell for cell in cells}


def compare_message_count_overlaps(message_sets: Sequence[Sequence[MessageCountCell]]) -> dict[str, JsonValue]:
    """Compare all source-file pairs for shared product/month Message Count values."""
    ordered_sets = sorted(
        [list(cells) for cells in message_sets],
        key=lambda cells: (cells[0].source_period_ym, cells[0].source_file) if cells else ("", ""),
    )
    compared_cells = 0
    matched_cells = 0
    mismatches: list[dict[str, JsonValue]] = []
    product_set_differences: list[dict[str, JsonValue]] = []
    pair_summaries: list[dict[str, JsonValue]] = []
    for left_cells, right_cells in combinations(ordered_sets, 2):
        if not left_cells or not right_cells:
            continue
        left_index = _cells_by_comparison_key(left_cells)
        right_index = _cells_by_comparison_key(right_cells)
        common_keys = sorted(left_index.keys() & right_index.keys(), key=lambda item: (item[1], item[0]))
        pair_compared = 0
        pair_matched = 0
        pair_mismatches = 0
        for key in common_keys:
            left = left_index[key]
            right = right_index[key]
            compared_cells += 1
            pair_compared += 1
            if left.value == right.value:
                matched_cells += 1
                pair_matched += 1
                continue
            pair_mismatches += 1
            mismatches.append(
                {
                    "kind": left.kind,
                    "product_name": left.product_name,
                    "month_ym": left.month_ym,
                    "left_file": left.source_file,
                    "left_value": left.value,
                    "right_file": right.source_file,
                    "right_value": right.value,
                }
            )
        left_products = {cell.product_key: cell.product_name for cell in left_cells}
        right_products = {cell.product_key: cell.product_name for cell in right_cells}
        left_only_keys = sorted(left_products.keys() - right_products.keys())
        right_only_keys = sorted(right_products.keys() - left_products.keys())
        if left_only_keys or right_only_keys:
            product_set_differences.append(
                {
                    "left_file": left_cells[0].source_file,
                    "right_file": right_cells[0].source_file,
                    "left_only_products": [left_products[key] for key in left_only_keys],
                    "right_only_products": [right_products[key] for key in right_only_keys],
                }
            )
        pair_summaries.append(
            {
                "kind": left_cells[0].kind,
                "left_file": left_cells[0].source_file,
                "right_file": right_cells[0].source_file,
                "common_cells": pair_compared,
                "matched_cells": pair_matched,
                "mismatch_cells": pair_mismatches,
                "left_products": len(left_products),
                "right_products": len(right_products),
            }
        )
    return {
        "file_pairs": len(pair_summaries),
        "compared_cells": compared_cells,
        "matched_cells": matched_cells,
        "mismatch_cells": len(mismatches),
        "product_set_difference_pairs": len(product_set_differences),
        "product_set_differences": product_set_differences,
        "mismatches": mismatches,
        "pair_summaries": pair_summaries,
    }


def compare_core_to_message_count(
    kind: str,
    events: Sequence[ProductPeriodEvent],
    message_cells: Sequence[MessageCountCell],
) -> dict[str, JsonValue]:
    """Compare source-file core event counts to same-file Message Count cells."""
    core_counts: Counter[tuple[str, str, str]] = Counter()
    product_names: dict[tuple[str, str, str], str] = {}
    for event in events:
        key = (event.source_file, normalize_key(event.product_name), event.period_ym)
        core_counts[key] += 1
        product_names[key] = event.product_name
    pivot_counts = {
        (cell.source_file, cell.product_key, cell.month_ym): cell.value
        for cell in message_cells
        if cell.kind == kind and cell.month_ym == cell.source_period_ym
    }
    mismatches: list[dict[str, JsonValue]] = []
    matched = 0
    missing_pivot = 0
    for key, core_count in sorted(core_counts.items(), key=lambda item: (item[0][0], item[0][2], item[0][1])):
        pivot_count = pivot_counts.get(key)
        if pivot_count is None:
            missing_pivot += 1
            mismatches.append(
                {
                    "kind": kind,
                    "source_file": key[0],
                    "product_name": product_names[key],
                    "month_ym": key[2],
                    "core_count": core_count,
                    "message_count": None,
                }
            )
            continue
        if pivot_count == core_count:
            matched += 1
            continue
        mismatches.append(
            {
                "kind": kind,
                "source_file": key[0],
                "product_name": product_names[key],
                "month_ym": key[2],
                "core_count": core_count,
                "message_count": pivot_count,
            }
        )
    return {
        "kind": kind,
        "checked_product_months": len(core_counts),
        "matched_product_months": matched,
        "mismatch_product_months": len(mismatches),
        "missing_message_count_cells": missing_pivot,
        "mismatches": mismatches,
    }


def period_distribution(events: Sequence[ProductPeriodEvent]) -> dict[str, int]:
    """Count events by normalized month for core-sheet single-month proof."""
    return _counter_to_json(Counter(event.period_ym for event in events))


def file_period_distribution(events: Sequence[ProductPeriodEvent]) -> dict[str, dict[str, int]]:
    """Count events by source file and normalized month."""
    file_counts: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for event in events:
        file_counts[event.source_file][event.period_ym] += 1
    return {source_file: _counter_to_json(counts) for source_file, counts in sorted(file_counts.items())}


def duplicate_hash_summary(events: Sequence[KmEvent]) -> dict[str, JsonValue]:
    """Count exact duplicate source rows by hash without exposing raw text."""
    hashes: Counter[str] = Counter()
    for event in events:
        row = {
            key: value
            for key, value in event.to_stage_row().items()
            if key not in {"source_file", "source_sheet", "source_row_no", "source_file_sha256"}
        }
        row_hash = text_sha256(json.dumps(row, ensure_ascii=False, sort_keys=True))
        hashes[row_hash] += 1
    duplicate_groups = {row_hash: count for row_hash, count in hashes.items() if count > 1}
    return {
        "total_rows": len(events),
        "unique_full_row_hashes": len(hashes),
        "duplicate_full_rows": sum(count - 1 for count in duplicate_groups.values()),
        "duplicate_groups": len(duplicate_groups),
    }


def keyword_enum_distribution(events: Sequence[KeywordEvent]) -> dict[str, dict[str, int]]:
    """Summarize Keyword enum fields for survey-alignment validation."""
    return {
        "interest": _counter_to_json(Counter(event.interest for event in events)),
        "prescription_frequency": _counter_to_json(Counter(event.prescription_frequency for event in events)),
        "prescription_evolution": _counter_to_json(Counter(event.prescription_evolution for event in events)),
        "abstract_lit": _counter_to_json(Counter(event.abstract_lit for event in events)),
        "patient_lit": _counter_to_json(Counter(event.patient_lit for event in events)),
        "promotional_lit": _counter_to_json(Counter(event.promotional_lit for event in events)),
        "samples_left": _counter_to_json(Counter(event.samples_left for event in events)),
        "other_materials_left": _counter_to_json(Counter(event.other_materials_left for event in events)),
    }


def meeting_enum_distribution(events: Sequence[MeetingEvent]) -> dict[str, dict[str, int]]:
    """Summarize Meeting enum fields for survey-alignment validation."""
    return {
        "meeting_format": _counter_to_json(Counter(event.meeting_format for event in events)),
        "therapeutic_class": _counter_to_json(Counter(event.therapeutic_class for event in events)),
        "prescription_frequency": _counter_to_json(Counter(event.prescription_frequency for event in events)),
        "prescription_evolution": _counter_to_json(Counter(event.prescription_evolution for event in events)),
        "interest": _counter_to_json(Counter(event.interest for event in events)),
    }


def text_field_summary(events: Sequence[KmEvent], field_name: str) -> dict[str, JsonValue]:
    """Summarize sensitive text by length and language without raw strings."""
    values: list[str] = []
    for event in events:
        match field_name:
            case "keyword_text":
                if isinstance(event, KeywordEvent):
                    values.append(event.keyword_text)
            case "meeting_topic":
                if isinstance(event, MeetingEvent):
                    values.append(event.meeting_topic)
            case "verbatim_message":
                if isinstance(event, MeetingEvent):
                    values.append(event.verbatim_message)
            case other:
                raise ValueError(f"unsupported sensitive text field: {other}")
    lengths = sorted(len(value) for value in values)
    language_counts = Counter(language_bucket(value) for value in values)
    if not lengths:
        return {"rows": 0, "language": {}, "min_len": 0, "median_len": 0, "p90_len": 0, "max_len": 0}
    median_index = len(lengths) // 2
    p90_index = min(len(lengths) - 1, int(len(lengths) * 0.9))
    return {
        "rows": len(lengths),
        "language": _counter_to_json(language_counts),
        "min_len": lengths[0],
        "median_len": lengths[median_index],
        "p90_len": lengths[p90_index],
        "max_len": lengths[-1],
    }
