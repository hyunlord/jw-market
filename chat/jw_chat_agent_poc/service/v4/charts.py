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
    ("prescription_volume", "처방량"),
    ("patient_count", "환자수"),
    ("amount_krw", "금액"),
)
_SERIES_FIELDS = (
    "brand",
    "product_name",
    "item_name",
    "entity",
    "sickCd",
    "sick_code",
    "code",
)


def build_grounded_charts(
    evidence_sets: Sequence[EvidenceSet],
    rendered_record_ids: Sequence[str],
    *,
    question: str = "",
) -> tuple[dict[str, Any], ...]:
    rendered = frozenset(rendered_record_ids)
    requested_fields = _requested_value_fields(question)
    charts: list[dict[str, Any]] = []
    for evidence_set in evidence_sets:
        records = tuple(
            record for record in evidence_set.records if record.evidence_id in rendered
        )
        for field, metric_label in _VALUE_FIELDS:
            if requested_fields and field not in requested_fields:
                continue
            points = tuple(
                point
                for record in records
                if (point := _point(record, field)) is not None
            )
            if len(points) < 2:
                continue
            periods = sorted(
                dict.fromkeys(point[0] for point in points), key=_period_sort_key
            )
            if len(periods) < 2:
                continue
            series: list[dict[str, Any]] = []
            labels = sorted(
                dict.fromkeys(point[1] for point in points), key=str.casefold
            )
            for label in labels:
                grouped = {
                    period: (value, record_id)
                    for period, item, value, record_id in sorted(
                        points, key=lambda point: (point[0], point[1], point[3])
                    )
                    if item == label
                }
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
                    "series_selection_rule": (
                        "all_rendered_series_with_at_least_two_periods"
                    ),
                    "display_order": "period_ascending",
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
    market_id = str(record.payload.get("market_id") or "").strip()
    if record.source == "mart" and market_id:
        label = f"{label} · {market_id}"
    if record.source == "hira":
        qualifiers = tuple(
            str(record.payload.get(key) or "").strip()
            for key in ("inpatOpat", "patient_type", "sex", "age")
            if str(record.payload.get(key) or "").strip()
        )
        if qualifiers:
            label = " · ".join((label, *dict.fromkeys(qualifiers)))
    displayed_value = value / 100_000_000 if field == "sales_krw" else value
    return period, label, displayed_value, record.evidence_id


def _unit(records: Sequence[EvidenceRecord], field: str) -> str:
    if field == "sales_krw":
        return "억원"
    for record in records:
        unit = record.payload.get("unit")
        if isinstance(unit, str) and unit.strip():
            return unit.strip()
        units = record.payload.get("units")
        if isinstance(units, Mapping) and isinstance(units.get(field), str):
            return str(units[field]).strip()
    return ""


def _requested_value_fields(question: str) -> frozenset[str]:
    normalized = question.casefold()
    if "환자수" in normalized or "환자 수" in normalized or "patient" in normalized:
        return frozenset({"patient_count"})
    if "점유율" in normalized or "market share" in normalized:
        return frozenset({"market_share"})
    if "처방" in normalized or "prescription" in normalized:
        return frozenset({"prescription_volume"})
    if "매출" in normalized or "sales" in normalized:
        return frozenset({"sales", "sales_krw"})
    return frozenset()


def requested_chart_metric(question: str) -> str | None:
    fields = _requested_value_fields(question)
    if fields == frozenset({"patient_count"}):
        return "환자수"
    if fields == frozenset({"market_share"}):
        return "점유율"
    if fields == frozenset({"prescription_volume"}):
        return "처방량"
    if fields == frozenset({"sales", "sales_krw"}):
        return "매출"
    return None


def requested_chart_absence_reason(
    evidence_sets: Sequence[EvidenceSet],
    rendered_record_ids: Sequence[str],
    *,
    question: str,
) -> str:
    """Explain why a requested chart is absent without hiding numeric parse failures."""
    rendered = frozenset(rendered_record_ids)
    requested_fields = _requested_value_fields(question)
    if requested_fields == frozenset({"patient_count"}):
        raw_periods: set[str] = set()
        numeric_points = 0
        for evidence_set in evidence_sets:
            if evidence_set.source != "hira":
                continue
            for record in evidence_set.records:
                if record.evidence_id not in rendered:
                    continue
                period = next(
                    (
                        str(record.payload.get(key)).strip()
                        for key in _PERIOD_FIELDS
                        if record.payload.get(key) not in (None, "")
                    ),
                    "",
                )
                if period and record.payload.get("ptntCnt") not in (None, ""):
                    raw_periods.add(period)
                if _point(record, "patient_count") is not None:
                    numeric_points += 1
        if len(raw_periods) >= 2 and numeric_points < 2:
            return "requested_metric_values_not_numeric"
    return "requested_metric_has_fewer_than_two_grounded_points"


def chart_was_requested(question: str) -> bool:
    normalized = question.casefold()
    return any(
        marker in normalized
        for marker in ("추이", "시계열", "연도별", "차트", "그래프", "비교")
    )


def _period_sort_key(period: str) -> tuple[int, int, int, str]:
    normalized = period.strip().upper()
    if len(normalized) == 4 and normalized.isdigit():
        return 0, int(normalized), 0, normalized
    if len(normalized) == 7 and normalized[:4].isdigit():
        suffix = normalized[5:]
        if suffix.startswith("Q") and suffix[1:].isdigit():
            return 0, int(normalized[:4]), int(suffix[1:]) * 3, normalized
        if suffix.isdigit():
            return 0, int(normalized[:4]), int(suffix), normalized
    return 1, 0, 0, normalized
