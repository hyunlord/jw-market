from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline.scripts.agent3.market_loader import (
    ExistingMarketState,
    MarketStrengthRecord,
    canonical_market_content_matches,
    compute_market_input_hash,
)
from pipeline.scripts.agent3.market_processing import (
    build_native_market_position,
    build_strategic_inputs,
)
from pipeline.scripts.agent3.market_repository import MarketUnit, StrategicMetricRow, _table_spec
from pipeline.scripts.agent3.run_market_source import load_worklist
from pipeline.scripts.agent3.run_source import ExecutionContractError


def _row(
    *,
    market_id: str,
    brand_key: str,
    brand_name: str,
    source: str = "ubist",
    history: dict[str, float],
) -> StrategicMetricRow:
    return StrategicMetricRow(
        view_kind="market_landscape",
        market_id=market_id,
        brand_key=brand_key,
        brand_name=brand_name,
        source=source,
        measure="sales",
        unit_label="원",
        raw_value_history=history,
        channel_data={},
        specialty_data={},
        dimension_data={},
        dimension_channel_data={},
        dimension_specialty_data={},
    )


def test_native_market_position_uses_only_requested_market_members() -> None:
    unit = MarketUnit(
        view_kind="market_landscape",
        market_id="ml_011",
        brand_key="target",
        brand_name="악템라",
        source="ubist",
        mart_source="ubist",
    )
    scope = [
        _row(market_id="ml_011", brand_key="target", brand_name="악템라", history={"2026-01": 300.0}),
        _row(market_id="ml_011", brand_key="a", brand_name="경쟁A", history={"2026-01": 500.0}),
        _row(market_id="ml_011", brand_key="b", brand_name="경쟁B", history={"2026-01": 200.0}),
    ]

    result = build_native_market_position(unit, scope, base_summary={})

    assert result.candidate["rank"] == 2
    assert result.candidate["share_pct"] == 30.0
    assert result.candidate["market_brand_count"] == 3
    assert result.candidate["market_key"] == "market_landscape:ml_011:ubist"
    assert result.candidate["slice"] == "UBIST 전략 ML ml_011"
    assert "ATC4" not in result.narrative


def test_cd_scope_projects_missing_specialty_dimension_as_null() -> None:
    assert _table_spec("competitive_dynamics") == (
        "mart_strategic_cd_brand_metric",
        "cd_market_id",
        "NULL AS dimension_specialty_data",
    )


def test_strategic_inputs_carry_native_scope_and_candidate_evidence() -> None:
    unit = MarketUnit(
        view_kind="competitive_dynamics",
        market_id="cd_001",
        brand_key="target",
        brand_name="대상약",
        source="iqvia",
        mart_source="iqvia_nsa",
    )
    scope = [
        StrategicMetricRow(
            view_kind="competitive_dynamics",
            market_id="cd_001",
            brand_key="target",
            brand_name="대상약",
            source="iqvia_nsa",
            measure="sales",
            unit_label="원",
            raw_value_history={"2025-Q4": 500_000_000.0, "2026-Q1": 650_000_000.0},
            channel_data={},
            specialty_data={},
            dimension_data={},
            dimension_channel_data={},
            dimension_specialty_data={},
        ),
        StrategicMetricRow(
            view_kind="competitive_dynamics",
            market_id="cd_001",
            brand_key="other",
            brand_name="경쟁약",
            source="iqvia_nsa",
            measure="sales",
            unit_label="원",
            raw_value_history={"2025-Q4": 700_000_000.0, "2026-Q1": 700_000_000.0},
            channel_data={},
            specialty_data={},
            dimension_data={},
            dimension_channel_data={},
            dimension_specialty_data={},
        ),
    ]

    profile, candidates = build_strategic_inputs(unit, scope, top_n=5)

    assert profile["view_kind"] == "competitive_dynamics"
    assert profile["market_id"] == "cd_001"
    assert profile["market_scope"]["member_count"] == 2
    assert profile["market_scope"]["latest_period"] == "2026-Q1"
    assert candidates
    assert all(item["view_kind"] == "competitive_dynamics" for item in candidates)
    assert all(item["market_id"] == "cd_001" for item in candidates)
    assert all("strategic_scope.competitive_dynamics.cd_001" in item["evidence"] for item in candidates)


def test_strategic_inputs_do_not_treat_dimension_labels_as_periods() -> None:
    unit = MarketUnit(
        view_kind="market_landscape",
        market_id="ml_001",
        brand_key="target",
        brand_name="가나플럭스",
        source="ubist",
        mart_source="ubist",
    )
    scope = [
        StrategicMetricRow(
            view_kind="market_landscape",
            market_id="ml_001",
            brand_key="target",
            brand_name="가나플럭스",
            source="ubist",
            measure="sales",
            unit_label="원",
            raw_value_history={"2026-04": 500_000_000.0, "2026-05": 600_000_000.0},
            channel_data={},
            specialty_data={},
            dimension_data={},
            dimension_channel_data={},
            dimension_specialty_data={
                "dosage_form": {
                    "정제, 저작정(TB)": {
                        "의원 IGF": 10_000_000.0,
                        "종합병원 내분비": 20_000_000.0,
                    }
                }
            },
        ),
        _row(
            market_id="ml_001",
            brand_key="other",
            brand_name="경쟁약",
            history={"2026-04": 700_000_000.0, "2026-05": 700_000_000.0},
        ),
    ]

    profile, candidates = build_strategic_inputs(unit, scope, top_n=5)

    assert profile["brand"] == "가나플럭스"
    assert all("channel_specialty_matrix" not in item["evidence"] for item in candidates)


def test_market_hash_changes_across_native_scopes() -> None:
    profile = {"brand": "A"}
    candidates = [{"metric": "recent_growth"}]

    ml_hash = compute_market_input_hash(
        view_kind="market_landscape",
        market_id="ml_001",
        brand_key="a",
        source="ubist",
        profile=profile,
        candidates=candidates,
        workflow_rev=5692,
    )
    cd_hash = compute_market_input_hash(
        view_kind="competitive_dynamics",
        market_id="cd_001",
        brand_key="a",
        source="ubist",
        profile=profile,
        candidates=candidates,
        workflow_rev=5692,
    )

    assert ml_hash != cd_hash


def test_market_content_match_rejects_view_kind_collision() -> None:
    old = ExistingMarketState(
        view_kind="market_landscape",
        input_hash="old",
        workflow_rev=5692,
        profile_json={"brand": "A"},
        strength_candidates_json=[],
        strength_summary_json={"strength_items": []},
    )
    new = MarketStrengthRecord(
        brand_key="a",
        source="ubist",
        market_id="ml_001",
        view_kind="competitive_dynamics",
        brand_name="A",
        serving_brand_name=None,
        profile_json={"brand": "A"},
        strength_candidates_json=[],
        strength_summary_json={"strength_items": []},
        workflow_id=316,
        workflow_rev=5692,
        input_hash="new",
        generation_status="market_position",
        generated_at=datetime.now(timezone.utc),
    )

    with pytest.raises(RuntimeError, match="view_kind collision"):
        canonical_market_content_matches(old, new)


def test_worklist_parser_requires_exact_source_mapping(tmp_path: Path) -> None:
    path = tmp_path / "worklist.tsv"
    path.write_text(
        "view_kind\tmarket_id\tbrand_key\tbrand_name\tsource\tmart_source\n"
        "market_landscape\tml_011\tactemra\t악템라\tubist\tubist\n"
        "competitive_dynamics\tcd_001\ttarget\t대상약\tiqvia\tiqvia_nsa\n",
        encoding="utf-8",
    )

    units = load_worklist(path)

    assert [unit.market_id for unit in units] == ["ml_011", "cd_001"]
    assert [unit.source for unit in units] == ["ubist", "iqvia"]


def test_market_runner_revision_assert_aborts_before_repository(monkeypatch, tmp_path: Path) -> None:
    from pipeline.scripts.agent3 import run_market_source

    def unexpected_repository(*_args, **_kwargs):
        raise AssertionError("repository must not be constructed")

    monkeypatch.setattr(run_market_source, "StrategicMarketRepository", unexpected_repository)

    with pytest.raises(ExecutionContractError, match="workflow revision mismatch"):
        run_market_source.run_market_source(
            worklist=tmp_path / "missing.tsv",
            mode="dry-run",
            output=tmp_path / "result.json",
            workflow_rev=5365,
            expected_workflow_rev=5692,
            environment_mode=None,
            top_n=5,
        )


def test_strategic_manifest_pins_revision_and_uses_cli_mode_only() -> None:
    text = Path("deploy/k8s/agent3/agent3-market-full-job.yaml").read_text(encoding="utf-8")

    assert "pipeline.scripts.agent3.run_market_source" in text
    assert 'name: AGENT3_WORKFLOW_REV' in text
    assert 'value: "5692"' in text
    assert "--expected-workflow-rev 5692" in text
    assert "--mode full" in text
    assert "AGENT3_MODE" not in text
    assert "agent3_brand_strength_source" not in text
