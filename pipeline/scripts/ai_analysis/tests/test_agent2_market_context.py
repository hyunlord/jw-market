from __future__ import annotations

from datetime import datetime
import json

import pytest

from agent2_regen_orchestrator import (
    Agent2RegenOrchestrator,
    DependencyPorts,
    JsonRunStore,
    LLMCallResult,
    ValidationOutcome,
    _market_scoped_artifact_stem,
)
from agent2_density_worklist import RoutedAgent2Brand
from bundle_builder.agent2_density_router import ProcessingMode, RouteDecision
from bundle_builder.catalog_db_loader import (
    AmbiguousBrandMarketError,
    RequestedBrandMarketNotFoundError,
    load_brand_from_catalog,
)
from bundle_builder.brand_context_builder import build_brand_context
from bundle_builder.orchestrator import build_brand_bundle


class CatalogCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.connection.calls.append((normalized, params))
        if normalized.startswith("SHOW TABLES LIKE"):
            self.rows = [{"table": "catalog_strategic_brand"}]
            return
        if "FROM catalog_strategic_brand" not in normalized:
            raise AssertionError(f"unexpected query: {normalized}")

        rows = list(self.connection.rows)
        requested_name = params[0]
        if "REPLACE(LOWER(name)" in normalized:
            compact = lambda value: str(value).lower().replace(" ", "")
            rows = [row for row in rows if compact(row["name"]) == requested_name]
        else:
            rows = [row for row in rows if row["name"] == requested_name]
        if "COALESCE(is_excluded, 0) = 0" in normalized:
            rows = [row for row in rows if not row.get("is_excluded")]
        if "ml_id = %s" in normalized:
            rows = [row for row in rows if row["ml_id"] == params[-2]]
        if "strategy_id = %s" in normalized:
            rows = [row for row in rows if row["strategy_id"] == params[-1]]
        self.rows = sorted(rows, key=lambda row: row["brand_id"])

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class CatalogConnection:
    def __init__(self):
        self.calls = []
        self.rows = [
            {
                "brand_id": "sb_006_1",
                "name": "복수브랜드",
                "ml_id": "ml_006",
                "strategy_id": "strategy_006",
                "cd_id": "cd_006",
            },
            {
                "brand_id": "sb_007_1",
                "name": "복수브랜드",
                "ml_id": "ml_007",
                "strategy_id": "strategy_007",
                "cd_id": "cd_007",
            },
        ]

    def cursor(self):
        return CatalogCursor(self)


def test_catalog_lookup_requires_market_for_multi_membership_brand() -> None:
    with pytest.raises(AmbiguousBrandMarketError, match="복수브랜드"):
        load_brand_from_catalog("복수브랜드", CatalogConnection())


def test_catalog_lookup_resolves_exact_requested_market() -> None:
    connection = CatalogConnection()

    selected_rows = [
        load_brand_from_catalog(
            "복수브랜드",
            connection,
            requested_ml_id="ml_007",
            requested_strategy_id="strategy_007",
        )
        for _ in range(3)
    ]

    assert {selected["brand_id"] for selected in selected_rows} == {"sb_007_1"}
    assert {selected["ml_id"] for selected in selected_rows} == {"ml_007"}
    assert {selected["strategy_id"] for selected in selected_rows} == {"strategy_007"}


def test_catalog_lookup_rejects_unknown_requested_market() -> None:
    with pytest.raises(RequestedBrandMarketNotFoundError, match="ml_999"):
        load_brand_from_catalog(
            "복수브랜드",
            CatalogConnection(),
            requested_ml_id="ml_999",
            requested_strategy_id="strategy_999",
        )


def test_catalog_lookup_rejects_excluded_requested_membership() -> None:
    connection = CatalogConnection()
    connection.rows[1]["is_excluded"] = 1

    with pytest.raises(RequestedBrandMarketNotFoundError, match="ml_007"):
        load_brand_from_catalog(
            "복수브랜드",
            connection,
            requested_ml_id="ml_007",
            requested_strategy_id="strategy_007",
        )


def test_catalog_lookup_keeps_single_membership_backward_compatible() -> None:
    connection = CatalogConnection()
    connection.rows = [connection.rows[0]]

    selected = load_brand_from_catalog("복수브랜드", connection)

    assert selected["brand_id"] == "sb_006_1"


def test_brand_context_records_requested_and_resolved_market(monkeypatch) -> None:
    connection = CatalogConnection()
    monkeypatch.setattr(
        "bundle_builder.brand_context_builder.load_market_from_catalog",
        lambda ml_id, db_conn: {"ml_id": ml_id, "ml_name": "요청시장"},
    )
    monkeypatch.setattr(
        "bundle_builder.brand_context_builder.detect_available_sources",
        lambda brand, db_conn: ["IQVIA"],
    )

    context = build_brand_context(
        "복수브랜드",
        db_conn=connection,
        requested_ml_id="ml_007",
        requested_strategy_id="strategy_007",
    )

    assert context["requested_ml_id"] == "ml_007"
    assert context["requested_strategy_id"] == "strategy_007"
    assert context["ml_id"] == "ml_007"
    assert context["strategy_id"] == "strategy_007"
    assert context["cd_id"] == "cd_007"


def test_legacy_bundle_refuses_to_ignore_requested_market() -> None:
    class LegacyConfig:
        config_version = "phase_zeta_v1"

    with pytest.raises(ValueError, match="phase_zeta_v1_1"):
        build_brand_bundle(
            "복수브랜드",
            None,
            LegacyConfig(),
            object(),
            requested_ml_id="ml_007",
            requested_strategy_id="strategy_007",
        )


def test_v11_bundle_meta_records_requested_and_resolved_market(monkeypatch) -> None:
    monkeypatch.setattr(
        "bundle_builder.orchestrator.build_brand_context",
        lambda *args, **kwargs: {
            "name": "복수브랜드",
            "ml_id": "ml_007",
            "strategy_id": "strategy_007",
            "cd_id": "cd_007",
        },
    )
    monkeypatch.setattr("bundle_builder.orchestrator.build_market_views", lambda *args: [])
    monkeypatch.setattr(
        "bundle_builder.orchestrator.build_event_bundle",
        lambda *args: {
            "events_brand_centric": [],
            "events_market_trend": [],
            "cross_match_events": [],
        },
    )
    monkeypatch.setattr(
        "bundle_builder.orchestrator.build_competitor_events",
        lambda *args: {"by_source": {}},
    )
    monkeypatch.setattr(
        "bundle_builder.orchestrator._build_forecast_simulation",
        lambda *args: {},
    )
    monkeypatch.setattr("bundle_builder.orchestrator._mart_computed_at", lambda *args: None)

    class V11Config:
        config_version = "phase_zeta_v1_1"
        builder_version = "test"

    bundle = build_brand_bundle(
        "복수브랜드",
        datetime(2026, 7, 29),
        V11Config(),
        object(),
        requested_ml_id="ml_007",
        requested_strategy_id="strategy_007",
    )

    assert bundle["bundle_meta"]["requested_market_identity"] == {
        "ml_id": "ml_007",
        "strategy_id": "strategy_007",
    }
    assert bundle["bundle_meta"]["resolved_market_identity"] == {
        "ml_id": "ml_007",
        "strategy_id": "strategy_007",
        "cd_id": "cd_007",
    }


def test_routed_run_uses_unique_market_identity_and_passes_requested_market(tmp_path) -> None:
    calls = []

    def build_bundle(
        brand: str,
        brand_key: str | None = None,
        requested_ml_id: str | None = None,
        requested_strategy_id: str | None = None,
    ):
        calls.append((brand, brand_key, requested_ml_id, requested_strategy_id))
        return {
            "bundle_meta": {
                "bundle_hash": f"sha256:{requested_ml_id}",
                "processing_mode": "full",
            },
            "brand_context": {"brand_name": brand},
            "market_views": [],
            "event_bundle": {
                "events_brand_centric": [],
                "events_market_trend": [],
                "cross_match_events": [],
            },
        }

    def call_llm(bundle):
        return LLMCallResult(
            success=False,
            parsed_output={},
            raw_response=json.dumps({}),
            tokens_in=0,
            tokens_out=0,
            duration_sec=0.0,
            model_version="test",
            retry_count=0,
            error="expected",
        )

    orchestrator = Agent2RegenOrchestrator(
        workflow_revision_id=3727,
        formatter_version="test",
        run_store=JsonRunStore(tmp_path / "runs.json"),
        ports=DependencyPorts(
            build_bundle,
            call_llm,
            lambda parsed, bundle: ValidationOutcome(True, {}, {}),
            lambda *args: {},
        ),
        fail_threshold=10,
        dry_run=True,
    )
    route = RouteDecision(
        "multi-key",
        1,
        "sparse",
        ProcessingMode.LLM_FULL,
        ("tier2_llm_v1",),
    )
    worklist = [
        RoutedAgent2Brand("multi-key", "복수브랜드", route, "ml_006", "strategy_006"),
        RoutedAgent2Brand("multi-key", "복수브랜드", route, "ml_007", "strategy_007"),
    ]

    manifest = orchestrator.run_routed(worklist)

    assert calls == [
        ("복수브랜드", "multi-key", "ml_006", "strategy_006"),
        ("복수브랜드", "multi-key", "ml_007", "strategy_007"),
    ]
    assert set(manifest["brands"]) == {
        "multi-key::strategy_006",
        "multi-key::strategy_007",
    }
    assert manifest["routing_identity"] == "brand_key+requested_market"


def test_routed_run_preserves_legacy_two_positional_argument_port(tmp_path) -> None:
    calls = []

    def build_bundle(brand, identity):
        calls.append((brand, identity))
        return {
            "bundle_meta": {"bundle_hash": "sha256:legacy-port"},
            "brand_context": {"brand_name": brand},
            "market_views": [],
            "event_bundle": {
                "events_brand_centric": [],
                "events_market_trend": [],
                "cross_match_events": [],
            },
        }

    orchestrator = Agent2RegenOrchestrator(
        workflow_revision_id=3727,
        formatter_version="test",
        run_store=JsonRunStore(tmp_path / "runs.json"),
        ports=DependencyPorts(
            build_bundle,
            lambda bundle: LLMCallResult(False, {}, "{}", 0, 0, 0.0, "test", 0, "expected"),
            lambda parsed, bundle: ValidationOutcome(True, {}, {}),
            lambda *args: {},
        ),
        fail_threshold=10,
        dry_run=True,
    )
    route = RouteDecision(
        "single-key",
        1,
        "sparse",
        ProcessingMode.LLM_FULL,
        ("tier2_llm_v1",),
    )

    orchestrator.run_routed([RoutedAgent2Brand("single-key", "단일브랜드", route)])

    assert calls == [("단일브랜드", "single-key")]


def test_market_scoped_artifacts_do_not_overwrite_between_memberships() -> None:
    assert _market_scoped_artifact_stem(
        "복수브랜드",
        requested_ml_id="ml_006",
        requested_strategy_id="strategy_006",
    ) == "복수브랜드__strategy_006"
    assert _market_scoped_artifact_stem(
        "복수브랜드",
        requested_ml_id="ml_007",
        requested_strategy_id="strategy_007",
    ) == "복수브랜드__strategy_007"
