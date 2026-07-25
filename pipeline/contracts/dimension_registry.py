"""Single dimension contract for sidecar producers and API consumers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any, Final


EMPTY_DIMENSION_VALUES: Final = frozenset(
    {"", "-", "<na>", "n/a", "na", "nan", "none", "null", "미상", "해당없음"}
)


@dataclass(frozen=True, slots=True)
class DimensionSpec:
    dimension_type: str
    display_name: str
    source_columns: tuple[str, ...]
    enabled: bool
    source: str
    notes: str
    api_name: str
    collapse_whitespace: bool = True


DIMENSION_REGISTRY: Final[dict[str, dict[str, DimensionSpec]]] = {
    "ubist": {
        "atc3": DimensionSpec(
            "atc3", "ATC3", ("atc4_code",), True, "ubist",
            "ATC4 코드의 4자리 prefix로 생성하는 UBIST ATC3 narrowing 축.", "atc3",
        ),
        "atc4": DimensionSpec(
            "atc4", "ATC4", ("atc4_code",), True, "ubist",
            "전략뷰/일반뷰 공통 ATC4 narrowing 축. mart atc4_code와 직접 정합한다.", "atc4",
        ),
        "seller": DimensionSpec(
            "seller", "판매사", ("company", "manufacturer"), True, "ubist",
            "판매사를 우선하고 비어 있으면 제조사를 사용한다.", "seller",
        ),
        "molecule": DimensionSpec(
            "molecule", "성분", ("ubist_molecule_raw",), True, "ubist",
            "원천 molecule 값을 분해하지 않고 양끝 공백만 제거해 하나의 성분 값으로 보존한다.",
            "molecule",
            collapse_whitespace=False,
        ),
        "molecule_strength": DimensionSpec(
            "molecule_strength", "성분용량", ("ubist_molecule_strength",), True, "ubist",
            "성분 자체는 제외하지만 용량 축은 분석레벨로 유지한다.", "molecule_strength",
        ),
        "form": DimensionSpec(
            "form", "제형", ("ubist_form",), True, "ubist",
            "제품 단위 제형 필터. brand-level row에 넣으면 과대 포함된다.", "form",
        ),
        "route": DimensionSpec(
            "route", "투여경로", ("ubist_route",), True, "ubist", "제품 단위 투여경로 필터.", "route",
        ),
        "reimbursement": DimensionSpec(
            "reimbursement", "급여구분", ("ubist_reimbursement",), True, "ubist",
            "제품 단위 급여구분 필터.", "reimbursement",
        ),
    },
    "iqvia_nsa": {
        "mfr": DimensionSpec(
            "mfr", "MFR NAME KOR", ("company", "manufacturer"), True, "iqvia_nsa",
            "IQVIA 판매사 축. MFR NAME KOR를 우선하고 row-level mfr_name을 fallback으로 둔다.",
            "mfr_name_kor",
        ),
        "molecule_desc": DimensionSpec(
            "molecule_desc", "MOLECULE DESC", ("molecule_desc", "molecule"), True, "iqvia_nsa",
            "IQVIA 성분 축. MOLECULE DESC 원본 값을 그대로 노출한다.", "molecule_desc",
        ),
        "molecule_type": DimensionSpec(
            "molecule_type", "MOLECULE TYPE", ("molecule_type",), True, "iqvia_nsa",
            "IQVIA 고유 분석레벨. 기존 dimension_data에 없어서 raw static에서 별도 추출한다.",
            "molecule_type",
        ),
        "pack": DimensionSpec(
            "pack", "PACK DESC", ("pack_desc",), True, "iqvia_nsa",
            "PL 결정으로 IQVIA 일반뷰 PACK DESC 분석레벨 필터를 활성화한다.", "pack_desc",
        ),
        "strength": DimensionSpec(
            "strength", "STRENGTH", ("strength",), True, "iqvia_nsa",
            "제품 단위 성분용량 필터. PACK DESC fallback을 쓰지 않아 제외 정책을 지킨다.", "strength",
        ),
        "nhi": DimensionSpec(
            "nhi", "NHI TYPE", ("nhi_type",), True, "iqvia_nsa", "제품 단위 급여구분 필터.", "nhi_type",
        ),
    },
}

DIMENSION_LABELS: Final[dict[str, str]] = {
    "seller": "판매사",
    "molecule": "성분",
    "molecule_strength": "성분용량",
    "form": "제형",
    "route": "투여경로",
    "reimbursement": "급여구분",
    "mfr": "MFR NAME KOR",
    "molecule_type": "MOLECULE TYPE",
    "molecule_desc": "성분",
    "pack": "PACK DESC",
    "strength": "STRENGTH",
    "nhi": "NHI TYPE",
    "mfr_name_kor": "MFR NAME KOR",
    "pack_desc": "PACK DESC",
    "nhi_type": "NHI TYPE",
}

DIMENSION_ORDER_HINTS: Final = (
    "class",
    "molecule",
    "molecule_strength",
    "strength_pack",
    "ox_gx",
    "seller",
    "form",
    "route",
    "reimbursement",
    "mfr",
    "mfr_name_kor",
    "molecule_type",
    "molecule_desc",
    "pack_desc",
    "strength",
    "nhi",
    "nhi_type",
    "audit_code",
)

DIMENSION_CANDIDATE_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "seller": ("seller", "mfr", "manufacturer", "company_name"),
    "class": ("class", "class_name", "market_class"),
    "mfr_name_kor": ("mfr_name_kor", "mfr", "manufacturer", "company_name"),
    "mfr": ("mfr", "mfr_name_kor", "manufacturer", "company_name"),
    "molecule": ("molecule", "molecule_desc"),
    "molecule_strength": ("molecule_strength", "strength_pack", "성분용량"),
    "strength_pack": ("strength_pack", "molecule_strength", "성분용량"),
    "ox_gx": ("ox_gx", "oxgx"),
    "form": ("form", "dosage_form", "제형"),
    "route": ("route", "투여경로"),
    "reimbursement": ("reimbursement", "nhi_type", "nhi", "급여구분"),
    "nhi": ("nhi", "nhi_type", "급여구분"),
    "nhi_type": ("nhi_type", "nhi", "급여구분"),
    "atc3": ("atc3", "atc3_code"),
    "atc4": ("atc4", "atc4_code"),
}

SHARED_API_DIMENSIONS: Final[dict[str, dict[str, str]]] = {
    "iqvia_nsa": {"atc4": "atc4"},
}


def enabled_dimension_specs(source: str) -> tuple[DimensionSpec, ...]:
    registry = DIMENSION_REGISTRY.get(source)
    if registry is None:
        raise ValueError(f"unsupported dimension source: {source}")
    return tuple(spec for spec in registry.values() if spec.enabled)


def api_dimension_names(
    source: str,
    *,
    include_shared: bool = False,
) -> dict[str, str]:
    names = {
        spec.api_name: spec.dimension_type for spec in enabled_dimension_specs(source)
    }
    if include_shared:
        return {**SHARED_API_DIMENSIONS.get(source, {}), **names}
    return names


def api_dimension_name(source: str | None, dimension_type: str) -> str:
    registry = DIMENSION_REGISTRY.get(source or "", {})
    spec = registry.get(dimension_type)
    return spec.api_name if spec is not None else dimension_type


def canonical_dimension_name(source: str | None, dimension_type: str) -> str:
    registry = DIMENSION_REGISTRY.get(source or "", {})
    for spec in registry.values():
        if spec.api_name == dimension_type:
            return spec.dimension_type
    return dimension_type


def dimension_label(dimension_type: str) -> str:
    return DIMENSION_LABELS.get(dimension_type, dimension_type)


def dimension_sort_key(dimension_type: str) -> tuple[int, str]:
    try:
        return (DIMENSION_ORDER_HINTS.index(dimension_type), dimension_type)
    except ValueError:
        return (len(DIMENSION_ORDER_HINTS), dimension_type)


def dimension_candidates(dimensions: Mapping[str, Any], key: str) -> tuple[Any, ...]:
    values: list[Any] = []
    for alias in DIMENSION_CANDIDATE_ALIASES.get(key, (key,)):
        value = dimensions.get(alias)
        if isinstance(value, list):
            values.extend(value)
        elif value not in (None, ""):
            values.append(value)
    return tuple(values)


def normalize_dimension_value(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if value != value:
            return None
    except TypeError:
        pass
    normalized = re.sub(r"\s+", " ", str(value)).strip()
    if normalized.lower() in EMPTY_DIMENSION_VALUES:
        return None
    return normalized


def normalize_spec_value(value: Any, spec: DimensionSpec) -> str | None:
    if spec.collapse_whitespace:
        return normalize_dimension_value(value)
    if value is None:
        return None
    try:
        if value != value:
            return None
    except TypeError:
        pass
    trimmed = str(value).strip()
    return None if trimmed.lower() in EMPTY_DIMENSION_VALUES else trimmed
