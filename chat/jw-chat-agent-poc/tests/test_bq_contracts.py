from __future__ import annotations

from jw_chat_agent_poc.agent_loop.bq_contracts import (
    BQ_CONTRACTS,
    BQ_CONTRACT_IDS,
    SlotTier,
    contract_for,
    evaluate_slot_coverage,
)
from jw_chat_agent_poc.service import answer_pipeline


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
        "comparison_periods",
        "top_brand_share_delta_pctp",
        "rank_change",
        "own_share_rank_change",
        "share_of_growth",
        "growth_decomposition",
    }
    assert by_tier[SlotTier.BUSINESS_REQUIRED] == {
        "concentration_change",
        "competition_verdict",
    }
    assert by_tier[SlotTier.OPTIONAL] == {
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

    assert statuses["comparison_periods"] == "supported"
    assert statuses["share_of_growth"] == "supported"
    assert statuses["growth_decomposition"] == "supported"
    assert statuses["rank_change"] == "missing"
    assert statuses["related_events"] == "not_applicable"


def test_b1_supported_analysis_slots_are_projected_into_the_final_answer() -> None:
    tool_calls = (
        {
            "tool": "bq_analysis",
            "render_data": {
                "contract_id": "B1",
                "source_results": [
                    {
                        "source": "UBIST",
                        "period": "2025-06~2026-06",
                        "share_of_growth_pct": 3.34,
                        "market_growth_pct": 60.40,
                        "excess_growth_pctp": 0.69,
                        "share_delta_pctp": 0.12,
                        "gain_loss": [
                            {"brand": "리바로젯", "share_delta_pctp": 0.69},
                            {"brand": "리피토", "share_delta_pctp": -0.44},
                        ],
                    }
                ],
            },
        },
    )

    context = answer_pipeline.AnswerPipelineContext(
        question="리바로 시장 경쟁 구도가 최근 어떻게 변하고 있어?",
        result={"tool_calls": list(tool_calls)},
        markdown_response={"fact_md": "### 필수 답변 fact\n- 검증된 BQ 분석 fact"},
        fact_md="### 필수 답변 fact\n- 검증된 BQ 분석 fact",
        policy_fact_md="### 필수 답변 fact\n- 검증된 BQ 분석 fact",
        file_context_fact="",
        deep_mode=False,
        market_contract_allowed=True,
        general_contracts_allowed=False,
        external_tool_agent_result=True,
        empty_file_answer=lambda _answer: False,
        file_context_fallback=lambda answer: answer,
        append_file_context_source=lambda answer, _fact, _file: answer,
        record_source_notice=lambda _attached: None,
        relational_claim_gate=lambda answer: answer,
        natural_fact_lead=lambda answer: answer,
        file_postprocess_isolation=lambda answer: answer,
        evidence_binding_gate=lambda answer: answer,
        strip_verified_progress=lambda answer: answer,
    )
    stages, _post = answer_pipeline.build_answer_pipeline_stages(context)
    bq_stage = next(stage for stage in stages if stage.name == "answer_contract_first")

    repaired = bq_stage.transform("상위 브랜드 순위는 표와 같습니다.")

    assert "share-of-growth 3.34%" in repaired
    assert "성장 분해" in repaired
    assert "시장 성장률 60.40%" in repaired
    assert "시장 대비 초과 성장 +0.69%p" in repaired
    assert "점유율 변화 +0.12%p" in repaired
    assert "gain-loss" in repaired
    assert "리바로젯 +0.69%p" in repaired
    assert "리피토 -0.44%p" in repaired
