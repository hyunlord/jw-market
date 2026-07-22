from __future__ import annotations

from pathlib import Path
import time

import pytest

from jw_chat_agent_poc.resolver.catalog_membership import (
    MariaDbCatalogMembershipReader,
    StaticCatalogMembershipReader,
    TtlCatalogMembershipReader,
    _merge_membership_rows,
)


def test_catalog_reader_uses_catalog_schema_env(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_CACHE_DB_NAME", "jw_mart")
    monkeypatch.setenv("CHAT_QUERY_DB_NAME", "jw_mart_d2_stage_20260630_r2")
    monkeypatch.setenv("CHAT_CATALOG_DB_NAME", "jw_mart_d2_stage_20260630_r2")

    reader = MariaDbCatalogMembershipReader()

    assert reader.database == "jw_mart_d2_stage_20260630_r2"


def test_catalog_reader_default_row_cap_covers_the_live_general_mart(monkeypatch) -> None:
    monkeypatch.delenv("CHAT_CATALOG_GENERAL_MAX_ROWS", raising=False)

    reader = MariaDbCatalogMembershipReader()

    assert reader.general_max_rows == 200_000


def test_catalog_membership_snapshot_is_cached_until_ttl() -> None:
    source = StaticCatalogMembershipReader(
        ({"brand": "마운자로", "market_id": "ml_003", "market_name": "당뇨 시장"},)
    )
    reader = TtlCatalogMembershipReader(source, ttl_seconds=300)

    first = reader.brand_memberships()
    second = reader.brand_memberships()

    assert first == second
    assert source.calls == 1


def test_membership_queries_read_each_presence_source_without_cross_mart_sorting() -> None:
    queries = MariaDbCatalogMembershipReader.membership_queries()

    assert len(queries) == 2
    joined = "\n".join(queries)
    assert "mart_strategic_ml_brand_metric" in joined
    assert "mart_general_brand_metric" not in joined
    assert "chat_general_brand_membership" not in joined
    assert "catalog_strategic_brand" in joined
    assert "catalog_ml_market" in joined
    assert "brand.brand_id = mart_brand.brand_id" in joined
    assert "brand.ml_id = mart_brand.ml_id" in joined
    assert "is_excluded = 0" in joined
    assert "strategic_mart" in joined
    assert "catalog_alias" in joined
    assert "brand_alias" in joined
    assert "parquet" not in joined.lower()
    for query in queries:
        normalized = query.upper()
        assert "UNION" not in normalized
        assert "GROUP BY" not in normalized
        assert "ORDER BY" not in normalized


def test_general_membership_query_pages_the_live_mart_by_primary_key() -> None:
    query = MariaDbCatalogMembershipReader.general_membership_page_query()
    normalized = " ".join(query.split())

    assert "FROM mart_general_brand_metric AS general FORCE INDEX (PRIMARY)" in normalized
    assert "general.id > %s" in normalized
    assert "ORDER BY general.id" in normalized
    assert "LIMIT %s" in normalized
    assert "general.brand_name AS brand" in normalized
    assert "general.brand_key" in normalized
    assert "'general_mart' AS support_source" in normalized
    assert "chat_general_brand_membership" not in normalized
    assert "DISTINCT" not in normalized.upper()
    assert "GROUP BY" not in normalized.upper()


class _PagedMembershipCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []
        self._page: list[dict[str, object]] = []

    def execute(self, _query: str, params: tuple[int, int]) -> None:
        self.calls.append(params)
        after_id, limit = params
        rows = [
            {"membership_id": 1, "brand": "아일리아", "brand_alias": "EYLEA"},
            {"membership_id": 4, "brand": "로수젯", "brand_alias": "로수젯"},
            {"membership_id": 9, "brand": "마운자로", "brand_alias": "MOUNJARO"},
        ]
        self._page = [row for row in rows if int(row["membership_id"]) > after_id][:limit]

    def fetchall(self) -> list[dict[str, object]]:
        return self._page


def test_general_membership_loader_keeps_every_keyset_page() -> None:
    cursor = _PagedMembershipCursor()

    rows = MariaDbCatalogMembershipReader(general_page_size=2).load_general_membership_rows(cursor)

    assert [row["brand"] for row in rows] == ["아일리아", "로수젯", "마운자로"]
    assert cursor.calls == [(0, 2), (4, 2)]


def test_general_membership_loader_fails_closed_above_row_limit() -> None:
    cursor = _PagedMembershipCursor()
    reader = MariaDbCatalogMembershipReader(general_page_size=2, general_max_rows=2)

    with pytest.raises(RuntimeError, match="exceeds configured row limit 2"):
        reader.load_general_membership_rows(cursor)

    assert cursor.calls == [(0, 2), (4, 1)]


class _StalledMembershipCursor:
    def execute(self, _query: str, _params: tuple[int, int]) -> None:
        return None

    def fetchall(self) -> list[dict[str, object]]:
        return [{"membership_id": 1, "brand": "아일리아", "brand_alias": "EYLEA"}]


def test_general_membership_loader_fails_closed_when_primary_key_does_not_advance() -> None:
    cursor = _StalledMembershipCursor()
    reader = MariaDbCatalogMembershipReader(general_page_size=1)

    with pytest.raises(RuntimeError, match="page did not advance"):
        reader.load_general_membership_rows(cursor)


def test_membership_merge_preserves_all_markets_and_prefers_stronger_duplicate_source() -> None:
    rows = (
        {
            "brand": "리바로",
            "brand_alias": "",
            "market_id": "ml_006",
            "market_name": "고지혈증 시장",
            "support_source": "general_mart",
        },
        {
            "brand": "리바로",
            "brand_alias": "",
            "market_id": "ml_006",
            "market_name": "고지혈증 시장",
            "support_source": "strategic_mart",
        },
        {
            "brand": "리바로",
            "brand_alias": "리바로정",
            "market_id": "ml_006",
            "market_name": "고지혈증 시장",
            "support_source": "catalog_alias",
        },
        {
            "brand": "리바로",
            "brand_alias": "",
            "market_id": "",
            "market_name": "",
            "support_source": "general_mart",
        },
    )

    merged = _merge_membership_rows(rows)

    assert merged == (
        {
            "brand": "리바로",
            "brand_alias": "",
            "market_id": "",
            "market_name": "",
            "support_source": "general_mart",
        },
        {
            "brand": "리바로",
            "brand_alias": "",
            "market_id": "ml_006",
            "market_name": "고지혈증 시장",
            "support_source": "strategic_mart",
        },
        {
            "brand": "리바로",
            "brand_alias": "리바로정",
            "market_id": "ml_006",
            "market_name": "고지혈증 시장",
            "support_source": "catalog_alias",
        },
    )


def test_catalog_membership_can_prewarm_without_request_io() -> None:
    source = StaticCatalogMembershipReader(
        ({"brand": "마운자로", "market_id": "ml_003", "market_name": "당뇨 시장"},)
    )
    reader = TtlCatalogMembershipReader(source, ttl_seconds=300, prewarm=True)

    deadline = time.monotonic() + 1
    while source.calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert reader.brand_memberships()[0]["brand"] == "마운자로"
    assert source.calls == 1


def test_deployment_coordinates_all_chat_readers_on_d2() -> None:
    deployment_patch = Path(__file__).parents[1] / "deploy" / "d2-database-env-patch.yaml"
    text = deployment_patch.read_text(encoding="utf-8")

    assert text.count("jw_mart_d2_stage_20260630_r2") == 4
    for variable in (
        "CHAT_QUERY_DB_NAME",
        "CHAT_CATALOG_DB_NAME",
        "CHAT_GENERAL_MART_SCHEMA",
        "CHAT_CD_MART_SCHEMA",
    ):
        assert variable in text
    assert "CHAT_CACHE_DB_NAME" not in text
    assert "jw_mart\n" not in text
