from __future__ import annotations

from datetime import datetime
from typing import Any

from pipeline.etl.io.catalog.cd_filter_schema import CD_FILTER_COLUMNS
from pipeline.etl.io.catalog.market_catalog_text import clean_text
import json


def dumps_json_array(values: list[str] | None) -> str | None:
    if values is None:
        return None
    cleaned = [clean_text(value) for value in values]
    if any(value is None for value in cleaned):
        raise ValueError(f"JSON array values must be non-empty strings: {values!r}")
    return json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))

def raw_filter_records(source_file_version_value: str, ingested_at: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {
            "cd_filter_id": "cdf_001",
            "name": "라베칸 라베칸듀오",
            "atc3": None,
            "atc4": None,
            "molecule": dumps_json_array(["Rabeprazole"]),
            "class": None,
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_002",
            "name": "제이클",
            "atc3": None,
            "atc4": dumps_json_array(["A06B1", "A06B2"]),
            "molecule": None,
            "class": None,
            "nhi": "NON_NHI",
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_003",
            "name": "가드렛 가드메트",
            "atc3": None,
            "atc4": dumps_json_array(["A10N3", "A10N1"]),
            "molecule": None,
            "class": None,
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_004",
            "name": "타발리스",
            "atc3": None,
            "atc4": None,
            "molecule": None,
            "class": None,
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_005",
            "name": "시그마트",
            "atc3": dumps_json_array(["C1D"]),
            "atc4": None,
            "molecule": None,
            "class": None,
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_006",
            "name": "리바로 리바로젯",
            "atc3": None,
            "atc4": None,
            "molecule": None,
            "class": None,
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_007",
            "name": "리바로페노",
            "atc3": None,
            "atc4": None,
            "molecule": None,
            "class": None,
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_008",
            "name": "리바로하이",
            "atc3": None,
            "atc4": None,
            "molecule": None,
            "class": dumps_json_array(["Statin/ARB/CCB"]),
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_009",
            "name": "리바로브이",
            "atc3": None,
            "atc4": None,
            "molecule": None,
            "class": dumps_json_array(["Statin/ARB"]),
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_010",
            "name": "트루패스",
            "atc3": None,
            "atc4": dumps_json_array(["G4C2"]),
            "molecule": None,
            "class": None,
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_011",
            "name": "피나스타 제이다트",
            "atc3": None,
            "atc4": dumps_json_array(["G4C3"]),
            "molecule": None,
            "class": None,
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_012",
            "name": "뉴트로진",
            "atc3": None,
            "atc4": dumps_json_array(["L03A1"]),
            "molecule": None,
            "class": None,
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_013",
            "name": "모빌리아",
            "atc3": None,
            "atc4": dumps_json_array(["L03A9"]),
            "molecule": None,
            "class": None,
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_014",
            "name": "악템라",
            "atc3": None,
            "atc4": None,
            "molecule": None,
            "class": None,
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_015",
            "name": "페린젝트 베노훼럼",
            "atc3": None,
            "atc4": dumps_json_array(["B03A1"]),
            "molecule": None,
            "class": None,
            "nhi": None,
            "dosage_form": "IV Iron",
        },
        {
            "cd_filter_id": "cdf_016",
            "name": "헴리브라",
            "atc3": None,
            "atc4": None,
            "molecule": None,
            "class": None,
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_017",
            "name": "엔커버",
            "atc3": None,
            "atc4": None,
            "molecule": None,
            "class": None,
            "nhi": None,
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_018",
            "name": "위너프 위너프에이플러스",
            "atc3": None,
            "atc4": dumps_json_array(["K01D2"]),
            "molecule": None,
            "class": dumps_json_array(["3CB"]),
            "nhi": "급여",
            "dosage_form": None,
        },
        {
            "cd_filter_id": "cdf_019",
            "name": "플라주오피",
            "atc3": None,
            "atc4": dumps_json_array(["K01A1", "K01A3"]),
            "molecule": None,
            "class": dumps_json_array(["Acetated"]),
            "nhi": None,
            "dosage_form": None,
        },
    ]
    return [
        {
            **row,
            "source_file_version": source_file_version_value,
            "ingested_at": ingested_at,
        }
        for row in rows
    ]
