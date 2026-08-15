from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Any

from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceRecord, EvidenceSet
from jw_chat_agent_poc.service.v4.source_labels import public_source_label


_PERIOD_FIELDS = ("period", "month", "quarter", "year", "date")
_VALUE_FIELDS = (
    ("sales", "매출"),
    ("sales_krw", "매출"),
    ("market_share", "점유율"),
    ("patient_count", "환자수"),
    ("amount_krw", "금액"),
)
_SERIES_FIELDS = ("brand", "product_name", "item_name", "entity", "code")


def build_grounded_charts(
    evidence_sets: Sequence[EvidenceSet],
    rendered_record_ids: Sequence[str],
) -> tuple[dict[str, Any], ...]:
    rendered = frozenset(rendered_record_ids)
    charts: list[dict[str, Any]] = []
    for evidence_set in evidence_sets:
        records = tuple(
            record for record in evidence_set.records if record.evidence_id in rendered
        )
        for field, metric_label in _VALUE_FIELDS:
            points = tuple(
                point
                for record in records
                if (point := _point(record, field)) is not None
            )
            if len(points) < 2:
                continue
            periods = list(dict.fromkeys(point[0] for point in points))
            if len(periods) < 2:
                continue
            series: list[dict[str, Any]] = []
            labels = list(dict.fromkeys(point[1] for point in points))
            for label in labels:
                grouped = {period: (value, record_id) for period, item, value, record_id in points if item == label}
                if len(grouped) < 2:
                    continue
                selected_periods = [period for period in periods if period in grouped]
                series.append(
                    {
                        "label": label,
                        "values": [grouped[period][0] for period in selected_periods],
                        "record_ids": [grouped[period][1] for period in selected_periods],
                    }
                )
            if not series:
                continue
            common_periods = [
                period
                for period in periods
                if all(period in {point[0] for point in points if point[1] == item["label"]} for item in series)
            ]
            if len(common_periods) < 2:
                continue
            normalized_series = []
            for item in series:
                point_map = {
                    period: (value, record_id)
                    for period, label, value, record_id in points
                    if label == item["label"]
                }
                normalized_series.append(
                    {
                        "label": item["label"],
                        "values": [point_map[period][0] for period in common_periods],
                        "record_ids": [point_map[period][1] for period in common_periods],
                    }
                )
            identity = "\x1f".join(
                (evidence_set.source, field, *common_periods, *(item["label"] for item in normalized_series))
            )
            charts.append(
                {
                    "chart_id": "v4-" + sha256(identity.encode("utf-8")).hexdigest()[:16],
                    "chart_type": "line",
                    "title": f"{metric_label} 추이",
                    "x": {"label": "기간", "values": common_periods},
                    "series": normalized_series,
                    "unit": _unit(records, field),
                    "source_label": public_source_label(evidence_set.source),
                }
            )
    return tuple(charts)


def _point(record: EvidenceRecord, field: str) -> tuple[str, str, int | float, str] | None:
    period = next(
        (str(record.payload.get(key)).strip() for key in _PERIOD_FIELDS if record.payload.get(key) not in (None, "")),
        "",
    )
    value = record.payload.get(field)
    if not period or not isinstance(value, int | float) or isinstance(value, bool):
        return None
    label = next(
        (str(record.payload.get(key)).strip() for key in _SERIES_FIELDS if record.payload.get(key) not in (None, "")),
        field,
    )
    return period, label, value, record.evidence_id


def _unit(records: Sequence[EvidenceRecord], field: str) -> str:
    for record in records:
        unit = record.payload.get("unit")
        if isinstance(unit, str) and unit.strip():
            return unit.strip()
        units = record.payload.get("units")
        if isinstance(units, Mapping) and isinstance(units.get(field), str):
            return str(units[field]).strip()
    return ""
