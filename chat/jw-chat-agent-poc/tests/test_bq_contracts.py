from __future__ import annotations

from jw_chat_agent_poc.agent_loop.bq_contracts import (
    BQ_CONTRACTS,
    BQ_CONTRACT_IDS,
    SlotTier,
    contract_for,
    evaluate_slot_coverage,
)


EXPECTED_IDS = (
    "A1",
    "A2",
    "A3",
    "B1",
    "B2",
    "B3",
    "C1",
    "C2",
    "C3",
    "D1",
    "D2",
    "D3",
    "E1",
    "E2",
)


def test_bq_catalog_contains_every_defined_question_contract() -> None:
    assert BQ_CONTRACT_IDS == EXPECTED_IDS
    assert tuple(contract.contract_id for contract in BQ_CONTRACTS) == EXPECTED_IDS


def test_every_bq_contract_declares_execution_and_safety_policy() -> None:
    for contract in BQ_CONTRACTS:
        assert contract.required_slots
        assert contract.tools
        assert contract.sources
        assert contract.calculations
        assert contract.safety_rules
        assert contract.chart_kinds
        assert contract.analysis_slots
        assert {slot.tier for slot in contract.analysis_slots} == {
            SlotTier.REQUIRED,
            SlotTier.BUSINESS_REQUIRED,
            SlotTier.OPTIONAL,
        }


def test_source_separation_rules_are_explicit_for_cross_source_contracts() -> None:
    c3 = contract_for("C3")
    assert c3.sources == ("UBIST", "IQVIA_NSA")
    assert "never_aggregate_sources" in c3.safety_rules

    e2 = contract_for("E2")
    assert {"UBIST", "IQVIA_NSA", "HIRA", "CSD", "NEWS"}.issubset(e2.sources)
    assert "evidence_required_for_every_claim" in e2.safety_rules


def test_contract_lookup_is_exact_and_does_not_guess() -> None:
    assert contract_for("A1") is BQ_CONTRACTS[0]
    assert contract_for("a1") is None
    assert contract_for("unknown") is None


def test_b1_contract_declares_the_approved_analysis_requirements() -> None:
    contract = contract_for("B1")
    by_tier = {
        tier: {slot.slot_id for slot in contract.analysis_slots if slot.tier is tier}
        for tier in SlotTier
    }

    assert by_tier[SlotTier.REQUIRED] == {
        "comparison_period",
        "current_top_structure",
        "share_gainers",
        "share_losers",
        "competition_change_conclusion",
    }
    assert by_tier[SlotTier.BUSINESS_REQUIRED] == {
        "own_share_rank_change",
        "rank_changes",
    }
    assert by_tier[SlotTier.OPTIONAL] == {
        "share_of_growth",
        "growth_decomposition",
        "concentration_change",
        "related_events",
        "channel_competition_change",
    }
    assert contract.forbidden_outputs == (
        "single_period_snapshot_only",
        "irrelevant_source_failure",
    )


def test_slot_coverage_distinguishes_supported_missing_and_unavailable() -> None:
    coverage = evaluate_slot_coverage(
        "B1",
        {
            "render_data": {
                "period": "2026-04~2026-05",
                "share_of_growth_pct": 3.34,
                "share_delta_pctp": 0.1,
                "market_growth_pct": 1.2,
                "excess_growth_pctp": -0.2,
            }
        },
        missing_sources=("iqvia_nsa",),
    )
    statuses = {item.slot_id: item.status.value for item in coverage}

    assert statuses["comparison_period"] == "supported"
    assert statuses["share_of_growth"] == "supported"
    assert statuses["growth_decomposition"] == "supported"
    assert statuses["rank_changes"] == "missing"
    assert statuses["related_events"] == "not_applicable"
