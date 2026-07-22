from __future__ import annotations

from pathlib import Path
import time

from jw_chat_agent_poc.resolver.catalog_membership import (
    MariaDbCatalogMembershipReader,
    StaticCatalogMembershipReader,
    TtlCatalogMembershipReader,
)


def test_catalog_reader_uses_catalog_schema_env(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_CACHE_DB_NAME", "jw_mart")
    monkeypatch.setenv("CHAT_QUERY_DB_NAME", "jw_mart_d2_stage_20260630_r2")
    monkeypatch.setenv("CHAT_CATALOG_DB_NAME", "jw_mart_d2_stage_20260630_r2")

    reader = MariaDbCatalogMembershipReader()

    assert reader.database == "jw_mart_d2_stage_20260630_r2"


def test_catalog_membership_snapshot_is_cached_until_ttl() -> None:
    source = StaticCatalogMembershipReader(
        ({"brand": "마운자로", "market_id": "ml_003", "market_name": "당뇨 시장"},)
    )
    reader = TtlCatalogMembershipReader(source, ttl_seconds=300)

    first = reader.brand_memberships()
    second = reader.brand_memberships()

    assert first == second
    assert source.calls == 1


def test_membership_sql_uses_all_marts_as_presence_truth_and_catalog_as_backed_aliases() -> None:
    sql = MariaDbCatalogMembershipReader.membership_sql()

    assert "mart_strategic_ml_brand_metric" in sql
    assert "mart_general_brand_metric" in sql
    assert "catalog_strategic_brand" in sql
    assert "catalog_ml_market" in sql
    assert "LEFT JOIN catalog_ml_market" in sql
    assert "brand.brand_id = mart_brand.brand_id" in sql
    assert "brand.ml_id = mart_brand.ml_id" in sql
    assert "is_excluded = 0" in sql
    assert "strategic_mart" in sql
    assert "general_mart" in sql
    assert "catalog_alias" in sql
    assert "brand_alias" in sql
    assert "MAX(NULLIF(membership.brand_alias, ''))" not in sql
    assert "GROUP BY membership.brand, membership.brand_alias" in sql
    assert "parquet" not in sql.lower()


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
