from __future__ import annotations

"""Build product-level filter dimension sidecars for dynamic general markets.

The existing ``mart_general_brand_metric`` grain intentionally stays at
brand×ATC4×source×measure. Dynamic filters such as UBIST 제형 or 급여구분 are
product-level facts, so applying them to the brand row would over-include every
other product under the same brand. This module emits an additive sidecar with a
product×dimension grain; the dynamic API can aggregate this table only when a
dimension filter is present, while the unfiltered path keeps using the proven
general mart.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
import re

import pandas as pd

from .general_config import MEASURES_BY_SOURCE


FILTER_DIMENSION_TABLE = "mart_general_filter_dimension_metric"
DIMENSION_STAGE_PREFIX = "jw_mart_dim_stage_"
BLOCKED_DIMENSION_TARGETS = frozenset(
    {
        "jw_mart",
        "jw_mart_test_stage2",
        "jw_mart_d1_stage_20260625_173115",
    }
)
EMPTY_DIMENSION_VALUES = frozenset({"", "-", "n/a", "na", "nan", "none", "null", "미상", "해당없음"})


@dataclass(frozen=True, slots=True)
class DimensionSpec:
    dimension_type: str
    display_name: str
    source_columns: tuple[str, ...]
    enabled: bool
    source: str
    notes: str


DIMENSION_REGISTRY: dict[str, dict[str, DimensionSpec]] = {
    "ubist": {
        "seller": DimensionSpec(
            dimension_type="seller",
            display_name="판매사",
            source_columns=("company", "manufacturer"),
            enabled=True,
            source="ubist",
            notes="판매사를 우선하고 비어 있으면 제조사를 사용한다.",
        ),
        "molecule": DimensionSpec(
            dimension_type="molecule",
            display_name="성분",
            source_columns=("ubist_molecule_raw",),
            enabled=False,
            source="ubist",
            notes="PL 결정으로 MVP 동적 필터에서 제외한다. raw provenance만 보존한다.",
        ),
        "molecule_strength": DimensionSpec(
            dimension_type="molecule_strength",
            display_name="성분용량",
            source_columns=("ubist_molecule_strength",),
            enabled=True,
            source="ubist",
            notes="성분 자체는 제외하지만 용량 축은 분석레벨로 유지한다.",
        ),
        "form": DimensionSpec(
            dimension_type="form",
            display_name="제형",
            source_columns=("ubist_form",),
            enabled=True,
            source="ubist",
            notes="제품 단위 제형 필터. brand-level row에 넣으면 과대 포함된다.",
        ),
        "route": DimensionSpec(
            dimension_type="route",
            display_name="투여경로",
            source_columns=("ubist_route",),
            enabled=True,
            source="ubist",
            notes="제품 단위 투여경로 필터.",
        ),
        "reimbursement": DimensionSpec(
            dimension_type="reimbursement",
            display_name="급여구분",
            source_columns=("ubist_reimbursement",),
            enabled=True,
            source="ubist",
            notes="제품 단위 급여구분 필터.",
        ),
    },
    "iqvia_nsa": {
        "mfr": DimensionSpec(
            dimension_type="mfr",
            display_name="MFR NAME KOR",
            source_columns=("company", "manufacturer"),
            enabled=True,
            source="iqvia_nsa",
            notes="IQVIA 판매사 축. MFR NAME KOR를 우선하고 row-level mfr_name을 fallback으로 둔다.",
        ),
        "molecule": DimensionSpec(
            dimension_type="molecule",
            display_name="MOLECULE DESC",
            source_columns=("molecule_desc", "molecule"),
            enabled=False,
            source="iqvia_nsa",
            notes="PL 결정으로 MVP 동적 필터에서 제외한다. raw provenance만 loader에 남긴다.",
        ),
        "molecule_type": DimensionSpec(
            dimension_type="molecule_type",
            display_name="MOLECULE TYPE",
            source_columns=("molecule_type",),
            enabled=True,
            source="iqvia_nsa",
            notes="IQVIA 고유 분석레벨. 기존 dimension_data에 없어서 raw static에서 별도 추출한다.",
        ),
        "pack": DimensionSpec(
            dimension_type="pack",
            display_name="PACK DESC",
            source_columns=("pack_desc",),
            enabled=False,
            source="iqvia_nsa",
            notes="PL 결정으로 제외한다. pack free-form 필터는 MVP 범위 밖이다.",
        ),
        "strength": DimensionSpec(
            dimension_type="strength",
            display_name="STRENGTH",
            source_columns=("strength",),
            enabled=True,
            source="iqvia_nsa",
            notes="제품 단위 성분용량 필터. PACK DESC fallback을 쓰지 않아 제외 정책을 지킨다.",
        ),
        "nhi": DimensionSpec(
            dimension_type="nhi",
            display_name="NHI TYPE",
            source_columns=("nhi_type",),
            enabled=True,
            source="iqvia_nsa",
            notes="제품 단위 급여구분 필터.",
        ),
    },
}


def enabled_dimension_specs(source: str) -> tuple[DimensionSpec, ...]:
    registry = DIMENSION_REGISTRY.get(source)
    if registry is None:
        raise ValueError(f"unsupported dimension source: {source}")
    return tuple(spec for spec in registry.values() if spec.enabled)


def normalize_dimension_value(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    normalized = re.sub(r"\s+", " ", str(value)).strip()
    if normalized.lower() in EMPTY_DIMENSION_VALUES:
        return None
    return normalized


def build_filter_dimension_rows(source: str, measure: str, frame: pd.DataFrame) -> list[dict[str, Any]]:
    if measure not in MEASURES_BY_SOURCE.get(source, ()):
        raise ValueError(f"unsupported measure for {source}: {measure}")
    if frame.empty:
        return []
    required = {"atc4_code", "brand_key", "brand_name", "product_code", "period_yyyymm", "raw_value"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"missing columns for filter dimension sidecar: {sorted(missing)}")

    rows: list[dict[str, Any]] = []
    working = frame.loc[frame["raw_value"].notna() & (frame["raw_value"] > 0)].copy()
    working["source"] = source
    working["measure"] = measure
    for spec in enabled_dimension_specs(source):
        label_col = f"__{spec.dimension_type}_display"
        norm_col = f"__{spec.dimension_type}_norm"
        working[label_col] = _dimension_display_series(working, spec)
        working[norm_col] = working[label_col].map(normalize_dimension_value)
        dim_frame = working.loc[working[norm_col].notna()].copy()
        if dim_frame.empty:
            continue
        group_cols = [
            "source",
            "measure",
            "atc4_code",
            "brand_key",
            "product_code",
            norm_col,
        ]
        for key, group in dim_frame.groupby(group_cols, dropna=False, sort=True):
            history = _period_history(group)
            if not history:
                continue
            source_v, measure_v, atc4, brand_key, product_code, norm = key
            brand_name = _first_sorted_label(group["brand_name"])
            display = _first_sorted_label(group[label_col])
            rows.append(
                {
                    "source": str(source_v),
                    "measure": str(measure_v),
                    "atc4_code": str(atc4),
                    "brand_key": str(brand_key),
                    "brand_name": str(brand_name),
                    "product_code": "" if pd.isna(product_code) else str(product_code),
                    "dimension_type": spec.dimension_type,
                    "dimension_value": str(display),
                    "dimension_value_norm": str(norm),
                    "raw_value_history": history,
                }
            )
    return rows


def guard_dimension_stage_target(target_db: str) -> None:
    if target_db in BLOCKED_DIMENSION_TARGETS:
        raise ValueError(f"refusing protected target schema: {target_db}")
    if not target_db.startswith(DIMENSION_STAGE_PREFIX):
        raise ValueError(f"target_db must start with {DIMENSION_STAGE_PREFIX}: {target_db}")
    if "`" in target_db or not target_db.replace("_", "").isalnum():
        raise ValueError(f"unsafe target schema name: {target_db}")


def _dimension_display_series(frame: pd.DataFrame, spec: DimensionSpec) -> pd.Series:
    """Return the first non-empty configured source column without row-wise apply.

    Full UBIST+IQVIA builds walk millions of raw product-period rows. A row-wise
    ``DataFrame.apply`` made STAGE B spend minutes before the first Galera-safe
    insert. This vectorized combine keeps the same fallback order while making
    the tracked builder viable for full isolated evidence builds.
    """
    result = pd.Series([None] * len(frame), index=frame.index, dtype=object)
    for column in spec.source_columns:
        if column not in frame:
            continue
        values = frame[column].map(normalize_dimension_value)
        result = result.where(result.notna(), values)
    return result


def _first_sorted_label(series: pd.Series) -> str:
    labels = sorted({str(value) for value in series.dropna().tolist() if normalize_dimension_value(value)})
    return labels[0] if labels else ""


def _period_history(group: pd.DataFrame) -> dict[str, float]:
    series = group.groupby("period_yyyymm", dropna=False)["raw_value"].sum().to_dict()
    return {
        str(period): float(value)
        for period, value in sorted(series.items(), key=lambda item: str(item[0]))
        if period and value and float(value) > 0
    }


def summarize_dimension_rows(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"row_count": 0, "dimension_types": {}}
    options: dict[str, set[str]] = {}
    for row in rows:
        summary["row_count"] += 1
        dim_type = str(row["dimension_type"])
        options.setdefault(dim_type, set()).add(str(row["dimension_value_norm"]))
    summary["dimension_types"] = {
        dim_type: {"option_count": len(values)}
        for dim_type, values in sorted(options.items())
    }
    return summary
