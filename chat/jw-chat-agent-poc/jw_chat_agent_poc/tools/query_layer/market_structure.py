from __future__ import annotations

from collections import Counter
from typing import Any, Final

from jw_chat_agent_poc.tools.query_layer.store import MartRecord, MartSnapshot


CLASS_1_KEY: Final[str] = "class_1"
CLASS_2_KEY: Final[str] = "class_2"


def market_structure(snapshot: MartSnapshot, market: str, source: str = "ubist") -> dict[str, Any]:
    """Return registry-style market structure metadata inferred from mart dimensions."""

    records = snapshot.market_records(market, source, "sales")
    return structure_from_records(records)


def structure_from_records(records: tuple[MartRecord, ...]) -> dict[str, Any]:
    """Infer whether a market has a split Class 1/Class 2 structure.

    Class 1 is preserved as catalog metadata. The user-facing grouping axis is
    Class 2 because ml_011 is operated with Class 2-only exposure.
    """

    class_1_values = _dimension_counts(records, CLASS_1_KEY)
    class_2_values = _dimension_counts(records, CLASS_2_KEY)
    if not class_1_values or not class_2_values:
        return {}
    return {
        "type": "class_split",
        "axes": (
            {"key": CLASS_1_KEY, "label": "Class 1", "exposure": "catalog_only", "values": _values(class_1_values)},
            {"key": CLASS_2_KEY, "label": "Class 2", "exposure": "display", "values": _values(class_2_values)},
        ),
        "display_axis": CLASS_2_KEY,
        "display_axis_label": "Class 2",
        "display_denominator": sum(class_2_values.values()),
        "class2_only_exposure": True,
        "comparison_guardrail": "전체 market_landscape 분모와 Class 기준 분모는 직접 비교하지 않음",
    }


def _dimension_counts(records: tuple[MartRecord, ...], key: str) -> Counter[str]:
    values: Counter[str] = Counter()
    for record in records:
        value = str(record.by_dimension.get(key) or "").strip()
        if value:
            values[value] += 1
    return values


def _values(counts: Counter[str]) -> tuple[dict[str, Any], ...]:
    return tuple({"name": name, "brand_count": count} for name, count in sorted(counts.items()))
