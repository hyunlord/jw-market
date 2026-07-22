from __future__ import annotations

from pathlib import Path
import time

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

    assert len(queries) == 3
    joined = "\n".join(queries)
    assert "mart_strategic_ml_brand_metric" in joined
    assert "chat_general_brand_membership" in joined
    assert "mart_general_brand_metric" not in joined
    assert "membership.brand_key AS brand" in joined
    assert "membership.brand_name" in joined
    assert "catalog_strategic_brand" in joined
    assert "catalog_ml_market" in joined
    assert "brand.brand_id = mart_brand.brand_id" in joined
    assert "brand.ml_id = mart_brand.ml_id" in joined
    assert "is_excluded = 0" in joined
    assert "strategic_mart" in joined
    assert "general_mart" in joined
    assert "catalog_alias" in joined
    assert "brand_alias" in joined
    assert "parquet" not in joined.lower()
    for query in queries:
        normalized = query.upper()
        assert "UNION" not in normalized
        assert "GROUP BY" not in normalized
        assert "ORDER BY" not in normalized


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
