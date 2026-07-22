from __future__ import annotations

# noqa: SIZE_OK - Self-contained stdlib XLSX parser plus MI Master group compiler; split after production schema is approved.

from collections import defaultdict
from collections.abc import Sequence
import json
import os
from pathlib import Path
import posixpath
import re
import unicodedata
import xml.etree.ElementTree as ET
from zipfile import ZipFile

import pymysql

from .data_source import SCHEMA, connect_mariadb, read_env_file
from .market_scope import scope_id
from .models import JsonValue


REPO_ROOT = Path(__file__).resolve().parents[5]
MI_MASTER_GLOB = "MI*Master*.xlsx"
TARGET_SHEET = "시장정의 & Target"
MARKET_DEFINITION_TABLE = "stg_master_market_definition"
GROUP_ID_BY_SHEET = {
    "가드렛 가드메트": "gardlet_family",
    "리바로 리바로젯": "livalo_family",
    "리바로하이 리바로브이": "livalo_high_family",
    "트루패스 피나스타 제이다트": "thrupas_family",
    "페린젝트 베노훼럼": "ferinject_family",
    "위너프 위너프A+": "winuf_family",
    "뉴트로진 모빌리아": "neutrogin_mobilia_family",
}
FALLBACK_NAMES = {
    "A02B2": "PPI(위산분비억제제)",
    "A03F0": "위장관운동촉진제",
    "A06B1": "장세정제",
    "A07E9": "염증성장질환기타",
    "B01C5": "항혈소판제복합",
    "L03A1": "G-CSF",
    "L04B0": "항TNF제제",
    "L04D0": "JAK억제제",
    "M01C0": "특수항류마티스제",
    "K01A3": "PLAJU OP Market",
    "V03G2": "FOSRENOL Market",
    "V06D0": "ENCOVER Market",
}


def build_market_group_map(markets: Sequence[str]) -> dict[str, JsonValue]:
    """Build the MI Master-derived market group map for the requested ATC4 markets."""
    records = None if _force_workbook_source() else _records_from_db()
    source: dict[str, JsonValue]
    if records is None:
        master_path = resolve_mi_master_path()
        if master_path is None:
            return _fallback_group_map(markets)
        sheet_names, target_rows = _read_target_sheet(master_path)
        records = _target_records(target_rows, sheet_names)
        source = {
            "type": "mi_master_xlsx",
            "path": str(master_path),
            "target_sheet": TARGET_SHEET,
            "product_row_label": "PRODUCT NAME KOR",
            "atc4_row_label": "ATC 4 CODE",
        }
    else:
        source = {
            "type": "db",
            "schema": os.environ.get("MARKET_GROUP_SCHEMA", SCHEMA),
            "table": MARKET_DEFINITION_TABLE,
            "target_sheet": TARGET_SHEET,
            "product_row_label": "PRODUCT NAME KOR",
            "atc4_row_label": "ATC 4 CODE",
        }
    groups = _groups_from_records(records, set(markets))
    atc4_map = _atc4_map(markets, groups)
    return {
        "source": source,
        "rule": "MI Master sheet grouping is additive metadata; source ATC4 and CSD market remain unchanged per row.",
        "atc4_map": atc4_map,
        "groups": groups,
        "group_scope_ids": [
            f"group:{group['group_id']}"
            for group in groups
            if isinstance(group.get("current_atc4"), list) and len(group["current_atc4"]) > 1
        ],
        "sanity_checks": _sanity_checks(atc4_map),
        "mi_master_missing_atc4": sorted(atc4 for atc4, row in atc4_map.items() if row.get("source") == "fallback_atc4_drug_class"),
        "additional_mi_master_groups": _additional_group_flags(groups),
    }


def apply_csd_market_names(group_map: dict[str, JsonValue], csd_bridge: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Return a group map whose display labels come from CSD English market names."""
    atc4_map = {atc4: dict(_dict(row)) for atc4, row in _dict(group_map.get("atc4_map")).items()}
    bridge_map = {atc4: _dict(row) for atc4, row in _dict(csd_bridge.get("atc4_map")).items()}
    for atc4, row in atc4_map.items():
        bridge = bridge_map.get(atc4, {})
        csd_market = str(bridge.get("csd_market") or "")
        display_name = csd_market or f"{atc4} (CSD market missing)"
        row["mi_master_group_name"] = row.get("group_name")
        row["mi_master_submarket_name"] = row.get("submarket_name")
        row["csd_market"] = csd_market or None
        row["csd_market_missing"] = not bool(csd_market)
        row["csd_market_candidates"] = bridge.get("csd_market_candidates") if isinstance(bridge.get("csd_market_candidates"), list) else []
        row["submarket_name"] = display_name
        row["friendly_name"] = display_name
        row["filter_options"] = _rename_filter_options(_list(row.get("filter_options")), atc4, display_name, None)
    groups = [_renamed_group(_dict(group), atc4_map) for group in _list(group_map.get("groups"))]
    group_label_by_id = {str(group.get("group_id")): str(group.get("group_name") or "") for group in groups}
    for atc4, row in atc4_map.items():
        group_id = str(row.get("group_id") or "")
        group_label = group_label_by_id.get(group_id)
        row["group_name"] = group_label or row.get("group_name")
        row["filter_options"] = _rename_filter_options(_list(row.get("filter_options")), atc4, str(row.get("friendly_name") or atc4), group_label)
    renamed = dict(group_map)
    renamed["atc4_map"] = atc4_map
    renamed["groups"] = groups
    renamed["csd_market_name_source"] = csd_bridge
    renamed["csd_market_missing_atc4"] = _list(csd_bridge.get("csd_market_missing_atc4"))
    renamed["dropped_atc4_csd_missing"] = []
    renamed["csd_optional_fallback_atc4"] = _csd_missing_atc4(atc4_map)
    renamed["csd_markets_without_keyword_data"] = _csd_markets_without_keyword_data(csd_bridge, atc4_map)
    renamed["rule"] = f"{group_map.get('rule')} Final markets use MI Master membership; CSD supplies optional English display names. Missing CSD names use an explicit ATC fallback and remain executable."
    return renamed


def resolve_mi_master_path() -> Path | None:
    """Resolve the preferred MI Master workbook without using lock files."""
    candidates = [path for root in (REPO_ROOT / "docs/reference", REPO_ROOT / "data") for path in root.rglob(MI_MASTER_GLOB)]
    candidates = [path for path in candidates if path.is_file() and not path.name.startswith("~$")]
    preferred = [path for path in candidates if "원본파일" in path.name or "2026.05.18" in path.name]
    ranked = preferred or candidates
    return sorted(ranked, key=lambda path: (0 if "docs/reference" in str(path) else 1, len(path.parts), str(path)))[0] if ranked else None


def scope_metadata_from_group_map(group_map: dict[str, JsonValue]) -> dict[str, dict[str, JsonValue]]:
    """Create final execution metadata from MI Master market membership and CSD coverage."""
    atc4_map = _dict(group_map.get("atc4_map"))
    metadata: dict[str, dict[str, JsonValue]] = {}
    covered_atc4: set[str] = set()
    for item in _list(group_map.get("groups")):
        group = _dict(item)
        current_atc4 = _requested_atc4(_strings(group.get("current_atc4")), atc4_map)
        if not current_atc4:
            continue
        scope_key = f"group:{group.get('group_id')}" if len(current_atc4) > 1 else current_atc4[0]
        missing_atc4 = _missing_csd_values(current_atc4, atc4_map)
        metadata[scope_key] = {
            "scope_key": scope_key,
            "scope_id": scope_key if scope_key.startswith("group:") else scope_id(scope_key),
            "scope_type": "market_group" if len(current_atc4) > 1 else "standalone",
            "display_name": _scope_display_name(scope_key, current_atc4, group, atc4_map),
            "group_id": group.get("group_id"),
            "group_name": group.get("group_name"),
            "submarket_name": _dict(atc4_map.get(current_atc4[0])).get("submarket_name") if len(current_atc4) == 1 else None,
            "atc4_values": current_atc4,
            "filter_options": [_market_filter_option(scope_key, _scope_display_name(scope_key, current_atc4, group, atc4_map), current_atc4)],
            "source": "mi_master_market_scope",
            "csd_market_missing": bool(missing_atc4),
            "csd_market_missing_atc4": missing_atc4,
            "display_name_source": "mi_master_group" if scope_key.startswith("group:") else _display_name_source(current_atc4, atc4_map),
        }
        covered_atc4.update(current_atc4)
    for atc4, value in atc4_map.items():
        row = _dict(value)
        if atc4 in covered_atc4:
            continue
        metadata[atc4] = {
            "scope_key": atc4,
            "scope_id": scope_id(atc4),
            "scope_type": "standalone",
            "display_name": row.get("friendly_name") or row.get("submarket_name") or atc4,
            "group_id": row.get("group_id"),
            "group_name": row.get("group_name"),
            "submarket_name": row.get("submarket_name"),
            "atc4_values": [atc4],
            "filter_options": [_market_filter_option(atc4, str(row.get("friendly_name") or row.get("submarket_name") or atc4), [atc4])],
            "source": row.get("source"),
            "csd_market_missing": bool(row.get("csd_market_missing")),
            "csd_market_missing_atc4": [atc4] if row.get("csd_market_missing") else [],
            "display_name_source": "atc4_fallback" if row.get("csd_market_missing") else "csd_market",
        }
    return metadata


def group_scope_keys(group_map: dict[str, JsonValue]) -> tuple[str, ...]:
    """Return MI Master multi-ATC4 market scope keys retained for compatibility."""
    return tuple(key for key, row in scope_metadata_from_group_map(group_map).items() if row.get("scope_type") == "market_group")


def source_scope_key_from_brand_sample_key(sample_key: str) -> tuple[str, str, str]:
    """Parse ATC4 and group brand-share sample keys without losing source ATC4."""
    if sample_key.startswith("group:"):
        prefix, group_id, atc4, brand = sample_key.split(":", 3)
        return f"{prefix}:{group_id}", atc4, brand
    atc4, brand = sample_key.split(":", 1)
    return atc4, atc4, brand


def _requested_atc4(values: list[str], atc4_map: dict[str, JsonValue]) -> list[str]:
    """Return requested group members regardless of optional CSD display coverage."""
    return [atc4 for atc4 in values if atc4 in atc4_map]


def _missing_csd_values(atc4_values: list[str], atc4_map: dict[str, JsonValue]) -> list[str]:
    """Return scope members whose CSD display-name bridge is absent."""
    return [atc4 for atc4 in atc4_values if _dict(atc4_map.get(atc4)).get("csd_market_missing")]


def _display_name_source(atc4_values: list[str], atc4_map: dict[str, JsonValue]) -> str:
    """Describe whether one standalone label came from CSD or ATC fallback."""
    return "atc4_fallback" if _missing_csd_values(atc4_values, atc4_map) else "csd_market"


def _scope_display_name(scope_key: str, atc4_values: list[str], group: dict[str, JsonValue], atc4_map: dict[str, JsonValue]) -> str:
    """Choose the CSD English display name for a final MI Master market scope."""
    if scope_key.startswith("group:"):
        return str(group.get("group_name") or scope_key)
    row = _dict(atc4_map.get(atc4_values[0])) if atc4_values else {}
    return str(row.get("friendly_name") or row.get("submarket_name") or scope_key)


def _market_filter_option(scope_key: str, label: str, atc4_values: list[str]) -> dict[str, JsonValue]:
    """Build one final-market filter option without reintroducing submarket scopes."""
    return {"option_id": scope_key, "label": label, "option_type": "mi_master_market", "atc4_values": atc4_values}


def _read_target_sheet(path: Path) -> tuple[list[str], list[dict[int, str]]]:
    """Read MI Master sheet names and target-sheet rows using only stdlib XLSX XML."""
    with ZipFile(path) as archive:
        shared_strings = _shared_strings(archive)
        sheets = _workbook_sheets(archive)
        sheet_names = [name for name, _target in sheets]
        target = dict(sheets)[TARGET_SHEET]
        sheet_path = _xlsx_path("xl", target)
        rows = _worksheet_rows(archive, sheet_path, shared_strings)
    return sheet_names, rows


def _shared_strings(archive: ZipFile) -> list[str]:
    """Read shared strings from the XLSX archive."""
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    strings: list[str] = []
    for si in root.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}si"):
        strings.append("".join(text.text or "" for text in si.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")))
    return strings


def _workbook_sheets(archive: ZipFile) -> list[tuple[str, str]]:
    """Return workbook sheet names and XML targets."""
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    relmap = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
    rows: list[tuple[str, str]] = []
    rel_ns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    for sheet in workbook.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheet"):
        rows.append((sheet.attrib["name"], relmap[sheet.attrib[rel_ns]]))
    return rows


def _worksheet_rows(archive: ZipFile, sheet_path: str, shared_strings: list[str]) -> list[dict[int, str]]:
    """Read worksheet rows as column-indexed strings."""
    root = ET.fromstring(archive.read(sheet_path))
    rows: list[dict[int, str]] = []
    for row in root.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row"):
        values: dict[int, str] = {}
        for cell in row.findall("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}c"):
            ref = cell.attrib.get("r", "")
            value = cell.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v")
            text = "" if value is None else value.text or ""
            if cell.attrib.get("t") == "s" and text:
                text = shared_strings[int(text)]
            values[_column_index(ref)] = _clean(text)
        rows.append(values)
    return rows


def _target_records(target_rows: list[dict[int, str]], sheet_names: list[str]) -> list[dict[str, JsonValue]]:
    """Extract PRODUCT NAME KOR and ATC 4 CODE column records from the target sheet."""
    product_row = next(row for row in target_rows if "PRODUCT NAME KOR" in row.values())
    atc4_row = next(row for row in target_rows if "ATC 4 CODE" in row.values())
    records: list[dict[str, JsonValue]] = []
    market_sheet_names = [name for name in sheet_names if name not in {"JW 전체_JWP MKT 구분", TARGET_SHEET, "정의 상세 ▶"}]
    for column, product_label in sorted(product_row.items()):
        if column < 2 or not product_label:
            continue
        atc4_values = _atc4_values(atc4_row.get(column, ""))
        if not atc4_values:
            continue
        sheet_name = _best_sheet_name(product_label, market_sheet_names)
        records.append({"column": _column_name(column), "product_label": product_label, "atc4_values": list(atc4_values), "sheet_name": sheet_name})
    return records


def _force_workbook_source() -> bool:
    """Return true when verification needs the legacy workbook path."""
    value = os.environ.get("MARKET_GROUP_FORCE_WORKBOOK", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _records_from_db() -> list[dict[str, JsonValue]] | None:
    """Read MI Master market definition rows from the local stage DB when available."""
    schema = os.environ.get("MARKET_GROUP_SCHEMA", SCHEMA)
    sql = f"""
        SELECT strategic_market_id, market_name, market_atc_codes_json,
               full_market_atc4_codes_json, raw_row_json, source_sheet, source_file_version
        FROM `{schema}`.`{MARKET_DEFINITION_TABLE}`
        WHERE source_sheet = %s
        ORDER BY strategic_market_id
    """
    try:
        connection = connect_mariadb(read_env_file())
        try:
            with connection.cursor() as cursor:
                cursor.execute("START TRANSACTION READ ONLY")
                cursor.execute(sql, (TARGET_SHEET,))
                rows = cursor.fetchall()
                cursor.execute("COMMIT")
        finally:
            connection.close()
    except (OSError, KeyError, pymysql.MySQLError):
        return None
    records = _records_from_market_definition_rows(rows)
    return records or None


def _records_from_market_definition_rows(rows: Sequence[dict[str, JsonValue]]) -> list[dict[str, JsonValue]]:
    """Convert DB master rows back to the workbook target-record shape."""
    records: list[dict[str, JsonValue]] = []
    for row in rows:
        market_name = str(row.get("market_name") or "")
        raw_row_json = row.get("raw_row_json")
        if not market_name or not isinstance(raw_row_json, str):
            continue
        try:
            raw_payload = json.loads(raw_row_json)
        except json.JSONDecodeError:
            continue
        for column in _list(_dict(raw_payload).get("columns")):
            item = _dict(column)
            product_label = _clean(_raw_value(item, "PRODUCT NAME KOR"))
            atc4_values = _atc4_values(_raw_value(item, "ATC 4 CODE"))
            column_id = item.get("column_id")
            if not isinstance(column_id, int) or not product_label or not atc4_values:
                continue
            records.append(
                {
                    "column": _column_name(column_id - 1),
                    "product_label": product_label,
                    "atc4_values": list(atc4_values),
                    "sheet_name": market_name,
                }
            )
    return records


def _raw_value(column: dict[str, JsonValue], label: str) -> str:
    """Return one labeled value from a raw_row_json column payload."""
    for value in _list(column.get("values")):
        item = _dict(value)
        if item.get("label") == label:
            return _clean(str(item.get("value") or ""))
    return ""


def _groups_from_records(records: list[dict[str, JsonValue]], current_markets: set[str]) -> list[dict[str, JsonValue]]:
    """Group target records by MI Master sheet name and flag current market coverage."""
    grouped: defaultdict[str, list[dict[str, JsonValue]]] = defaultdict(list)
    for record in records:
        grouped[str(record["sheet_name"])].append(record)
    groups: list[dict[str, JsonValue]] = []
    for sheet_name, items in sorted(grouped.items()):
        atc4_values = sorted({atc4 for item in items for atc4 in _strings(item.get("atc4_values"))})
        current_atc4 = [atc4 for atc4 in atc4_values if atc4 in current_markets]
        if not current_atc4:
            continue
        groups.append(
            {
                "group_id": GROUP_ID_BY_SHEET.get(sheet_name, _slug(sheet_name)),
                "group_name": sheet_name,
                "atc4_values": atc4_values,
                "current_atc4": current_atc4,
                "is_multi_atc4_group": len(current_atc4) > 1,
                "members": items,
                "source": f"{TARGET_SHEET} columns",
                "flag": "additional_mi_master_group" if sheet_name not in {"리바로 리바로젯", "가드렛 가드메트"} and len(current_atc4) > 1 else "",
            }
        )
    return groups


def _atc4_map(markets: Sequence[str], groups: list[dict[str, JsonValue]]) -> dict[str, dict[str, JsonValue]]:
    """Map every requested ATC4 to its MI Master group or fallback drug-class name."""
    result: dict[str, dict[str, JsonValue]] = {}
    for group in groups:
        group_id = str(group["group_id"])
        group_name = str(group["group_name"])
        current_atc4 = _strings(group.get("current_atc4"))
        is_grouped = len(current_atc4) > 1
        for atc4 in current_atc4:
            submarket_name = _submarket_name(group, atc4)
            filter_options = [{"option_id": scope_id(atc4), "label": submarket_name, "option_type": "source_atc4", "atc4_values": [atc4]}]
            if is_grouped:
                filter_options.append({"option_id": f"group:{group_id}", "label": group_name, "option_type": "group_union", "atc4_values": current_atc4})
            result[atc4] = {
                "group_id": group_id,
                "group_name": group_name,
                "submarket_name": submarket_name,
                "friendly_name": submarket_name,
                "is_grouped": is_grouped,
                "source": "mi_master_target_sheet",
                "filter_options": filter_options,
                "source_atc4_preserved": True,
            }
    for atc4 in markets:
        if atc4 not in result:
            label = FALLBACK_NAMES.get(atc4, atc4)
            result[atc4] = {
                "group_id": None,
                "group_name": None,
                "submarket_name": label,
                "friendly_name": label,
                "is_grouped": False,
                "source": "fallback_atc4_drug_class",
                "filter_options": [{"option_id": scope_id(atc4), "label": label, "option_type": "source_atc4", "atc4_values": [atc4]}],
                "source_atc4_preserved": True,
            }
    return {atc4: result[atc4] for atc4 in markets}


def _submarket_name(group: dict[str, JsonValue], atc4: str) -> str:
    """Infer the source-market label for one ATC4 from the MI Master product label."""
    for member in _list(group.get("members")):
        item = _dict(member)
        atc4_values = _strings(item.get("atc4_values"))
        if atc4 not in atc4_values:
            continue
        product_label = str(item.get("product_label") or "")
        parts = _product_parts(product_label)
        if len(parts) == len(atc4_values) and atc4_values.index(atc4) < len(parts):
            return parts[atc4_values.index(atc4)]
        return product_label.replace("/", " ").strip()
    return str(group.get("group_name") or atc4)


def _sanity_checks(atc4_map: dict[str, dict[str, JsonValue]]) -> dict[str, JsonValue]:
    """Record the two PL-mandated sanity checks with exact ATC4 evidence."""
    livalo = _pair_group_check(atc4_map, "C10A1", "C10C0")
    gardlet = _pair_group_check(atc4_map, "A10N1", "A10N3")
    return {
        "livalo_C10A1_C10C0_grouped": livalo,
        "gardlet_A10N1_A10N3_grouped": gardlet,
        "status": "pass" if livalo is not False and gardlet is not False else "fail",
    }


def _pair_group_check(atc4_map: dict[str, dict[str, JsonValue]], left: str, right: str) -> bool | None:
    """Check grouping only when both reference ATC values are execution targets."""
    if left not in atc4_map or right not in atc4_map:
        return None
    return _same_group(atc4_map, left, right)


def _same_group(atc4_map: dict[str, dict[str, JsonValue]], left: str, right: str) -> bool:
    """Return true when two ATC4 rows are in the same non-empty MI Master group."""
    left_group = atc4_map.get(left, {}).get("group_id")
    right_group = atc4_map.get(right, {}).get("group_id")
    return bool(left_group and left_group == right_group)


def _additional_group_flags(groups: list[dict[str, JsonValue]]) -> list[dict[str, JsonValue]]:
    """Surface MI Master multi-ATC4 groups beyond the two explicitly requested sanity groups."""
    return [
        {"group_id": str(group.get("group_id")), "group_name": str(group.get("group_name")), "current_atc4": _strings(group.get("current_atc4"))}
        for group in groups
        if group.get("flag") == "additional_mi_master_group"
    ]


def _renamed_group(group: dict[str, JsonValue], atc4_map: dict[str, dict[str, JsonValue]]) -> dict[str, JsonValue]:
    """Rename one MI Master group to a CSD English union label when possible."""
    current_atc4 = _strings(group.get("current_atc4"))
    names = [str(atc4_map.get(atc4, {}).get("csd_market") or "") for atc4 in current_atc4]
    present_names = [name for name in names if name]
    renamed = dict(group)
    renamed["mi_master_group_name"] = group.get("group_name")
    renamed["csd_market_names"] = present_names
    renamed["csd_market_missing_atc4"] = [atc4 for atc4, name in zip(current_atc4, names, strict=False) if not name]
    if len(present_names) == len(current_atc4) and len(present_names) > 1:
        renamed["group_name"] = _combine_csd_markets(present_names)
    elif len(present_names) == 1:
        renamed["group_name"] = present_names[0]
    return renamed


def _rename_filter_options(options: list[JsonValue], atc4: str, submarket_label: str, group_label: str | None) -> list[dict[str, JsonValue]]:
    """Apply CSD labels to submarket and group-union filter options."""
    renamed: list[dict[str, JsonValue]] = []
    for option in options:
        item = dict(_dict(option))
        if item.get("option_type") == "source_atc4":
            item["label"] = submarket_label
        if item.get("option_type") == "group_union" and group_label:
            item["label"] = group_label
        item["source_atc4_preserved"] = atc4 in _strings(item.get("atc4_values"))
        renamed.append(item)
    return renamed


def _combine_csd_markets(names: list[str]) -> str:
    """Combine member CSD market names into the requested A+B Market form."""
    stems = list(dict.fromkeys(name.removesuffix(" Market") for name in names))
    return "+".join(stems) + " Market"


def _csd_missing_atc4(atc4_map: dict[str, dict[str, JsonValue]]) -> list[str]:
    """List executable Keyword ATC4 values using fallback display metadata."""
    return sorted(atc4 for atc4, row in atc4_map.items() if row.get("csd_market_missing"))


def _csd_markets_without_keyword_data(csd_bridge: dict[str, JsonValue], atc4_map: dict[str, dict[str, JsonValue]]) -> list[str]:
    """List CSD market sheets that remain outside execution because Keyword has no matching ATC4 rows."""
    all_csd = set(_strings(csd_bridge.get("all_csd_markets")))
    matched = {str(row.get("csd_market")) for row in atc4_map.values() if row.get("csd_market")}
    return sorted(all_csd - matched)


def _fallback_group_map(markets: Sequence[str]) -> dict[str, JsonValue]:
    """Return a transparent fallback map when the MI Master workbook is missing."""
    atc4_map = {
        atc4: {
            "group_id": None,
            "group_name": None,
            "submarket_name": FALLBACK_NAMES.get(atc4, atc4),
            "friendly_name": FALLBACK_NAMES.get(atc4, atc4),
            "is_grouped": False,
            "source": "fallback_atc4_drug_class",
            "filter_options": [{"option_id": scope_id(atc4), "label": FALLBACK_NAMES.get(atc4, atc4), "option_type": "source_atc4", "atc4_values": [atc4]}],
            "source_atc4_preserved": True,
        }
        for atc4 in markets
    }
    return {"source": {"type": "fallback_missing_mi_master"}, "rule": "MI Master workbook missing; no inferred grouping applied.", "atc4_map": atc4_map, "groups": [], "group_scope_ids": [], "sanity_checks": _sanity_checks(atc4_map), "mi_master_missing_atc4": list(markets), "additional_mi_master_groups": []}


def _best_sheet_name(product_label: str, sheet_names: list[str]) -> str:
    """Find the MI Master market sheet whose name contains the product label tokens."""
    parts = [_norm(part) for part in _product_parts(product_label) if len(_norm(part)) > 1]
    best = ""
    best_score = -1
    for sheet_name in sheet_names:
        sheet_norm = _norm(sheet_name)
        score = sum(1 for part in parts if part and part in sheet_norm)
        if score > best_score:
            best = sheet_name
            best_score = score
    return best or product_label


def _atc4_values(value: str) -> tuple[str, ...]:
    """Split an MI Master ATC4 cell into normalized ATC4 values."""
    values = tuple(item.strip() for item in value.replace(",", "/").split("/") if item.strip())
    return values


def _product_parts(label: str) -> list[str]:
    """Split a product label by MI Master separators while preserving product words."""
    return [part.strip() for part in re.split(r"/|·", label) if part.strip()]


def _clean(value: str) -> str:
    """Normalize workbook cell strings for deterministic comparisons."""
    return " ".join(unicodedata.normalize("NFC", value).split())


def _norm(value: str) -> str:
    """Normalize labels by removing spacing and punctuation separators."""
    return re.sub(r"[^0-9A-Za-z가-힣]+", "", _clean(value)).lower()


def _slug(value: str) -> str:
    """Build a stable fallback group id for unlisted MI Master sheets."""
    return "mi_" + "_".join(_norm(part) for part in value.split() if _norm(part))


def _xlsx_path(base: str, target: str) -> str:
    """Normalize a workbook relationship target into a zip member path."""
    return posixpath.normpath(posixpath.join(base, target.lstrip("/")))


def _column_index(cell_ref: str) -> int:
    """Convert an Excel cell reference into a zero-based column index."""
    match = re.match(r"([A-Z]+)", cell_ref)
    letters = match.group(1) if match else "A"
    index = 0
    for letter in letters:
        index = index * 26 + ord(letter) - 64
    return index - 1


def _column_name(index: int) -> str:
    """Convert a zero-based column index into an Excel column name."""
    value = index + 1
    letters = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _strings(value: JsonValue) -> list[str]:
    """Return a JSON list as strings."""
    return [str(item) for item in value] if isinstance(value, list) else []


def _dict(value: JsonValue) -> dict[str, JsonValue]:
    """Return a JSON object or an empty object."""
    return value if isinstance(value, dict) else {}


def _list(value: JsonValue) -> list[JsonValue]:
    """Return a JSON array or an empty array."""
    return value if isinstance(value, list) else []
