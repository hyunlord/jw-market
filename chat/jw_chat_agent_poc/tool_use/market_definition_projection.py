from __future__ import annotations

from collections.abc import Mapping
import json

from jw_chat_agent_poc.tool_use.market_definition_catalog import CatalogRow


def analysis_dimensions(row: Mapping[str, object]) -> list[str]:
    fields = (
        ("analyze_class", "Class"),
        ("analyze_molecule", "성분"),
        ("analyze_dosage_form", "제형"),
        ("analyze_strength_pack", "함량/포장"),
        ("analyze_nhi_type", "급여 유형"),
        ("analyze_ox_gx", "오리지널/제네릭"),
        ("analyze_fish_oil", "어유 구분"),
    )
    return [label for field_name, label in fields if bool(row.get(field_name))]


def brand_inclusion(brand: str, rows: tuple[CatalogRow, ...]) -> dict[str, object] | None:
    if not brand:
        return None
    conditions: list[dict[str, object]] = []
    if rows:
        values_by_label = (
            ("ATC4", _many_json(rows, "allowed_atc4_codes_json")),
            ("Class", _many(rows, "class")),
            ("Class 1", _many(rows, "class_1")),
            ("Class 2", _many(rows, "class_2")),
            ("성분", _many(rows, "molecule")),
            ("제형", _many(rows, "dosage_form")),
            ("함량/포장", _many(rows, "strength_pack")),
            ("급여 유형", _many(rows, "nhi_type")),
            ("오리지널/제네릭", _many(rows, "ox_gx")),
            ("어유 구분", _many(rows, "fish_oil")),
        )
        conditions = [
            {"label": label, "values": list(values)}
            for label, values in values_by_label
            if values
        ]
    return {
        "brand": brand,
        "included": bool(rows),
        "matched_conditions": conditions,
    }


def class_structure(rows: tuple[CatalogRow, ...]) -> dict[str, object] | None:
    class_1 = _many(rows, "class_1")
    class_2 = _many(rows, "class_2")
    if not class_1 or not class_2:
        return None
    return {
        "catalog_axes": ["Class 1", "Class 2"],
        "class_1_values": list(class_1),
        "class_2_values": list(class_2),
        "display_axis": "Class 2",
    }


def analysis_statement(dimensions: object, source: object) -> str:
    labels = ", ".join(str(item) for item in dimensions) if isinstance(dimensions, list) else "없음"
    return f"분석 차원은 {labels}이고 데이터 출처는 {source}입니다."


def inclusion_statement(inclusion: Mapping[str, object]) -> str:
    brand = text(inclusion.get("brand"))
    conditions = inclusion.get("matched_conditions")
    if not isinstance(conditions, list) or not conditions:
        return f"{brand}의 기록된 포함 조건을 찾지 못했습니다."
    parts = []
    for condition in conditions:
        if not isinstance(condition, Mapping):
            continue
        values = condition.get("values")
        if not isinstance(values, list):
            continue
        parts.append(f"{text(condition.get('label'))} {'/'.join(str(value) for value in values)}")
    return f"{brand}는 {', '.join(parts)} 조건으로 포함됩니다."


def class_structure_statements(structure: Mapping[str, object]) -> list[str]:
    class_1 = structure.get("class_1_values")
    class_2 = structure.get("class_2_values")
    display_axis = text(structure.get("display_axis"))
    class_1_text = ", ".join(str(item) for item in class_1) if isinstance(class_1, list) else ""
    class_2_text = ", ".join(str(item) for item in class_2) if isinstance(class_2, list) else ""
    return [
        f"Class 1 구성은 {class_1_text}이고 Class 2 구성은 {class_2_text}입니다.",
        f"시각화 표시 축은 {display_axis}입니다.",
    ]


def unavailable_rationale() -> dict[str, object]:
    return {
        "available": False,
        "message": "시장 선정 사유와 의사결정 배경은 현재 카탈로그에 기록돼 있지 않습니다.",
    }


def json_strings(value: object) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(text(item) for item in value if text(item))
    if not isinstance(value, str) or not value.strip():
        return ()
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("market definition catalog contains invalid JSON") from exc
    if not isinstance(decoded, list):
        raise ValueError("market definition catalog JSON must be an array")
    return tuple(text(item) for item in decoded if text(item))


def source_label(value: object) -> str:
    normalized = text(value).lower()
    return {"iqvia": "IQVIA", "iqvia_nsa": "IQVIA", "ubist": "UBIST"}.get(
        normalized,
        normalized,
    )


def text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _many(rows: tuple[CatalogRow, ...], key: str) -> tuple[str, ...]:
    return tuple(sorted({text(row.get(key)) for row in rows if text(row.get(key))}))


def _many_json(rows: tuple[CatalogRow, ...], key: str) -> tuple[str, ...]:
    return tuple(sorted({item for row in rows for item in json_strings(row.get(key))}))
