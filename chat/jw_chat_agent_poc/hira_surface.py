from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass(frozen=True, slots=True)
class HiraDiseaseMapping:
    canonical_name: str
    aliases: tuple[str, ...]
    code_prefixes: tuple[str, ...]
    catalog_basis: str


# These aliases are exact entries validated against the HIRA disease-name catalog.
# Keep generic family words such as "피부염" out: they span unrelated KCD families.
HIRA_DISEASE_CODE_MAPPINGS: tuple[HiraDiseaseMapping, ...] = (
    HiraDiseaseMapping(
        canonical_name="아토피 피부염",
        aliases=(
            "아토피 피부염",
            "아토피피부염",
            "아토피성 피부염",
            "아토피성피부염",
            "아토피",
        ),
        code_prefixes=("L20",),
        catalog_basis="HIRA 상병명칭 코드목록 L20 계열",
    ),
    HiraDiseaseMapping(
        canonical_name="기저귀 피부염",
        aliases=("기저귀 피부염", "기저귀피부염"),
        code_prefixes=("L22",),
        catalog_basis="HIRA 상병명칭 코드목록 L22",
    ),
    HiraDiseaseMapping(
        canonical_name="전신홍반루푸스",
        aliases=(
            "전신 홍반 루푸스",
            "전신홍반루푸스",
            "전신성 홍반성 루푸스",
        ),
        code_prefixes=("M32",),
        catalog_basis="HIRA 상병명칭 코드목록 M32 계열",
    ),
)

_DIMENSION_KEYS: tuple[tuple[str, ...], ...] = (
    ("inpatOpat", "care_type", "visit_type", "patient_type"),
    ("sex", "gender", "sexCd"),
    ("age", "age_group", "ageCd"),
)
_TOTAL_LABELS = frozenset({"계", "전체", "합계", "total"})
_HIRA_AXIS_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("sex", ("성별", "남녀", "남성", "여성")),
    ("age", ("연령", "나이", "연령별")),
    ("patient_type", ("입원", "외래", "구분별")),
)


def _normalized_name(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", value.casefold())


def hira_disease_mapping(text: str) -> HiraDiseaseMapping | None:
    normalized = _normalized_name(text)
    matches = (
        (len(_normalized_name(alias)), mapping)
        for mapping in HIRA_DISEASE_CODE_MAPPINGS
        for alias in mapping.aliases
        if _normalized_name(alias) in normalized
    )
    return max(matches, key=lambda item: item[0], default=(0, None))[1]


def canonical_hira_disease_query(text: str) -> str | None:
    mapping = hira_disease_mapping(text)
    return mapping.canonical_name if mapping is not None else None


def requested_hira_axes(text: str) -> tuple[str, ...]:
    """Return only demographic or care axes explicitly requested by the user."""

    normalized = " ".join(text.split())
    return tuple(
        axis
        for axis, markers in _HIRA_AXIS_MARKERS
        if any(marker in normalized for marker in markers)
    )


def mentions_hira_axis(text: str) -> bool:
    """Report whether planner-authored text asks for an explicit HIRA axis."""

    return bool(requested_hira_axes(text))


def filter_hira_codes(text: str, codes: Sequence[str]) -> tuple[str, ...]:
    unique = tuple(
        dict.fromkeys(str(code).strip().upper() for code in codes if str(code).strip())
    )
    mapping = hira_disease_mapping(text)
    if mapping is None:
        return unique
    return tuple(
        code
        for code in unique
        if any(code.startswith(prefix) for prefix in mapping.code_prefixes)
    )


def hira_record_matches_question(question: str, payload: Mapping[str, Any]) -> bool:
    mapping = hira_disease_mapping(question)
    if mapping is None:
        return True
    code = _text(payload, "sickCd", "sick_cd", "disease_code", "code").upper()
    return any(code.startswith(prefix) for prefix in mapping.code_prefixes)


def hira_is_aggregate_row(payload: Mapping[str, Any]) -> bool:
    dimensions = tuple(_text(payload, *keys) for keys in _DIMENSION_KEYS)
    return all(not value or value.casefold() in _TOTAL_LABELS for value in dimensions)


def hira_dimension_display(payload: Mapping[str, Any], *keys: str) -> str:
    value = _text(payload, *keys)
    if not value:
        return "전체"
    return re.sub(r"(?<=\d)_(?=\d)", "~", value)


def hira_patient_value(payload: Mapping[str, Any]) -> float | None:
    raw = next(
        (
            payload.get(key)
            for key in ("patient_count", "ptntCnt", "value")
            if payload.get(key) not in (None, "")
        ),
        None,
    )
    if raw is None:
        return None
    try:
        return float(Decimal(str(raw).replace(",", "")))
    except (InvalidOperation, ValueError):
        return None


def hira_row_reconciliation(payloads: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    numeric = tuple(
        payload for payload in payloads if hira_patient_value(payload) is not None
    )
    if not numeric:
        return {"status": "unavailable"}
    latest_period = max((_period(payload) for payload in numeric), default="")
    latest = tuple(
        payload
        for payload in numeric
        if not latest_period or _period(payload) == latest_period
    )
    aggregates = tuple(payload for payload in latest if hira_is_aggregate_row(payload))
    details = tuple(payload for payload in latest if not hira_is_aggregate_row(payload))
    if aggregates:
        aggregate = max(
            aggregates, key=lambda payload: hira_patient_value(payload) or 0
        )
        whole = hira_patient_value(aggregate)
        comparable = _best_detail_sum(details, target=whole)
        if comparable is None:
            return {"status": "aggregate_only", "whole": whole}
        status = "matched" if abs((whole or 0) - comparable) < 0.005 else "mismatch"
        result: dict[str, Any] = {
            "status": status,
            "whole": whole,
            "partial_sum": comparable,
        }
        if status == "mismatch":
            result["reason"] = (
                "집계행과 세부행 합계가 달라 연령 미상 등 "
                "원천 구분 차이가 포함될 수 있습니다."
            )
        return result
    detail_sum = _best_detail_sum(details, target=None)
    return {
        "status": "detail_sum" if detail_sum is not None else "unavailable",
        "partial_sum": detail_sum,
    }


def hira_summary_payload(
    payloads: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any] | None, float | None, str]:
    numeric = tuple(
        payload for payload in payloads if hira_patient_value(payload) is not None
    )
    if not numeric:
        return None, None, "unavailable"
    latest_period = max((_period(payload) for payload in numeric), default="")
    latest = tuple(
        payload
        for payload in numeric
        if not latest_period or _period(payload) == latest_period
    )
    aggregates = tuple(payload for payload in latest if hira_is_aggregate_row(payload))
    if aggregates:
        selected = max(aggregates, key=lambda payload: hira_patient_value(payload) or 0)
        return selected, hira_patient_value(selected), "aggregate"
    selected = max(latest, key=lambda payload: hira_patient_value(payload) or 0)
    return selected, _best_detail_sum(latest, target=None), "detail_sum"


def _best_detail_sum(
    payloads: Sequence[Mapping[str, Any]],
    *,
    target: float | None,
) -> float | None:
    groups: dict[tuple[str, ...], list[float]] = {}
    for payload in payloads:
        value = hira_patient_value(payload)
        if value is None:
            continue
        signature = tuple(keys[0] for keys in _DIMENSION_KEYS if _text(payload, *keys))
        source_tool = _text(payload, "_source_tool")
        groups.setdefault((source_tool, *signature), []).append(value)
    if not groups:
        return None
    sums = tuple(sum(values) for values in groups.values())
    if target is not None:
        return min(sums, key=lambda value: abs(target - value))
    return max(sums)


def _period(payload: Mapping[str, Any]) -> str:
    return _text(payload, "period", "year", "month")


def _text(payload: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""
