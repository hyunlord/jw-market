from __future__ import annotations

import json
from typing import Any

from pipeline.scripts.etl.cache_brand_elements import (
    build_brand_element_payloads,
    ensure_cache_brand_elements_table,
    parse_strength_row,
    upsert_brand_elements,
    verify_cache_brand_elements,
)


class FakeCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...] | list[Any] | None]] = []
        self._rows: list[dict[str, Any]] = []
        self.executemany_rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] | None = None) -> None:
        self.calls.append((sql, params))
        if "FROM `mart_general_brand_metric`" in sql:
            self._rows = [{"brand_name": "리바로", "atc4_code": "C10A1"}]
        elif "FROM `mart_general_filter_dimension_metric`" in sql:
            self._rows = [
                {"brand_name": "리바로", "source": "ubist", "dimension_type": "seller", "dimension_value": "JW중외제약"},
                {"brand_name": "리바로", "source": "iqvia_nsa", "dimension_type": "mfr", "dimension_value": "JW중외제약"},
            ]
        elif "agent3_brand_strength" in sql:
            self._rows = [
                {
                    "serving_brand_name": "리바로",
                    "strength_summary_json": json.dumps(
                        {"profile_display": {"headline": "strong"}, "strength_items": [{"axis": "growth"}], "limitations": []},
                        ensure_ascii=False,
                    ),
                    "generated_at": "2026-07-09 09:00:00",
                    "workflow_rev": 1,
                }
            ]
        elif "COUNT(*) AS rows_total" in sql:
            self._rows = [
                {
                    "rows_total": 1,
                    "factors_json_valid": 1,
                    "strength_json_valid": 1,
                    "rows_with_strength": 1,
                    "rows_with_atc": 1,
                }
            ]
        else:
            self._rows = []

    def executemany(self, sql: str, rows: list[tuple[Any, ...]]) -> None:
        self.calls.append((sql, None))
        self.executemany_rows.extend(rows)

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows

    def fetchone(self) -> dict[str, Any]:
        return self._rows[0]


class FakeConnection:
    def __init__(self) -> None:
        self.cursor_obj = FakeCursor()
        self.commits = 0

    def cursor(self) -> FakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.commits += 1


def test_parse_strength_row_projects_agent3_contract() -> None:
    payload = parse_strength_row(
        {
            "strength_summary_json": json.dumps(
                {"profile_display": {"headline": "strong"}, "strength_items": [1], "limitations": ["pilot"]},
                ensure_ascii=False,
            ),
            "generated_at": "2026-07-09 09:00:00",
            "workflow_rev": 7,
        }
    )

    assert payload == {
        "available": True,
        "profile_display": {"headline": "strong"},
        "strength_items": [1],
        "limitations": ["pilot"],
        "meta": {"generated_at": "2026-07-09 09:00:00", "workflow_rev": 7},
    }


def test_cache_brand_elements_builds_and_upserts_payloads() -> None:
    conn = FakeConnection()
    ensure_cache_brand_elements_table(conn)

    payloads = build_brand_element_payloads(conn, ["리바로"], agent3_schema="agent3")
    assert payloads[0].factors["atc"] == ["C10A1"]
    assert payloads[0].factors["ubist"]["seller"] == ["JW중외제약"]
    assert payloads[0].strength["available"] is True

    assert upsert_brand_elements(conn, payloads) == 1
    row = conn.cursor_obj.executemany_rows[0]
    assert row[0] == "리바로"
    assert json.loads(row[3])["atc"] == ["C10A1"]
    assert json.loads(row[4])["available"] is True
    assert verify_cache_brand_elements(conn)["rows_total"] == 1
