from __future__ import annotations

import pandas as pd

from pipeline.etl.io.mart.general_history import build_audit_code_matrix
from pipeline.etl.io.mart.general_rows import build_brand_rows


def _base_rows(source: str = "iqvia_nsa") -> list[dict[str, object]]:
    return [
        {
            "brand_key": "livalo",
            "brand_name": "리바로",
            "product_name": "리바로정",
            "product_code": "LIVALO TAB",
            "atc4_code": "C10A1",
            "atc4_desc": "Statins",
            "period_yyyymm": "2025-Q4",
            "raw_value": 100.0,
            "raw_sales": 100.0,
            "audit_code": "KPA",
            "channel": "KPA" if source == "iqvia_nsa" else "HOSPITAL",
            "specialty": None if source == "iqvia_nsa" else "CARDIO",
            "manufacturer": "JW",
            "company": "JW",
            "payload_static": {"MFR NAME KOR": "JW"},
        },
        {
            "brand_key": "livalo",
            "brand_name": "리바로",
            "product_name": "리바로정",
            "product_code": "LIVALO TAB",
            "atc4_code": "C10A1",
            "atc4_desc": "Statins",
            "period_yyyymm": "2026-Q1",
            "raw_value": 25.0,
            "raw_sales": 25.0,
            "audit_code": "KHPA",
            "channel": "KHPA" if source == "iqvia_nsa" else "CLINIC",
            "specialty": None if source == "iqvia_nsa" else "CARDIO",
            "manufacturer": "JW",
            "company": "JW",
            "payload_static": {"MFR NAME KOR": "JW"},
        },
    ]


def test_build_audit_code_matrix_sums_by_audit_and_omits_missing_periods() -> None:
    frame = pd.DataFrame(
        [
            {"audit_code": "KPA", "period_yyyymm": "2025-Q4", "raw_value": 100.0},
            {"audit_code": "KPA", "period_yyyymm": "2025-Q4", "raw_value": 7.5},
            {"audit_code": "KHPA", "period_yyyymm": "2026-Q1", "raw_value": 25.0},
            {"audit_code": "", "period_yyyymm": "2026-Q1", "raw_value": 99.0},
            {"audit_code": None, "period_yyyymm": "2026-Q1", "raw_value": 99.0},
        ]
    )

    matrix = build_audit_code_matrix(frame, ["2025-Q4", "2026-Q1", "2026-Q2"])

    assert matrix == {
        "KHPA": {"2026-Q1": 25.0},
        "KPA": {"2025-Q4": 107.5},
    }
    assert "2026-Q2" not in matrix["KPA"]


def test_build_brand_rows_adds_iqvia_audit_code_matrix_only_for_iqvia() -> None:
    rows = build_brand_rows("iqvia_nsa", "sales", pd.DataFrame(_base_rows()), {})

    assert len(rows) == 1
    assert rows[0]["audit_code_matrix"] == {
        "KHPA": {"2026-Q1": 25.0},
        "KPA": {"2025-Q4": 100.0},
    }
    assert rows[0]["channel_specialty_matrix"] == {}
    assert rows[0]["raw_value_history"] == {"2025-Q4": 100.0, "2026-Q1": 25.0}


def test_build_brand_rows_keeps_ubist_channel_matrix_and_leaves_audit_empty() -> None:
    rows = build_brand_rows("ubist", "sales", pd.DataFrame(_base_rows(source="ubist")), {})

    assert len(rows) == 1
    assert rows[0]["audit_code_matrix"] == {}
    assert rows[0]["channel_specialty_matrix"] == {
        "CLINIC": {"CARDIO": {"2025-Q4": 0.0, "2026-Q1": 25.0}},
        "HOSPITAL": {"CARDIO": {"2025-Q4": 100.0, "2026-Q1": 0.0}},
    }
