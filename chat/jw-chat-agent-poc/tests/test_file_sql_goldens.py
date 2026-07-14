from __future__ import annotations

import json
from pathlib import Path


GOLDEN_PATH = Path(__file__).parent / "goldens" / "file_sql_goldens.json"


def _contracts() -> list[dict]:
    payload = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    return payload["contracts"]


def test_file_sql_goldens_have_independent_source_truth() -> None:
    contracts = _contracts()

    assert contracts
    for contract in contracts:
        assert contract["gate_enabled"] is True
        assert contract["request"]
        assert contract["truth_basis_status"] == "confirmed"
        assert "Original XLSX direct reproduction" in contract["truth_basis"]
        assert "mock" not in contract["truth_basis"].casefold()
        assert len(contract["source"]["file_sha256"]) == 64
        assert contract["source"]["data_row_count"] > 0


def test_chso_r05a0_golden_records_correct_label_values_and_population() -> None:
    contract = next(
        item
        for item in _contracts()
        if item["id"] == "chso_r05a0_manufacturer_sell_out_compare_2026_01"
    )

    assert contract["calculation"]["filters"]["ATC 4"] == "R05A0_COLD PREPARATIONS"
    assert contract["source"]["measure_column"] == "VALUES LC SI PRICE\n1/2026"
    assert contract["expected"] == [
        {"manufacturer": "동화약품", "value": 3853883875, "applied_rows": 22},
        {"manufacturer": "동아제약", "value": 3315233364, "applied_rows": 17},
    ]


def test_a02b2_is_not_promoted_as_a_value_golden() -> None:
    serialized = json.dumps(_contracts(), ensure_ascii=False)

    assert "A02B2" not in serialized
    assert '"applied_rows": 120' not in serialized
    assert '"applied_rows": 98' not in serialized
