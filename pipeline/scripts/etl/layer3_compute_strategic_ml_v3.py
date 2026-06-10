#!/usr/bin/env python3
"""Build and load strategic ML JSON marts from general-view rows.

도메인 규칙 요약:
- 일반뷰는 ATC4 기반 집계다.
- 전략뷰 market_landscape는 MI Master 시트가 정의한 ATC4, molecule,
  class, 제형/strength recode를 기준으로 멤버십을 만든다.
- competitive_dynamics는 ML에서 cd_filter로 좁힌 universe다.
- recode/override는 자기 field의 raw 값을 덮어쓰는 OVERWRITE다. 예를 들어
  dosage_form recode는 dosage_form만, molecule recode는 molecule만 바꾼다.
  class recode를 molecule에 넣는 식의 교차-field fallback은 금지한다.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import duckdb
import pandas as pd

from brand_key_normalize import normalize_brand_name
from layer3_compute_general_v3 import (
    ALLOWED_SOURCES,
    GENERAL_BRAND_INSERT_COLUMNS,
    JSON_INSERT_COLUMNS,
    cagr_from_history,
    dumps,
    ensure_json_columns,
    fill_periods,
    general_brand_jsonl_path,
    json_ready,
    mariadb_connect,
    mat_growth,
    pct_growth,
    read_jsonl,
    SKU_DIMENSION_COLUMNS,
    ubist_channel_to_raw,
    value_at,
    write_jsonl,
)
from layer3_compute_extended import compute_ei, compute_growth_contribution, compute_momentum
from layer3_compute_market_metric import compute_market_mart_payload
from layer3_normalize import prev_month, prev_quarter_month, same_month_prev_year
from layer2_normalize import normalize_atc
from ops_utils import configure_logging, find_project_root
from utils.ubist_channel_mapping import parse_channel_code


LOGGER = configure_logging(__name__)
PROJECT_ROOT = find_project_root(Path(__file__).resolve())
CATALOG_DIR = PROJECT_ROOT / "output" / "catalog"
ENRICHED_DIR = PROJECT_ROOT / "output" / "enriched"
DRY_RUN_DIR = Path("/tmp")
ML_BRAND_JSONL = "strategic_ml_v3_brand_rows.jsonl"
ML_MARKET_JSONL = "strategic_ml_v3_market_rows.jsonl"
ML_BRAND_COLUMNS = [
    "ml_id",
    "brand_id",
    "brand_key",
    "brand_name",
    "source",
    "measure",
    "is_jw",
    "unit_label",
    "metric_history",
    "extended_metric_history",
    "channel_data",
    "specialty_data",
    "dimension_data",
    "dimension_channel_data",
    "dimension_specialty_data",
    "by_dimension",
    "raw_value_history",
    "overlay_data",
    "payload",
]
ML_MARKET_COLUMNS = [
    "ml_id",
    "ml_name",
    "source",
    "measure",
    "unit_label",
    "market_size_series",
    "hhi_series_5y",
    "brand_ranking_stacked",
    "company_ranking_stacked",
    "company_concentration_trend",
    "ei_ms_matrix",
    "growth_contribution_ms_matrix",
    "growth_contribution",
    "analysis_levels",
    "level_top5_trend",
    "target_customer_competition",
    "payload",
]
UBIST_MEASURES = ("sales", "volume")
IQVIA_MEASURES = ("sales", "unit", "dosage_unit", "counting_unit")


def _notna(value: Any) -> bool:
    try:
        return not bool(pd.isna(value))
    except Exception:
        return value is not None


def _truthy(value: Any) -> bool:
    if not _notna(value):
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    return bool(value)


def _parse_json_list(value: Any) -> list[str]:
    if not _notna(value):
        return []
    if isinstance(value, list):
        return [str(item).strip().upper() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "<na>"}:
        return []
    parsed = json.loads(text)
    if not isinstance(parsed, list):
        raise ValueError(f"expected JSON list, found={text!r}")
    return [str(item).strip().upper() for item in parsed if str(item).strip()]


def _allowed_atc4_codes(overlay: dict[str, Any], ml_row: pd.Series) -> set[str]:
    """Return MI Master allowed ATC4 codes for this market-member row.

    Row-level catalog definitions are the first authority.  If an older
    catalog lacks them, fall back to the market-level ATC4 set so legacy data
    remains loadable while clean regenerations use precise sheet rows.
    """

    allowed = set(_parse_json_list(overlay.get("allowed_atc4_codes_json")))
    if allowed:
        return allowed
    return set(_parse_json_list(ml_row.get("atc_codes_json")))


def _atc4_aliases(value: Any) -> set[str]:
    """Return equivalent MI/IQVIA and UBIST ATC4 spellings for matching only.

    IQVIA 쪽 MI Master 정의는 A10H0처럼 5글자 ATC4를 쓰는 반면,
    UBIST general/raw 집계는 A10H처럼 끝의 0이 빠진 4글자 코드를 쓰는 경우가
    있다. 이 차이 때문에 ml_003 UBIST에서 SU/AGI 제네릭 97개가 layer3
    selection에서 누락됐다. 5글자 영문-숫자-숫자-영문-0 패턴만 4글자
    alias로 연결하고, 다른 코드를 무차별 prefix 매칭하는 대안은 unrelated
    ATC를 끌어들일 위험이 있어 기각했다.
    """

    text = str(value or "").strip().upper()
    if not text:
        return set()

    aliases = {text}
    normalized = normalize_atc(text).upper()
    if normalized:
        aliases.add(normalized)

    for code in list(aliases):
        if len(code) >= 4 and code[0].isalpha() and code[1] == "0" and code[2].isdigit():
            aliases.add(code[0] + code[2:])

    for code in list(aliases):
        if len(code) == 4 and code[-1] == "0" and code[0].isalpha() and code[1].isdigit() and code[2].isalpha():
            aliases.add(code[:-1])

    for code in list(aliases):
        if len(code) == 5 and code[-1] == "0" and code[0].isalpha() and code[1:3].isdigit() and code[3].isalpha():
            aliases.add(code[:-1])

    return aliases


def _allowed_atc4_aliases(allowed_atc4_codes: Iterable[str]) -> set[str]:
    aliases: set[str] = set()
    for code in allowed_atc4_codes:
        aliases.update(_atc4_aliases(code))
    return aliases


def _row_atc4_code(row: dict[str, Any]) -> str:
    return str(row.get("atc4_code") or "").strip().upper()


def _is_numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _merge_numeric_json_values(left: Any, right: Any) -> Any:
    if isinstance(left, dict) and isinstance(right, dict):
        merged = dict(left)
        for key, value in right.items():
            merged[key] = _merge_numeric_json_values(merged[key], value) if key in merged else deepcopy(value)
        return merged
    if _is_numeric(left) and _is_numeric(right):
        return float(left) + float(right)
    return deepcopy(left) if left not in (None, {}, []) else deepcopy(right)


def _sum_raw_histories(rows: list[dict[str, Any]]) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        for period, value in (row.get("raw_value_history") or {}).items():
            try:
                totals[str(period)] += float(value or 0)
            except (TypeError, ValueError):
                continue
    return dict(sorted(totals.items()))


def _collapse_same_brand_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse selected ATC4 sibling rows after MI Master filtering.

    The DB uniqueness grain is ``ml_id × brand_id × source × measure``.  When a
    single MI Master brand intentionally spans multiple ATC4 rows, those rows
    must be summed before insertion so the brand mart and market mart use the
    same totals.
    """

    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[
            (
                str(row.get("ml_id")),
                str(row.get("brand_id")),
                str(row.get("source")),
                str(row.get("measure")),
            )
        ].append(row)

    collapsed: list[dict[str, Any]] = []
    for members in grouped.values():
        if len(members) == 1:
            collapsed.append(members[0])
            continue
        base = deepcopy(members[0])
        base["raw_value_history"] = _sum_raw_histories(members)
        for column in (
            "channel_data",
            "specialty_data",
            "dimension_data",
            "dimension_channel_data",
            "dimension_specialty_data",
            "channel_specialty_matrix",
        ):
            merged: Any = {}
            for member in members:
                merged = _merge_numeric_json_values(merged, member.get(column) or {})
            base[column] = merged
        atc4_codes = sorted({_row_atc4_code(member) for member in members if _row_atc4_code(member)})
        overlay = dict(base.get("overlay_data") or {})
        overlay["collapsed_from_atc4_codes"] = atc4_codes
        overlay["collapsed_row_count"] = len(members)
        base["overlay_data"] = overlay
        collapsed.append(base)
    return collapsed


def expected_measure_pairs(data_source: Any) -> set[tuple[str, str]]:
    value = str(data_source or "").strip().lower()
    expected: set[tuple[str, str]] = set()
    if value in {"ubist", "both", "dual"}:
        expected.update(("ubist", measure) for measure in UBIST_MEASURES)
    if value in {"iqvia", "iqvia_nsa", "both", "dual"}:
        expected.update(("iqvia_nsa", measure) for measure in IQVIA_MEASURES)
    if not expected:
        raise RuntimeError(f"Unsupported strategic data_source={data_source!r}")
    return expected


def load_catalogs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    ml_market = pd.read_parquet(CATALOG_DIR / "ml_market" / "ml_market.parquet")
    strategic_brand = pd.read_parquet(CATALOG_DIR / "strategic_brand" / "strategic_brand.parquet")
    strategic_product = pd.read_parquet(CATALOG_DIR / "strategic_product" / "strategic_product.parquet")
    strategic_brand = drop_strict_excluded_rows(strategic_brand, "strategic_brand")
    strategic_product = drop_strict_excluded_rows(strategic_product, "strategic_product")
    if "general_brand_key" in strategic_brand.columns:
        strategic_brand["brand_key"] = strategic_brand["general_brand_key"].fillna(strategic_brand["name"]).map(normalize_brand_name)
    else:
        strategic_brand["brand_key"] = strategic_brand["name"].map(normalize_brand_name)
    return ml_market, strategic_brand, strategic_product


def drop_strict_excluded_rows(brands: pd.DataFrame, label: str) -> pd.DataFrame:
    if "is_excluded" not in brands.columns:
        return brands
    excluded_mask = brands["is_excluded"].map(_truthy)
    removed = int(excluded_mask.sum())
    if removed:
        print(f"[exclude] strict 제외 제거 ({label}): {len(brands)} -> {len(brands) - removed}")
    return brands.loc[~excluded_mask].copy()


def _clean_dimension_label(value: Any) -> str | None:
    if not _notna(value):
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "<na>"}:
        return None
    return text


def _dimension_atoms(value: Any) -> list[str]:
    text = _clean_dimension_label(value)
    if not text:
        return []
    atoms = [part.strip() for part in text.split("|") if part.strip()]
    return list(dict.fromkeys(atoms))


def _value_from_history_item(item: Any) -> float:
    if isinstance(item, dict):
        item = item.get("raw_value") or item.get("value") or item.get("sales")
    try:
        return float(item or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _series_from_history(history: Any) -> dict[str, dict[str, float]]:
    if not isinstance(history, dict):
        return {}
    return {
        str(period): {"raw_value": _value_from_history_item(value)}
        for period, value in history.items()
    }


def _fill_dimension_series(
    target: dict[str, Any],
    existing: dict[str, Any],
    field: str,
    label: str,
    series: dict[str, Any],
) -> None:
    if not label or not isinstance(series, dict):
        return
    label_bucket = target.setdefault(field, {}).setdefault(label, {})
    for period, item in series.items():
        period_key = str(period)
        if _field_period_has_existing_value(existing, field, period_key):
            continue
        period_bucket = label_bucket.setdefault(period_key, {"raw_value": 0.0})
        period_bucket["raw_value"] = _value_from_history_item(period_bucket) + _value_from_history_item(item)


def _field_period_has_existing_value(existing: dict[str, Any], field: str, period: str) -> bool:
    field_bucket = existing.get(field) if isinstance(existing, dict) else None
    if not isinstance(field_bucket, dict):
        return False
    for label_series in field_bucket.values():
        if isinstance(label_series, dict) and _value_from_history_item(label_series.get(period)) > 0:
            return True
    return False


def _rekey_dimension_field_to_label(row: dict[str, Any], field: str, label: Any) -> None:
    clean_label = _clean_dimension_label(label)
    if not clean_label:
        return
    for payload_key in ("dimension_data", "dimension_channel_data", "dimension_specialty_data"):
        payload = row.get(payload_key)
        if not isinstance(payload, dict):
            continue
        field_bucket = payload.get(field)
        if not isinstance(field_bucket, dict) or not field_bucket:
            continue
        if set(field_bucket) == {clean_label}:
            continue
        merged: dict[str, Any] = {}
        for series in field_bucket.values():
            merged = _merge_numeric_json_values(merged, series)
        payload[field] = {clean_label: merged}
    by_dimension = row.get("by_dimension")
    if isinstance(by_dimension, dict):
        by_dimension[field] = clean_label


def _clear_dimension_field(row: dict[str, Any], field: str) -> None:
    for payload_key in ("dimension_data", "dimension_channel_data", "dimension_specialty_data"):
        payload = row.get(payload_key)
        if isinstance(payload, dict):
            payload.pop(field, None)
    by_dimension = row.get("by_dimension")
    if isinstance(by_dimension, dict):
        by_dimension.pop(field, None)


def _raw_iqvia_strength_atom(atom: str) -> bool:
    # IQVIA strength raw 누출 판정은 "명백히 raw pack/제형 토큰인가"만 본다.
    # A2 fix의 목표는 mart dimension_data group key 자체를 catalog recode로
    # 집계해 No STRENGTH, PRE-F, BAG 같은 raw pack 라벨이 API까지 나오지 않게
    # 하는 것이다. cache 출력에서만 이름을 바꾸는 대안은 mart와 cache가 서로
    # 다른 single source를 갖게 되어 기각했다.
    text = str(atom or "").strip()
    upper = text.upper()
    if not upper or upper in {"NO STRENGTH", "NAN", "NONE", "NULL"}:
        return True
    raw_markers = (
        "INFU", "C.T", "TAB", "CAP", "AMP", "LIQ", "PWD", "SYR",
        "SACH", "ORAL", "VIAL", "FILM", "GRAN", "SUSP",
        "V.SC", "PRE-F", "PREF", "PFS", "SRN", "DRY", "PLASTI", "BAG",
    )
    return any(marker in upper for marker in raw_markers)


def _raw_iqvia_dosage_atom(atom: str) -> bool:
    text = str(atom or "").strip()
    upper = text.upper()
    raw_markers = (
        "ORDINARY", "TABLET", "CAPSULE", "POWDER", "SOLUTION", "UNIT DOSE",
        "PARENTAL", "RETARD", "DRY", "VIAL", "BOTTLE", "INFUSION",
    )
    return any(marker in upper for marker in raw_markers)


def _iqvia_recode_label(field: str, label: Any) -> str | None:
    # IQVIA dimension 집계의 권위 라벨은 catalog의 recode된 field다.
    # UBIST는 이미 catalog recode 경로를 타고 있었으므로 IQVIA만 raw NFC/raw
    # pack이 남았다. 여기서 fallback은 recode가 비어 있는 예외 셀을 위한
    # 보존 장치이고, recode가 있는데 raw를 우선하는 방식은 5/18 OVERWRITE
    # 원칙을 깨므로 기각했다.
    clean_label = _clean_dimension_label(label)
    if not clean_label or field not in {"dosage_form", "strength_pack"}:
        return clean_label
    atoms = _dimension_atoms(clean_label)
    if field == "strength_pack":
        atoms = [atom for atom in atoms if not _raw_iqvia_strength_atom(atom)]
    elif field == "dosage_form":
        atoms = [atom for atom in atoms if not _raw_iqvia_dosage_atom(atom)]
    return " | ".join(atoms) if atoms else None


def _fill_dimension_channel_series(
    target: dict[str, Any],
    existing: dict[str, Any],
    channel_totals: dict[str, Any],
    field: str,
    label: str,
    channel: str,
    series: dict[str, Any],
) -> None:
    if not label or not channel or not isinstance(series, dict):
        return
    channel_bucket = target.setdefault(field, {}).setdefault(label, {}).setdefault(str(channel), {})
    for period, item in series.items():
        period_key = str(period)
        if _channel_period_total(channel_totals, str(channel), period_key) <= 0:
            continue
        if _field_channel_period_has_existing_value(existing, field, str(channel), period_key):
            continue
        period_bucket = channel_bucket.setdefault(period_key, {"raw_value": 0.0})
        period_bucket["raw_value"] = _value_from_history_item(period_bucket) + _value_from_history_item(item)


def _channel_period_total(channel_totals: dict[str, Any], channel: str, period: str) -> float:
    if not isinstance(channel_totals, dict):
        return 0.0
    series = channel_totals.get(channel)
    if not isinstance(series, dict):
        return 0.0
    return _value_from_history_item(series.get(period))


def _field_channel_period_has_existing_value(existing: dict[str, Any], field: str, channel: str, period: str) -> bool:
    field_bucket = existing.get(field) if isinstance(existing, dict) else None
    if not isinstance(field_bucket, dict):
        return False
    for label_channels in field_bucket.values():
        if not isinstance(label_channels, dict):
            continue
        channel_series = label_channels.get(channel)
        if isinstance(channel_series, dict) and _value_from_history_item(channel_series.get(period)) > 0:
            return True
    return False


def _enriched_ubist_specialty_display(channel: Any, specialty: Any) -> str | None:
    """Map Layer2 UBIST channel/specialty codes to chart7 display channels.

    Layer2 stores compact codes such as TH/GH/Semi/CL and Cardio/Endo.  The
    chart7 target-channel contract groups TH/GH/Semi into GH ("종합병원") and
    exposes the display label from ``ubist_channel_mapping``.
    """

    facility_text = str(channel or "").strip()
    specialty_text = str(specialty or "").strip()
    if not facility_text or not specialty_text or specialty_text == "Unknown":
        return None
    facility_code = {
        "TH": "GH",
        "GH": "GH",
        "Semi": "GH",
        "CL": "CL",
        "기타": "OT",
        "OT": "OT",
    }.get(facility_text)
    if not facility_code:
        return None
    try:
        parsed = parse_channel_code(f"{facility_code} {specialty_text}")
    except ValueError:
        return None
    return parsed.display_name if parsed else None


def _fill_dimension_specialty_series(
    target: dict[str, Any],
    field: str,
    label: str,
    specialty_channel: str,
    series: dict[str, Any],
) -> None:
    if not label or not specialty_channel or not isinstance(series, dict):
        return
    channel_bucket = target.setdefault(field, {}).setdefault(label, {}).setdefault(str(specialty_channel), {})
    for period, item in series.items():
        period_key = str(period)
        period_bucket = channel_bucket.setdefault(period_key, {"raw_value": 0.0})
        period_bucket["raw_value"] = _value_from_history_item(period_bucket) + _value_from_history_item(item)


def _catalog_single_dimension_by_brand(
    catalog_rows: pd.DataFrame,
    strategic_products: pd.DataFrame,
) -> dict[str, dict[str, str]]:
    """Return dimensions that are a single catalog fact for each brand_id.

    Multi-valued fields deliberately stay absent so downstream code never has
    to split a pipe-joined label back into separate SKU semantics.
    """

    result: dict[str, dict[str, str]] = {}
    source_frames = []
    for frame in (strategic_products, catalog_rows):
        if frame is not None and not frame.empty and "brand_id" in frame.columns:
            source_frames.append(frame)
    if not source_frames:
        return result

    all_rows = pd.concat(source_frames, ignore_index=True, sort=False)
    for brand_id, part in all_rows.groupby("brand_id", dropna=False):
        brand_key = str(brand_id or "")
        if not brand_key:
            continue
        for field in SKU_DIMENSION_COLUMNS:
            if field not in part.columns:
                continue
            atoms: set[str] = set()
            for value in part[field]:
                atoms.update(_dimension_atoms(value))
            if len(atoms) == 1:
                result.setdefault(brand_key, {})[field] = next(iter(atoms))
    return result


def _load_ubist_dimension_context(ml_id: str, strategic_products: pd.DataFrame) -> dict[str, Any]:
    """Build product-code dimension evidence from Layer2 enriched rows.

    Raw rows can duplicate when a product code maps to multiple catalog product
    rows (for example NHI variants).  Channel histories are therefore summed
    from distinct raw source rows, while product_id is used only to determine
    whether a product_code has a single unambiguous label for a dimension.
    """

    enriched_path = ENRICHED_DIR / f"ml_id={ml_id}" / "data.parquet"
    stats: dict[str, Any] = {
        "enriched_path": str(enriched_path),
        "exists": enriched_path.exists(),
        "ubist_source_rows": 0,
        "product_code_count": 0,
        "single_label_counts": {field: 0 for field in SKU_DIMENSION_COLUMNS},
        "multi_label_counts": {field: 0 for field in SKU_DIMENSION_COLUMNS},
    }
    if not enriched_path.exists() or strategic_products.empty:
        return {"code_dimensions": {}, "code_channel_history": {}, "code_specialty_history": {}, "stats": stats}

    con = duckdb.connect()
    try:
        code_product = con.execute(
            f"""
            SELECT DISTINCT
              split_part(source_row_id, '::', 6) AS product_code,
              product_id
            FROM read_parquet('{enriched_path}')
            WHERE source='ubist' AND source_row_id IS NOT NULL
            """
        ).df()
        raw_channel = con.execute(
            f"""
            SELECT product_code, channel, specialty, period_yyyymm,
                   SUM(raw_sales) AS raw_sales,
                   SUM(raw_volume) AS raw_volume
            FROM (
              SELECT DISTINCT
                source_row_id,
                split_part(source_row_id, '::', 6) AS product_code,
                channel,
                specialty,
                period_yyyymm,
                TRY_CAST(raw_rx_amt AS DOUBLE) AS raw_sales,
                TRY_CAST(raw_rx_qty AS DOUBLE) AS raw_volume
              FROM read_parquet('{enriched_path}')
              WHERE source='ubist' AND source_row_id IS NOT NULL
                AND (TRY_CAST(raw_rx_amt AS DOUBLE) > 0 OR TRY_CAST(raw_rx_qty AS DOUBLE) > 0)
            ) AS raw_rows
            GROUP BY 1,2,3,4
            """
        ).df()
    finally:
        con.close()

    stats["ubist_source_rows"] = int(len(raw_channel))
    if code_product.empty:
        return {"code_dimensions": {}, "code_channel_history": {}, "code_specialty_history": {}, "stats": stats}

    product_dims = strategic_products[
        [col for col in ["product_id", *SKU_DIMENSION_COLUMNS] if col in strategic_products.columns]
    ].drop_duplicates("product_id")
    code_dims = code_product.merge(product_dims, on="product_id", how="left")
    code_dimensions: dict[str, dict[str, str]] = {}
    for product_code, part in code_dims.groupby("product_code", dropna=False):
        code = str(product_code or "").strip()
        if not code:
            continue
        stats["product_code_count"] += 1
        for field in SKU_DIMENSION_COLUMNS:
            if field not in part.columns:
                continue
            atoms: set[str] = set()
            for value in part[field]:
                atoms.update(_dimension_atoms(value))
            if len(atoms) == 1:
                code_dimensions.setdefault(code, {})[field] = next(iter(atoms))
                stats["single_label_counts"][field] += 1
            elif len(atoms) > 1:
                stats["multi_label_counts"][field] += 1

    channel_history: dict[str, dict[str, dict[str, dict[str, dict[str, dict[str, float]]]]]] = {
        "sales": {},
        "volume": {},
    }
    specialty_history: dict[str, dict[str, dict[str, dict[str, dict[str, dict[str, float]]]]]] = {
        "sales": {},
        "volume": {},
    }
    specialty_channels_seen: set[str] = set()
    for row in raw_channel.to_dict("records"):
        code = str(row.get("product_code") or "").strip()
        if not code or code not in code_dimensions:
            continue
        channel = ubist_channel_to_raw(row.get("channel"))
        specialty_channel = _enriched_ubist_specialty_display(row.get("channel"), row.get("specialty"))
        period = str(row.get("period_yyyymm") or "").strip()
        if not period:
            continue
        for field, label in code_dimensions[code].items():
            for measure, value_col in (("sales", "raw_sales"), ("volume", "raw_volume")):
                value = _value_from_history_item(row.get(value_col))
                if value <= 0:
                    continue
                measure_bucket = channel_history.setdefault(measure, {})
                field_bucket = measure_bucket.setdefault(code, {}).setdefault(field, {}).setdefault(label, {})
                channel_bucket = field_bucket.setdefault(channel, {})
                channel_bucket[period] = {"raw_value": _value_from_history_item(channel_bucket.get(period)) + value}
                if specialty_channel:
                    specialty_channels_seen.add(specialty_channel)
                    specialty_measure_bucket = specialty_history.setdefault(measure, {})
                    specialty_field_bucket = (
                        specialty_measure_bucket
                        .setdefault(code, {})
                        .setdefault(field, {})
                        .setdefault(label, {})
                    )
                    specialty_bucket = specialty_field_bucket.setdefault(specialty_channel, {})
                    specialty_bucket[period] = {
                        "raw_value": _value_from_history_item(specialty_bucket.get(period)) + value
                    }

    stats["specialty_channel_count"] = len(specialty_channels_seen)
    stats["specialty_channels"] = sorted(specialty_channels_seen)
    return {
        "code_dimensions": code_dimensions,
        "code_channel_history": channel_history,
        "code_specialty_history": specialty_history,
        "stats": stats,
    }


def _enhance_strategic_dimensions(row: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    dimension_data = deepcopy(row.get("dimension_data") or {})
    dimension_channel_data = deepcopy(row.get("dimension_channel_data") or {})
    dimension_specialty_data = deepcopy(row.get("dimension_specialty_data") or {})
    existing_dimension_data = deepcopy(dimension_data)
    existing_dimension_channel_data = deepcopy(dimension_channel_data)
    by_dimension = deepcopy(row.get("by_dimension") or {})
    brand_id = str(row.get("brand_id") or "")
    measure = str(row.get("measure") or "")
    brand_single = context.get("brand_single_dimensions", {}).get(brand_id, {})
    code_dimensions = context.get("code_dimensions", {})
    code_channel_history = context.get("code_channel_history", {}).get(measure, {})
    code_specialty_history = context.get("code_specialty_history", {}).get(measure, {})
    row_history = _series_from_history(row.get("raw_value_history"))
    channel_data = row.get("channel_data") if isinstance(row.get("channel_data"), dict) else {}
    products = by_dimension.get("products") if isinstance(by_dimension.get("products"), list) else []
    is_iqvia = str(row.get("source") or "").strip().lower() == "iqvia_nsa"

    for field in SKU_DIMENSION_COLUMNS:
        overlay_data = row.get("overlay_data") if isinstance(row.get("overlay_data"), dict) else {}
        label = brand_single.get(field) or _clean_dimension_label(overlay_data.get(field))
        if is_iqvia and field in {"dosage_form", "strength_pack"}:
            # A2: IQVIA mart dimension_data도 catalog recode 라벨로 다시 묶는다.
            # cache에서만 recode하면 화면은 맞아도 mart audit에는 raw NFC/pack이
            # 남아 false source of truth가 되므로, layer3 집계 시점부터 raw
            # label을 제거한다. UBIST와 다른 dimension은 기대 경로라 건드리지
            # 않는다.
            label = _iqvia_recode_label(field, label)
            if not label:
                _clear_dimension_field(row, field)
                dimension_data = deepcopy(row.get("dimension_data") or {})
                dimension_channel_data = deepcopy(row.get("dimension_channel_data") or {})
                dimension_specialty_data = deepcopy(row.get("dimension_specialty_data") or {})
                by_dimension = deepcopy(row.get("by_dimension") or {})
                continue
        if label:
            _rekey_dimension_field_to_label(row, field, label)
            dimension_data = deepcopy(row.get("dimension_data") or {})
            dimension_channel_data = deepcopy(row.get("dimension_channel_data") or {})
            dimension_specialty_data = deepcopy(row.get("dimension_specialty_data") or {})
            existing_dimension_data = deepcopy(dimension_data)
            existing_dimension_channel_data = deepcopy(dimension_channel_data)
            by_dimension = deepcopy(row.get("by_dimension") or {})
            _fill_dimension_series(dimension_data, existing_dimension_data, field, label, row_history)
            for channel, series in channel_data.items():
                _fill_dimension_channel_series(
                    dimension_channel_data,
                    existing_dimension_channel_data,
                    channel_data,
                    field,
                    label,
                    str(channel),
                    series,
                )
            if not _clean_dimension_label(by_dimension.get(field)):
                by_dimension[field] = label

        for product in products:
            if not isinstance(product, dict):
                continue
            code = str(product.get("product_code") or "").strip()
            product_label = code_dimensions.get(code, {}).get(field) or label
            if not product_label:
                continue
            if not label:
                product_history = _series_from_history(product.get("raw_value_history"))
                _fill_dimension_series(dimension_data, existing_dimension_data, field, product_label, product_history)
                channel_map = (((code_channel_history.get(code) or {}).get(field) or {}).get(product_label) or {})
                for channel, series in channel_map.items():
                    _fill_dimension_channel_series(
                        dimension_channel_data,
                        existing_dimension_channel_data,
                        channel_data,
                        field,
                        product_label,
                        str(channel),
                        series,
                    )
            specialty_map = (((code_specialty_history.get(code) or {}).get(field) or {}).get(product_label) or {})
            for specialty_channel, series in specialty_map.items():
                _fill_dimension_specialty_series(
                    dimension_specialty_data,
                    field,
                    product_label,
                    str(specialty_channel),
                    series,
                )

    row["dimension_data"] = dimension_data
    row["dimension_channel_data"] = dimension_channel_data
    row["dimension_specialty_data"] = dimension_specialty_data
    row["by_dimension"] = by_dimension
    return row


def fetch_general_rows_from_db(source: str | None = None) -> list[dict[str, Any]]:
    ensure_json_columns("mart_general_brand_metric", ("dimension_data", "dimension_channel_data"))
    where = "WHERE source=%s" if source else ""
    params = (source,) if source else ()
    sql = "SELECT " + ",".join(GENERAL_BRAND_INSERT_COLUMNS) + " FROM mart_general_brand_metric " + where
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()
    for row in rows:
        for col in GENERAL_BRAND_INSERT_COLUMNS:
            if col in {
                "metric_history",
                "extended_metric_history",
                "channel_data",
                "specialty_data",
                "dimension_data",
                "dimension_channel_data",
                "by_dimension",
                "raw_value_history",
                "payload",
            }:
                row[col] = json.loads(row[col]) if row.get(col) else {}
        row["channel_specialty_matrix"] = {}
    return rows


def load_general_rows(output_dir: Path, source: str) -> list[dict[str, Any]]:
    rows = fetch_general_rows_from_db(source)
    if not rows:
        jsonl_rows = read_jsonl(general_brand_jsonl_path(source, output_dir))
        if jsonl_rows:
            raise RuntimeError(f"DB returned no {source} general rows while stale JSONL rows exist")
    return rows


def is_jw_name(name: Any) -> bool:
    text = str(name or "")
    return any(token in text for token in ("리바로", "가드", "라베칸", "제이클", "타발리스", "시그마트", "악템라", "페린젝트", "베노훼럼", "헴리브라", "엔커버", "위너프", "플라주오피"))


def catalog_by_key(brands: pd.DataFrame) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    brands = brands.copy()
    if "is_jw" not in brands.columns:
        brands["is_jw"] = False
    brands["_jw_sort"] = brands["is_jw"].map(_truthy).astype(int)
    brands = brands.sort_values(["_jw_sort", "brand_id"], ascending=[False, True])
    for key, part in brands.groupby("brand_key", dropna=False):
        if not key:
            continue
        first = part.iloc[0].to_dict()
        first["catalog_brand_ids"] = part["brand_id"].astype(str).tolist()
        first["catalog_names"] = part["name"].astype(str).tolist()
        if "allowed_atc4_codes_json" in part.columns:
            allowed: set[str] = set()
            for value in part["allowed_atc4_codes_json"]:
                allowed.update(_parse_json_list(value))
            first["allowed_atc4_codes_json"] = json.dumps(sorted(allowed), ensure_ascii=False) if allowed else None
        if "is_class_excluded" in part.columns:
            first["is_class_excluded"] = bool(part["is_class_excluded"].map(_truthy).any())
        grouped[str(key)] = first
    return grouped


def _display_brand_name(row: dict[str, Any], overlay: dict[str, Any]) -> str:
    if _truthy(overlay.get("is_jw")):
        canonical_name = overlay.get("canonical_name")
        if _notna(canonical_name) and str(canonical_name).strip():
            return str(canonical_name)
        return str(overlay.get("name") or row.get("brand_name") or row.get("brand_key") or "")
    return str(row.get("brand_name") or row.get("brand_key") or overlay.get("name") or "")


def _output_brand_key(row: dict[str, Any], overlay: dict[str, Any], display_name: str) -> str:
    if _truthy(overlay.get("is_jw")):
        return display_name
    return str(row.get("brand_key") or normalize_brand_name(display_name))


def validate_market_completeness(ml_row: pd.Series, catalog_rows: pd.DataFrame, selected: list[dict[str, Any]]) -> None:
    expected_pairs = expected_measure_pairs(ml_row.get("data_source"))
    actual_pairs = {(str(row.get("source")), str(row.get("measure"))) for row in selected}
    missing_market_pairs = expected_pairs - actual_pairs

    jw_catalog = catalog_rows.loc[catalog_rows.get("is_jw", False).map(_truthy)] if "is_jw" in catalog_rows.columns else pd.DataFrame()
    missing_jw: list[str] = []
    for _, catalog_row in jw_catalog.iterrows():
        join_key = str(catalog_row.get("brand_key") or "")
        display = str(catalog_row.get("canonical_name") or catalog_row.get("name") or join_key)
        present = {
            (str(row.get("source")), str(row.get("measure")))
            for row in selected
            if row.get("_catalog_join_key") == join_key
        }
        missing_pairs = expected_pairs - present
        if missing_pairs:
            missing_jw.append(f"{display}:{sorted(missing_pairs)}")

    if missing_market_pairs or missing_jw:
        raise RuntimeError(
            f"Strategic ML completeness failed for {ml_row.get('ml_id')} "
            f"market_missing={sorted(missing_market_pairs)} jw_missing={missing_jw}"
        )


def _row_raw_history(row: dict[str, Any], periods: list[str]) -> dict[str, float]:
    raw_history = row.get("raw_value_history") or {}
    metric_history = row.get("metric_history") or {}
    result: dict[str, float] = {}
    for period in periods:
        value = raw_history.get(period)
        if value is None and isinstance(metric_history.get(period), dict):
            value = metric_history[period].get("raw_value")
        try:
            result[period] = float(value or 0.0)
        except (TypeError, ValueError):
            result[period] = 0.0
    return result


def recompute_market_scoped_metric_history(rows: list[dict[str, Any]]) -> None:
    """Rewrite rank/MS fields at the selected strategic market scope.

    General mart rows are ATC4-scoped.  Strategic ML/CD marts select a narrower
    sibling set, so copying the general ``metric_history`` leaves stale rank and
    market share values.  This function keeps the brand raw histories and
    recalculates every period against the selected strategic rows.
    """

    periods = fill_periods(period for row in rows for period in (row.get("raw_value_history") or {}).keys())
    if not periods:
        periods = fill_periods(
            period
            for row in rows
            for period in (row.get("metric_history") or {}).keys()
        )
    if not periods:
        return

    raw_by_brand: dict[str, dict[str, float]] = {
        str(row.get("brand_name") or row.get("brand_key") or idx): _row_raw_history(row, periods)
        for idx, row in enumerate(rows)
    }
    market_history = {period: sum(history.get(period, 0.0) for history in raw_by_brand.values()) for period in periods}

    rank_by_period: dict[str, dict[str, int | None]] = {}
    for period in periods:
        ranked = sorted(
            ((brand, history.get(period, 0.0)) for brand, history in raw_by_brand.items() if history.get(period, 0.0) > 0),
            key=lambda item: (-item[1], item[0]),
        )
        rank_by_period[period] = {brand: idx + 1 for idx, (brand, _) in enumerate(ranked)}

    for idx, row in enumerate(rows):
        brand_name = str(row.get("brand_name") or row.get("brand_key") or idx)
        history = raw_by_brand[brand_name]
        metric_history = dict(row.get("metric_history") or {})
        extended_history = dict(row.get("extended_metric_history") or {})
        ms_values: list[float] = []

        for period in periods:
            value = history.get(period, 0.0)
            market_total = market_history.get(period, 0.0)
            ms_pct = (value / market_total * 100.0) if market_total > 0 else 0.0
            ms_values.append(ms_pct)

            prev = value_at(history, prev_month(period))
            prev_q = value_at(history, prev_quarter_month(period))
            prev_y = value_at(history, same_month_prev_year(period))
            market_prev_y = value_at(market_history, same_month_prev_year(period))
            growth_abs = value - prev_y if prev_y is not None else None
            market_growth_abs = market_history.get(period, 0.0) - market_prev_y if market_prev_y is not None else None
            growth_contribution, gc_warning = compute_growth_contribution(growth_abs, market_growth_abs)
            cagr_5y = cagr_from_history(history, period, 5)
            market_cagr_5y = cagr_from_history(market_history, period, 5)
            ei_5y, ei_warning = compute_ei(cagr_5y, market_cagr_5y)

            metric_payload = dict(metric_history.get(period) or {})
            metric_payload.update(
                {
                    "raw_value": value,
                    "ms": ms_pct,
                    "mom": pct_growth(value, prev),
                    "qoq": pct_growth(value, prev_q),
                    "yoy": pct_growth(value, prev_y),
                    "mat": mat_growth(history, period),
                    "growth_abs": growth_abs,
                    "rank": rank_by_period[period].get(brand_name) if value > 0 else None,
                }
            )
            metric_history[period] = metric_payload

            extended_payload = dict(extended_history.get(period) or {})
            extended_payload.update(
                {
                    "cagr_1y": cagr_from_history(history, period, 1),
                    "cagr_3y": cagr_from_history(history, period, 3),
                    "cagr_5y": cagr_5y,
                    "ei_5y": ei_5y,
                    "momentum_score": compute_momentum(ms_values[-4:]) if len(ms_values) >= 4 else None,
                    "growth_contribution": growth_contribution,
                    "growth_contribution_pct": growth_contribution,
                    "market_cagr_5y": market_cagr_5y,
                    "warnings": [warning for warning in (gc_warning, ei_warning) if warning],
                }
            )
            extended_history[period] = extended_payload

        row["raw_value_history"] = history
        row["metric_history"] = metric_history
        row["extended_metric_history"] = extended_history


def build_ml_rows(
    ml_row: pd.Series,
    catalog_rows: pd.DataFrame,
    general_rows: list[dict[str, Any]],
    dimension_context: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_key = catalog_by_key(catalog_rows)
    expected_pairs = expected_measure_pairs(ml_row.get("data_source"))
    dimension_context = dimension_context or {}
    selected: list[dict[str, Any]] = []
    for row in general_rows:
        source_measure = (str(row.get("source")), str(row.get("measure")))
        if source_measure not in expected_pairs:
            continue
        overlay = by_key.get(str(row.get("brand_key")))
        if not overlay:
            continue
        allowed_atc4 = _allowed_atc4_codes(overlay, ml_row)
        allowed_atc4_aliases = _allowed_atc4_aliases(allowed_atc4)
        row_atc4 = _row_atc4_code(row)
        if allowed_atc4_aliases and row_atc4 and not (_atc4_aliases(row_atc4) & allowed_atc4_aliases):
            continue
        copied = dict(row)
        display_name = _display_brand_name(copied, overlay)
        output_key = _output_brand_key(copied, overlay, display_name)
        dim = dict(copied.get("by_dimension") or {})
        for key in ("class", "class_1", "class_2"):
            dim[key] = overlay.get(key)
        copied.update(
            {
                "ml_id": ml_row["ml_id"],
                "brand_id": overlay.get("brand_id"),
                "brand_key": output_key,
                "brand_name": display_name,
                "is_jw": _truthy(overlay.get("is_jw")) if "is_jw" in overlay else is_jw_name(overlay.get("name")),
                "by_dimension": dim,
                "dimension_data": copied.get("dimension_data") or {},
                "dimension_channel_data": copied.get("dimension_channel_data") or {},
                "dimension_specialty_data": copied.get("dimension_specialty_data") or {},
                "_catalog_join_key": str(overlay.get("brand_key") or row.get("brand_key") or ""),
                "overlay_data": {
                    "catalog_source": "strategic_brand",
                    "ml_id": ml_row["ml_id"],
                    "canonical_name": overlay.get("canonical_name"),
                    "general_brand_key": overlay.get("general_brand_key"),
                    "is_target": overlay.get("is_target"),
                    "catalog_brand_ids": overlay.get("catalog_brand_ids"),
                    "catalog_names": overlay.get("catalog_names"),
                    "allowed_atc4_codes": sorted(allowed_atc4),
                    "allowed_atc4_aliases": sorted(allowed_atc4_aliases),
                    "is_class_excluded": _truthy(overlay.get("is_class_excluded")),
                    "class": overlay.get("class"),
                    "class_1": overlay.get("class_1"),
                    "class_2": overlay.get("class_2"),
                    "molecule": overlay.get("molecule"),
                    "dosage_form": overlay.get("dosage_form"),
                    "strength_pack": overlay.get("strength_pack"),
                    "nhi_type": overlay.get("nhi_type"),
                    "ox_gx": overlay.get("ox_gx"),
                    "fish_oil": overlay.get("fish_oil"),
                },
            }
        )
        copied = _enhance_strategic_dimensions(copied, dimension_context)
        selected.append(copied)

    selected = _collapse_same_brand_rows(selected)
    validate_market_completeness(ml_row, catalog_rows, selected)
    for rows in _group_by_source_measure(selected).values():
        recompute_market_scoped_metric_history(rows)

    market_rows: list[dict[str, Any]] = []
    for (source, measure), rows in _group_by_source_measure(selected).items():
        payload = compute_market_mart_payload(rows, source=source, measure=measure, view_type="strategic_ml", catalog_market_row=ml_row.to_dict())
        market_rows.append(
            {
                "ml_id": ml_row["ml_id"],
                "ml_name": ml_row.get("name"),
                "source": source,
                "measure": measure,
                "unit_label": rows[0].get("unit_label") if rows else "",
                **payload,
            }
        )
    return selected, market_rows


def _group_by_source_measure(rows: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row.get("source")), str(row.get("measure")))].append(row)
    return grouped


def insert_rows(table: str, columns: list[str], rows: list[dict[str, Any]], unique_cols: set[str], batch_size: int = 500) -> None:
    if not rows:
        return
    placeholders = ",".join(["%s"] * len(columns))
    update_sql = ",".join([f"{col}=VALUES({col})" for col in columns if col not in unique_cols])
    sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {update_sql}"
    payloads = [
        tuple(dumps(row.get(col)) if col in JSON_INSERT_COLUMNS else row.get(col) for col in columns)
        for row in rows
    ]
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            for start in range(0, len(payloads), batch_size):
                cur.executemany(sql, payloads[start : start + batch_size])
    finally:
        conn.close()


def delete_existing_rows(table: str, market_col: str, market_ids: set[str]) -> None:
    if not market_ids:
        return
    placeholders = ",".join(["%s"] * len(market_ids))
    sql = f"DELETE FROM {table} WHERE {market_col} IN ({placeholders})"
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(sorted(market_ids)))
    finally:
        conn.close()


def compute_strategic_ml(dry_run: bool, insert: bool, output_dir: Path, ml: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if not dry_run and not insert:
        raise RuntimeError("Use --dry-run or --insert")
    ml_market, strategic_brand, strategic_product = load_catalogs()
    if ml:
        ml_market = ml_market.loc[ml_market["ml_id"] == ml]
    all_general: list[dict[str, Any]] = []
    for source in ALLOWED_SOURCES:
        all_general.extend(load_general_rows(output_dir, source))
    brand_rows: list[dict[str, Any]] = []
    market_rows: list[dict[str, Any]] = []
    for _, ml_row in ml_market.iterrows():
        catalog_rows = strategic_brand.loc[strategic_brand["ml_id"] == ml_row["ml_id"]].copy()
        product_rows = strategic_product.loc[strategic_product["ml_id"] == ml_row["ml_id"]].copy()
        ubist_context = _load_ubist_dimension_context(str(ml_row["ml_id"]), product_rows)
        dimension_context = {
            **ubist_context,
            "brand_single_dimensions": _catalog_single_dimension_by_brand(catalog_rows, product_rows),
        }
        rows, markets = build_ml_rows(ml_row, catalog_rows, all_general, dimension_context)
        brand_rows.extend(rows)
        market_rows.extend(markets)
    if dry_run:
        write_jsonl(output_dir / ML_BRAND_JSONL, brand_rows)
        write_jsonl(output_dir / ML_MARKET_JSONL, market_rows)
    if insert:
        market_ids = {str(row["ml_id"]) for _, row in ml_market.iterrows()}
        ensure_json_columns(
            "mart_strategic_ml_brand_metric",
            ("dimension_data", "dimension_channel_data", "dimension_specialty_data"),
        )
        delete_existing_rows("mart_strategic_ml_brand_metric", "ml_id", market_ids)
        delete_existing_rows("mart_strategic_ml_market_metric", "ml_id", market_ids)
        insert_rows("mart_strategic_ml_brand_metric", ML_BRAND_COLUMNS, brand_rows, {"ml_id", "brand_id", "source", "measure"})
        insert_rows("mart_strategic_ml_market_metric", ML_MARKET_COLUMNS, market_rows, {"ml_id", "source", "measure"})
    stats = {"brand_rows": len(brand_rows), "market_rows": len(market_rows), "ml_count": int(ml_market["ml_id"].nunique())}
    return brand_rows, market_rows, stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--insert", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=DRY_RUN_DIR)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    brand_rows, market_rows, stats = compute_strategic_ml(args.dry_run, args.insert, args.output_dir, ml=args.ml)
    print("\n=== strategic ML v3.1 ===")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if brand_rows:
        print("sample brand row:")
        print(json.dumps(json_ready(brand_rows[0]), ensure_ascii=False)[:1200])
    if market_rows:
        print("sample market row:")
        print(json.dumps(json_ready(market_rows[0]), ensure_ascii=False)[:1200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
