from __future__ import annotations

from pathlib import Path
from typing import Any

from pipeline.scripts.agent3 import loader
from pipeline.scripts.agent3 import repository
from pipeline.scripts.agent3.brand_identity import (
    BrandIdentity,
    canonical_brand_names_from_rows,
    serving_brand_names_for_identities,
)
from pipeline.scripts.agent3.loader import Agent3Loader
from pipeline.scripts.agent3.repository import Agent3Repository


class FakeCursor:
    def __init__(self) -> None:
        self.sql = ""
        self.params: tuple[str, ...] = ()

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[str, ...]) -> None:
        self.sql = sql
        self.params = params

    def fetchall(self) -> list[dict[str, Any]]:
        return [{"brand_key": "BK-001", "input_hash": "abc123", "workflow_rev": 5365, "validation_failed": 1}]


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self.fake_cursor = cursor

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return self.fake_cursor


class FakeRowsCursor(FakeCursor):
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        super().__init__()
        self.rows = rows

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


def test_canonical_brand_name_prefers_highest_recent_sales_for_brand_key() -> None:
    rows = [
        {
            "brand_key": "BK-001",
            "brand_name": "낮은표시명",
            "raw_value_history": {"2026-04": 100.0},
        },
        {
            "brand_key": "BK-001",
            "brand_name": "높은표시명",
            "raw_value_history": {"2026-04": 250.0},
        },
        {
            "brand_key": "BK-002",
            "brand_name": "단일표시명",
            "raw_value_history": {"2026-04": 10.0},
        },
    ]

    assert canonical_brand_names_from_rows(rows) == {
        "BK-001": "높은표시명",
        "BK-002": "단일표시명",
    }


def test_load_existing_hashes_uses_brand_key(monkeypatch: Any) -> None:
    fake_cursor = FakeCursor()

    def fake_connect(config: object) -> FakeConnection:
        return FakeConnection(fake_cursor)

    monkeypatch.setattr(loader, "connect", fake_connect)

    result = Agent3Loader().load_existing_hashes(["BK-001"])

    assert result["BK-001"].input_hash == "abc123"
    assert result["BK-001"].workflow_rev == 5365
    assert result["BK-001"].validation_failed is True
    assert "brand_key" in fake_cursor.sql
    assert "input_hash" in fake_cursor.sql
    assert "workflow_rev" in fake_cursor.sql
    assert "validation_failed" in fake_cursor.sql
    assert "'workflow_error'" in fake_cursor.sql
    assert "WHERE brand_key IN (%s)" in " ".join(fake_cursor.sql.split())
    assert fake_cursor.params == ("BK-001",)


def test_resolve_brand_identities_uses_display_aliases(monkeypatch: Any) -> None:
    fake_cursor = FakeRowsCursor(
        [
            {
                "brand_key": "위너프에이플러스",
                "brand_name": "위너프에이플러스",
                "raw_value_history": {"2026-Q1": 42.0},
            }
        ]
    )

    def fake_connect(config: object) -> FakeConnection:
        return FakeConnection(fake_cursor)

    monkeypatch.setattr(repository, "connect", fake_connect)

    identities = Agent3Repository().resolve_brand_identities(
        ["위너프A+"],
        {"위너프A+": ("위너프에이플러스",)},
    )

    assert [(item.brand_key, item.brand_name) for item in identities] == [("위너프에이플러스", "위너프에이플러스")]
    assert fake_cursor.params == ("위너프A+", "위너프에이플러스", "위너프A+", "위너프에이플러스")


def test_agent3_brand_strength_v2_schema_uses_brand_key_primary_key() -> None:
    ddl = Path("pipeline/scripts/agent3/sql/002_recreate_agent3_brand_strength_brand_key.sql").read_text(encoding="utf-8")

    assert "brand_key VARCHAR(255) NOT NULL" in ddl
    assert "brand_name VARCHAR(255) NOT NULL" in ddl
    assert "PRIMARY KEY (brand_key)" in ddl
    assert "KEY idx_agent3_brand_strength_brand_name (brand_name)" in ddl


def test_serving_brand_name_keeps_one_representative_for_colliding_name() -> None:
    identities = [
        BrandIdentity(brand_key="BK-LOW", brand_name="충돌브랜드", latest_sales=100.0),
        BrandIdentity(brand_key="BK-HIGH", brand_name="충돌브랜드", latest_sales=250.0),
        BrandIdentity(brand_key="BK-OTHER", brand_name="다른브랜드", latest_sales=10.0),
    ]

    assert serving_brand_names_for_identities(identities) == {
        "BK-LOW": None,
        "BK-HIGH": "충돌브랜드",
        "BK-OTHER": "다른브랜드",
    }


def test_serving_brand_name_is_non_null_when_names_do_not_collide() -> None:
    identities = [
        BrandIdentity(brand_key="BK-001", brand_name="브랜드1", latest_sales=100.0),
        BrandIdentity(brand_key="BK-002", brand_name="브랜드2", latest_sales=50.0),
    ]

    assert serving_brand_names_for_identities(identities) == {
        "BK-001": "브랜드1",
        "BK-002": "브랜드2",
    }


def test_serving_brand_name_representative_is_stable_on_rerun() -> None:
    first = [
        BrandIdentity(brand_key="BK-A", brand_name="같은이름", latest_sales=200.0),
        BrandIdentity(brand_key="BK-B", brand_name="같은이름", latest_sales=200.0),
    ]
    second = list(reversed(first))

    assert serving_brand_names_for_identities(first) == serving_brand_names_for_identities(second)
    assert serving_brand_names_for_identities(first) == {
        "BK-A": "같은이름",
        "BK-B": None,
    }


def test_agent3_brand_strength_v3_schema_adds_unique_nullable_serving_name() -> None:
    ddl = Path("pipeline/scripts/agent3/sql/003_add_serving_brand_name.sql").read_text(encoding="utf-8")

    assert "serving_brand_name VARCHAR(255) NULL" in ddl
    assert "UNIQUE INDEX" in ddl
    assert "serving_brand_name" in ddl
