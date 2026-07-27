from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from jw_chat_agent_poc.service import process_observability as process_observability_module
from jw_chat_agent_poc.service.app import SessionStore, create_app
from jw_chat_agent_poc.resolver.catalog_membership import (
    StaticCatalogMembershipReader,
    TtlCatalogMembershipReader,
)
from jw_chat_agent_poc.tools.general_view_membership import (
    GeneralBrandMembership,
    StaticGeneralMembershipReader,
    TtlGeneralMembershipCache,
)
from jw_chat_agent_poc.tools.metrics.cache_live import StaticMetricsCacheReader
from jw_chat_agent_poc.tools.metrics.market_scope import MarketScopeResolver
from jw_chat_agent_poc.tools.query_layer.store import (
    MartRecord,
    StaticStrategicMartReader,
    TtlStrategicMartStore,
)


def test_session_store_aligns_conversation_capacity_with_session_capacity() -> None:
    store = SessionStore(max_sessions=3)

    assert store.conversations.observability()["max_states"] == 3


def test_runtime_memory_observability_endpoint_is_aggregate_only() -> None:
    store = SessionStore(max_sessions=3)
    store.conversations.record_exchange(
        "secret-conversation",
        "private question",
        "private answer",
    )
    resolver = MarketScopeResolver(
        cache_reader=StaticMetricsCacheReader(
            cache_brands=[{"brand": "secret-brand"}],
            market_status={},
        ),
    )
    client = TestClient(
        create_app(
            agent_factory=lambda external_mode="live": None,
            market_scope_resolver=resolver,
            store=store,
        )
    )

    response = client.get("/__runtime/observability")

    assert response.status_code == 200
    payload = response.json()
    assert payload["conversation"]["state_count"] == 1
    assert payload["conversation"]["turn_count"] == 1
    assert set(payload) == {
        "conversation",
        "strategic_mart",
        "catalog",
        "general_membership",
        "process",
    }
    assert set(payload["strategic_mart"]) >= {
        "row_count",
        "derived_point_count",
        "snapshot_age_seconds",
        "refresh_successes",
        "refresh_failures",
    }
    assert set(payload["catalog"]) >= {"row_count"}
    assert set(payload["general_membership"]) >= {
        "row_count",
        "snapshot_age_seconds",
        "refresh_successes",
        "refresh_failures",
    }
    process = payload["process"]
    assert set(process) == {
        "process_start_time",
        "process_uptime_seconds",
        "observed_at",
        "current_rss_bytes",
    }
    process_start_time = datetime.fromisoformat(process["process_start_time"])
    observed_at = datetime.fromisoformat(process["observed_at"])
    assert process_start_time.tzinfo == UTC
    assert observed_at.tzinfo == UTC
    assert process_start_time <= observed_at
    assert process["process_uptime_seconds"] >= 0
    assert process["current_rss_bytes"] is None or process["current_rss_bytes"] > 0
    rendered = repr(payload)
    assert "secret-conversation" not in rendered
    assert "private question" not in rendered
    assert "private answer" not in rendered
    assert "secret-brand" not in rendered


def test_snapshot_observability_reports_rows_points_age_and_refresh_counts() -> None:
    strategic = TtlStrategicMartStore(
        StaticStrategicMartReader(
            (
                MartRecord(
                    ml_id="ml_001",
                    brand_name="브랜드",
                    source="ubist",
                    measure="sales",
                    metric_history={"2026-01": {"raw_value": 100.0}},
                    channel_data={},
                    specialty_data={},
                    dimension_data={},
                    by_dimension={},
                ),
            )
        ),
        prewarm=False,
    )
    strategic.snapshot()

    metrics = strategic.observability()

    assert metrics["row_count"] == 1
    assert metrics["derived_point_count"] == 2
    assert metrics["snapshot_age_seconds"] is not None
    assert metrics["refresh_successes"] == 1
    assert metrics["refresh_failures"] == 0


def test_process_observability_reads_current_linux_rss_from_procfs(
    monkeypatch,
    tmp_path,
) -> None:
    proc_statm = tmp_path / "statm"
    proc_statm.write_text("100 25 0 0 0 0 0\n", encoding="ascii")
    monkeypatch.setattr(process_observability_module, "_PROC_STATM", proc_statm)
    monkeypatch.setattr(process_observability_module.os, "sysconf", lambda _name: 4096)

    metrics = process_observability_module.process_observability()

    assert metrics["current_rss_bytes"] == 25 * 4096


def test_catalog_and_general_membership_observability_reports_loaded_rows() -> None:
    catalog = TtlCatalogMembershipReader(
        StaticCatalogMembershipReader(
            (
                {
                    "brand": "브랜드",
                    "brand_alias": "",
                    "market_id": "ml_001",
                    "market_name": "시장",
                    "support_source": "general_mart",
                },
            )
        )
    )
    general = TtlGeneralMembershipCache(
        StaticGeneralMembershipReader(
            (
                GeneralBrandMembership(
                    brand_key="brand",
                    brand_name="브랜드",
                    atc4_code="C10A",
                    atc4_description="지질조절제",
                    source="ubist",
                ),
            )
        ),
        ttl_seconds=300,
    )
    catalog.brand_memberships()
    general.candidates("브랜드", "ubist")

    catalog_metrics = catalog.observability()
    general_metrics = general.observability()
    assert catalog_metrics["row_count"] == 1
    assert catalog_metrics["refresh_successes"] == 1
    assert general_metrics["row_count"] == 1
    assert general_metrics["refresh_successes"] == 1
