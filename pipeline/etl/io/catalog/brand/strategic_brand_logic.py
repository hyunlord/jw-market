from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from pipeline.etl.io.catalog._lib.exclusion_policy import (
    classify_exclusion_cells as classify_exclusion_cells_by_policy,
    contains_exclusion_marker,
)
from pipeline.etl.io.catalog.brand.strategic_brand_schema import SHEET_TOTAL_FILTER_IDS
from pipeline.etl.mi_master_registry import apply_record_rules

def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFC", str(value)).strip()
    if not text or text.lower() == "nan":
        return None
    if text in {"#N/A", "N/A", "NA"}:
        return None
    return text.replace("위너프A+", "위너프에이플러스")


def normalize_for_match(value: Any) -> str:
    text = clean_text(value) or ""
    return re.sub(r"\s+", "", text).upper().replace("_", "-")


def contains_excluded(value: Any) -> bool:
    return contains_exclusion_marker(value)


def _is_class_header(header: Any) -> bool:
    text = clean_text(header)
    if not text:
        return False
    normalized = re.sub(r"[\s_-]+", "", text).lower()
    return normalized in {"class", "class1", "class2"} or normalized.startswith("class")


def _class_source_indexes(headers: list[Any] | tuple[Any, ...], metadata: dict[str, dict[str, Any]]) -> set[int]:
    indexes: set[int] = {idx for idx, header in enumerate(headers) if _is_class_header(header)}
    for target in ("class", "class_1", "class_2"):
        spec = metadata.get(target) or {}
        if spec.get("position") is not None:
            try:
                indexes.add(int(spec["position"]))
            except (TypeError, ValueError):
                pass
            continue
        source_column = clean_text(spec.get("source_column"))
        if not source_column:
            continue
        for idx, header in enumerate(headers):
            text = clean_text(header)
            if text and (text == source_column or text.startswith(source_column)):
                indexes.add(idx)
    return indexes


def classify_exclusion_cells(
    headers: list[Any] | tuple[Any, ...],
    values: list[Any] | tuple[Any, ...],
    class_indexes: set[int] | None = None,
    *,
    strategic_market_id: str | None = None,
    sheet_name: str | None = None,
) -> tuple[bool, bool]:
    class_indexes = set(class_indexes or ())
    if not class_indexes:
        class_indexes = {idx for idx, header in enumerate(headers) if _is_class_header(header)}
    return classify_exclusion_cells_by_policy(
        values,
        class_indexes=class_indexes,
        strategic_market_id=strategic_market_id,
        sheet_name=sheet_name,
    )


def null_if_excluded(value: Any) -> str | None:
    return None if contains_excluded(value) else clean_text(value)

def parse_json_array(value: Any) -> list[str]:
    text = clean_text(value)
    if text is None:
        return []
    parsed = json.loads(text)
    if not isinstance(parsed, list):
        raise ValueError(f"expected JSON array string, found={text!r}")
    return [str(item) for item in parsed]


def extract_atc_code(value: Any) -> str | None:
    text = clean_text(value)
    if text is None:
        return None
    bracket = re.search(r"\[([A-Z0-9]+)\]", text.upper())
    if bracket:
        return bracket.group(1)
    plain = re.search(r"\b([A-Z][0-9][A-Z0-9]{2,3})\b", text.upper())
    return plain.group(1) if plain else text


def dumps_json_array(values: list[str]) -> str | None:
    cleaned = sorted({str(value).strip().upper() for value in values if str(value).strip()})
    return json.dumps(cleaned, ensure_ascii=False) if cleaned else None


def canonical_nhi(value: Any) -> str | None:
    text = normalize_for_match(value)
    if not text:
        return None
    if text in {"급여", "NHI"}:
        return "NHI"
    if text in {"비급여", "NON-NHI", "NONNHI"}:
        return "NON-NHI"
    return text


def match_text(actual: Any, expected: str, *, mode: str = "exact") -> bool:
    actual_text = normalize_for_match(actual)
    expected_text = normalize_for_match(expected)
    if not actual_text or not expected_text:
        return False
    if mode == "contains":
        return expected_text in actual_text
    if mode == "prefix":
        return actual_text.startswith(expected_text)
    return actual_text == expected_text


def field_matches(row: dict[str, Any], field: str, values: list[str] | str | None) -> bool:
    if values is None or values == []:
        return True
    expected_values = values if isinstance(values, list) else [values]
    if not expected_values:
        return True

    if field == "atc3":
        actual_atc = extract_atc_code(row.get("atc4_code"))
        return any(match_text(actual_atc, expected, mode="prefix") for expected in expected_values)
    if field == "atc4":
        actual_atc = extract_atc_code(row.get("atc4_code"))
        return any(match_text(actual_atc, expected, mode="exact") for expected in expected_values)
    if field == "molecule":
        return any(match_text(row.get("molecule"), expected, mode="contains") for expected in expected_values)
    if field == "class":
        actual = row.get("class")
        for expected in expected_values:
            if "/" in expected:
                if match_text(actual, expected, mode="exact"):
                    return True
            elif match_text(actual, expected, mode="exact") or match_text(actual, expected, mode="prefix"):
                return True
        return False
    if field == "nhi":
        actual_nhi = canonical_nhi(row.get("nhi_type"))
        return any(actual_nhi == canonical_nhi(expected) for expected in expected_values)
    if field == "dosage_form":
        return any(match_text(row.get("dosage_form"), expected, mode="exact") for expected in expected_values)
    raise ValueError(f"unknown filter field: {field}")


def cd_filter_conditions(filter_row: dict[str, Any]) -> dict[str, list[str] | str | None]:
    return {
        "atc3": parse_json_array(filter_row.get("atc3")),
        "atc4": parse_json_array(filter_row.get("atc4")),
        "molecule": parse_json_array(filter_row.get("molecule")),
        "class": parse_json_array(filter_row.get("class")),
        "nhi": clean_text(filter_row.get("nhi")),
        "dosage_form": clean_text(filter_row.get("dosage_form")),
    }


def is_sheet_total_filter(cd_filter_id: str, conditions: dict[str, Any]) -> bool:
    return cd_filter_id in SHEET_TOTAL_FILTER_IDS and all(not value for value in conditions.values())


def assign_cd_id(
    row: dict[str, Any],
    cd_markets_for_ml: dict[str, list[dict[str, Any]]],
    filter_by_id: dict[str, dict[str, Any]],
) -> tuple[str | None, list[str]]:
    candidates: list[str] = []
    for cd_market in cd_markets_for_ml.get(str(row["ml_id"]), []):
        cd_filter_id = str(cd_market["cd_filter_id"])
        conditions = cd_filter_conditions(filter_by_id[cd_filter_id])
        if is_sheet_total_filter(cd_filter_id, conditions):
            candidates.append(str(cd_market["cd_id"]))
            continue
        if all(field_matches(row, field, value) for field, value in conditions.items() if value):
            candidates.append(str(cd_market["cd_id"]))
    if len(candidates) == 1:
        return candidates[0], candidates
    if len(candidates) == 0:
        return None, candidates
    return None, candidates

def source_version_from_ml_market(ml_rows: list[dict[str, Any]]) -> str:
    versions = {clean_text(row.get("source_file_version")) for row in ml_rows}
    versions.discard(None)
    if len(versions) != 1:
        raise ValueError(f"ml_market source_file_version must be single-valued: {sorted(versions)}")
    return str(next(iter(versions)))


def first_present(*values: Any) -> str | None:
    for value in values:
        text = null_if_excluded(value)
        if text is not None:
            return text
    return None


def source_value_by_header(headers: list[Any] | tuple[Any, ...], values: list[Any] | tuple[Any, ...], source_column: str) -> Any:
    target = str(source_column).strip()
    for header, value in zip(headers, values):
        if header is not None and str(header).strip().startswith(target):
            return value
    return None


def make_name(
    standard_values: dict[str, Any],
    source_row_id: int,
    *,
    sheet_name: str | None = None,
) -> str:
    candidate = {
        "name": first_present(
            standard_values.get("product_name"),
            standard_values.get("molecule"),
            standard_values.get("atc4_code"),
        ),
        **standard_values,
    }
    if sheet_name is not None:
        candidate = apply_record_rules(
            candidate,
            stage="strategic_brand_name",
            context={"sheet_name": sheet_name},
        )
    name = first_present(candidate.get("name"))
    if name is None:
        name = f"unknown_row_{source_row_id}"
    return name


def strategic_fields(
    standard_values: dict[str, Any],
    extras: dict[str, Any],
    *,
    sheet_name: str | None = None,
) -> dict[str, str | None]:
    class_2_value = first_present(standard_values.get("class_2"), standard_values.get("class"), extras.get("class_raw"))
    class_1_value = first_present(standard_values.get("class_1"))
    if class_1_value is None and first_present(standard_values.get("class_2")) is not None:
        class_1_value = first_present(standard_values.get("class"))
    fields = {
        "class": class_2_value,
        "class_1": class_1_value,
        "class_2": class_2_value if first_present(standard_values.get("class_2")) is not None else None,
        "molecule": first_present(standard_values.get("molecule")),
        "dosage_form": first_present(standard_values.get("dosage_form"), extras.get("administration_route")),
        "strength_pack": first_present(standard_values.get("strength"), standard_values.get("pack_desc"), extras.get("product_pack")),
        "nhi_type": first_present(standard_values.get("nhi_type")),
        "ox_gx": first_present(standard_values.get("ox_gx"), extras.get("ox_gx"), extras.get("ox_gx_biosimilar")),
        "fish_oil": first_present(extras.get("fish_oil_yn")),
        "판매사": first_present(standard_values.get("seller")),
        "제조사": first_present(standard_values.get("manufacturer")),
        "atc4_code": first_present(standard_values.get("atc4_code")),
    }
    if sheet_name is None:
        return fields
    return apply_record_rules(
        fields,
        stage="strategic_brand_fields",
        context={"sheet_name": sheet_name},
    )
