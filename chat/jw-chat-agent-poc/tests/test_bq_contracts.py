from __future__ import annotations

from jw_chat_agent_poc.agent_loop.bq_contracts import (
    BQ_CONTRACTS,
    BQ_CONTRACT_IDS,
    contract_for,
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
