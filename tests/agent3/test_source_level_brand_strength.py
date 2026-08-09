from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline.scripts.agent3 import brand_factors
from pipeline.scripts.agent3.run_source import _parse_brands
from pipeline.scripts.agent3.source_loader import compute_source_input_hash
from pipeline.scripts.agent3.source_processing import (
    available_sources_from_general_rows,
    extract_source_candidates,
    filter_rows_for_source,
)


def test_source_schema_uses_brand_key_source_primary_key() -> None:
    ddl = Path("pipeline/scripts/agent3/sql/004_create_agent3_brand_strength_source.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS agent3_brand_strength_source" in ddl
    assert "source VARCHAR(16) NOT NULL" in ddl
    assert "PRIMARY KEY (brand_key, source)" in ddl
    assert "KEY idx_serving_brand_source (serving_brand_name, source)" in ddl


def test_source_input_hash_includes_source() -> None:
    profile = {"brand": "리바로", "sources": ["iqvia_nsa", "ubist"]}
    candidates = [{"slice": "전체 IQVIA", "value_current": 200.0}]

    iqvia_hash = compute_source_input_hash(profile, candidates, 5365, "iqvia")
    ubist_hash = compute_source_input_hash(profile, candidates, 5365, "ubist")

    assert iqvia_hash != ubist_hash


def test_file_backed_brand_scope_preserves_every_brand(tmp_path) -> None:
    brands_file = tmp_path / "brands.json"
    brands_file.write_text('["brand-a", "brand-b"]\n', encoding="utf-8")

    assert _parse_brands(None, brands_file) == ["brand-a", "brand-b"]


def test_source_filter_keeps_only_requested_source() -> None:
    rows = [
        {"brand_name": "리바로", "source": "iqvia_nsa"},
        {"brand_name": "리바로", "source": "ubist"},
        {"brand_name": "리바로", "source": "other"},
    ]

    assert filter_rows_for_source(rows, "iqvia") == [{"brand_name": "리바로", "source": "iqvia_nsa"}]
    assert filter_rows_for_source(rows, "ubist") == [{"brand_name": "리바로", "source": "ubist"}]


def test_source_units_follow_general_sales_rows_for_both_sources() -> None:
    general_rows = [
        {"source": "iqvia_nsa", "measure": "sales"},
        {"source": "ubist", "measure": "sales"},
    ]

    assert available_sources_from_general_rows(general_rows) == ("iqvia", "ubist")


def test_source_units_follow_general_sales_rows_for_one_source() -> None:
    general_rows = [
        {"source": "iqvia_nsa", "measure": "sales"},
        {"source": "ubist", "measure": "volume"},
    ]

    assert available_sources_from_general_rows(general_rows) == ("iqvia",)


def test_source_units_do_not_depend_on_strategic_rows() -> None:
    general_rows = [{"source": "ubist", "measure": "sales"}]
    strategic_rows = [{"source": "iqvia_nsa", "measure": "sales"}]

    assert strategic_rows
    assert available_sources_from_general_rows(general_rows) == ("ubist",)


def test_source_candidate_extraction_does_not_mix_sources() -> None:
    rows = [
        {
            "brand_name": "리바로",
            "brand_key": "리바로",
            "source": "iqvia_nsa",
            "measure": "sales",
            "raw_value_history": {"2026-04": 100_000_000.0, "2026-05": 200_000_000.0},
            "channel_data": {},
            "specialty_data": {},
            "channel_specialty_matrix": {},
            "dimension_data": {"nhi_type": {"NHI": {"2026-04": 100_000_000.0, "2026-05": 200_000_000.0}}},
        },
        {
            "brand_name": "리바로",
            "brand_key": "리바로",
            "source": "ubist",
            "measure": "sales",
            "raw_value_history": {"2026-04": 100_000_000.0, "2026-05": 200_000_000.0},
            "channel_data": {"상급종합": {"2026-04": 100_000_000.0, "2026-05": 200_000_000.0}},
            "specialty_data": {},
            "channel_specialty_matrix": {},
            "dimension_data": {},
        },
    ]

    iqvia_candidates = extract_source_candidates(source="iqvia", general_rows=rows, top_n=5)
    ubist_candidates = extract_source_candidates(source="ubist", general_rows=rows, top_n=5)

    assert iqvia_candidates
    assert ubist_candidates
    assert {item["source"] for item in iqvia_candidates} == {"iqvia_nsa"}
    assert {item["source"] for item in ubist_candidates} == {"ubist"}


class FakeCursor:
    def __init__(self) -> None:
        self.sql = ""
        self.params_history: list[tuple[str, ...]] = []
        self.statement_count = 0

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[str, ...]) -> None:
        self.sql = sql
        self.params_history.append(params)
        self.statement_count += 1

    def fetchone(self) -> dict[str, Any]:
        return {"ml_id": "ml_006", "brand_key": "리바로", "brand_name": "리바로", "source": "iqvia_nsa"}

    def fetchall(self) -> list[dict[str, Any]]:
        return []


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self.cursor_instance = cursor
        cursor.connection = self

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return self.cursor_instance


def test_source_competitor_top5_delegates_to_existing_resolver(monkeypatch: Any) -> None:
    cursor = FakeCursor()
    calls = []

    def fake_connect(config: object) -> FakeConnection:
        return FakeConnection(cursor)

    def fake_resolver(
        brand_name: str,
        ml_id: str,
        cd_id: str | None,
        source: str,
        db_conn: FakeConnection,
    ) -> dict[str, Any]:
        calls.append((brand_name, ml_id, cd_id, source, db_conn))
        return {
            "top_competitors": [
                {
                    "brand_name": "로수젯",
                    "is_jw": False,
                    "latest_period": "2026-05",
                    "rank_in_market": 1,
                    "raw_value": 200.0,
                    "ms_pct": 12.3,
                }
            ]
        }

    monkeypatch.setattr(brand_factors, "connect", fake_connect)
    monkeypatch.setattr(brand_factors, "resolve_market_top5_competitors", fake_resolver)

    result = brand_factors.source_competitor_top5(brand_name="리바로", source="iqvia")

    assert [item["brand_name"] for item in result] == ["로수젯"]
    assert cursor.params_history[0] == ("리바로", "리바로", "iqvia_nsa")
    assert calls == [("리바로", "ml_006", None, "IQVIA", cursor.connection)]
