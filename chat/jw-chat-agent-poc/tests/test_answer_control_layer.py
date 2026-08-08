from __future__ import annotations

import pytest

from jw_chat_agent_poc.agent_loop.question_contracts import (
    AnswerIntent,
    OperationMode,
    question_spec_for,
)
from jw_chat_agent_poc.agent_loop.semantic_parser import parse_semantic_question
from jw_chat_agent_poc.orchestrator.answer_projection import (
    AnswerClaim,
    AnswerFailure,
    AnswerGateError,
    ClaimPlan,
    apply_answer_control_layer,
    finalize_answer,
)


def _claim(slot_id: str, claim_type: str = "observation") -> AnswerClaim:
    return AnswerClaim(
        claim_id=f"claim:{slot_id}",
        slot_id=slot_id,
        claim_type=claim_type,
        subject_type="brand",
        subject_id="리바로",
        market_scope="STRATEGIC",
        source="UBIST",
        period_start="2025-06",
        period_end="2026-06",
        canonical_value="3.72",
        canonical_unit="pct",
        display_value="3.72",
        display_unit="%",
        display_text=f"{slot_id}: 3.72%",
        evidence_ids=("ev:1",),
    )


def test_contract_registry_has_twelve_scoped_intents() -> None:
    assert len(AnswerIntent) == 12
    assert question_spec_for("리바로 시장 앞으로 어떻게 될 것 같아?").intent is AnswerIntent.MARKET_OUTLOOK
    assert question_spec_for("IQVIA랑 UBIST 수치가 다른데 왜?").intent is AnswerIntent.SOURCE_DIFFERENCE
    assert question_spec_for("경쟁사 영업활동 변화 있어?").intent is AnswerIntent.SALES_ACTIVITY_TREND


@pytest.mark.parametrize(
    ("question", "forbidden"),
    (
        ("리바로 시장 앞으로 어떻게 될 것 같아?", "source_definition"),
        ("IQVIA랑 UBIST 수치가 다른데 왜?", "brand_share_trend"),
        ("경쟁사 영업활동 변화 있어?", "own_prescription_sales_trend"),
    ),
)
def test_projection_rejects_unentitled_claim(question: str, forbidden: str) -> None:
    spec = question_spec_for(question)
    claim = _claim("irrelevant", forbidden)
    with pytest.raises(AnswerGateError, match="unentitled_claim"):
        finalize_answer(spec, ClaimPlan((claim.slot_id,)), (claim,), ())


def test_missing_required_slots_create_code_owned_limitations() -> None:
    spec = question_spec_for("IQVIA랑 UBIST 수치가 다른데 왜?")
    result = finalize_answer(spec, ClaimPlan(()), (), ())
    assert result.degraded is True
    assert result.answer == ""
    assert result.limitations
    assert all(item.code == "required_slot_unfilled" for item in result.limitations)


def test_no_missing_slots_means_no_limitations_and_display_text_only() -> None:
    spec = question_spec_for("IQVIA랑 UBIST 수치가 다른데 왜?")
    claims = tuple(_claim(slot) for slot in spec.required_slots)
    result = finalize_answer(spec, ClaimPlan(tuple(item.slot_id for item in claims)), claims, ())
    assert result.limitations == ()
    assert result.degraded is False
    assert result.answer == "\n\n".join(item.display_text for item in claims)
    assert "canonical" not in result.answer


def test_provenance_fallback_and_degradation_gates() -> None:
    spec = question_spec_for("리바로 시장 앞으로 어떻게 될 것 같아?")
    claims = tuple(_claim(slot) for slot in spec.required_slots)
    plan = ClaimPlan(tuple(item.slot_id for item in claims))
    with pytest.raises(AnswerGateError, match="anchor_provenance_missing"):
        finalize_answer(spec.with_anchor_provenance(None), plan, claims, ())
    with pytest.raises(AnswerGateError, match="fallback_reason_inconsistent"):
        finalize_answer(
            spec,
            plan,
            claims,
            (AnswerFailure("source_unavailable", "UBIST", evidence_ids=("ev:1",)),),
        )
    with pytest.raises(AnswerGateError, match="silent_degradation"):
        finalize_answer(spec, plan, claims, (), selected_branch="legacy", degradation_notice=None)


@pytest.mark.parametrize(
    ("question", "intent"),
    (
        ("IQVIA랑 UBIST 수치가 다른데 왜?", AnswerIntent.SOURCE_DIFFERENCE),
        ("환자수랑 매출 한번에", AnswerIntent.MULTI_SOURCE_SNAPSHOT),
        ("경쟁 구도가 최근 어떻게 변하고 있어?", AnswerIntent.COMPETITION_CHANGE),
        ("신규 진입자나 위협 브랜드 있어?", AnswerIntent.NEW_ENTRANT_THREAT),
    ),
)
def test_semantic_parser_fast_paths(question: str, intent: AnswerIntent) -> None:
    parsed = parse_semantic_question(question)
    assert parsed.intent is intent
    assert parsed.parser == "deterministic_fast_path"


@pytest.mark.parametrize(
    ("question", "operation"),
    (
        ("환자수랑 매출 한번에 나란히 보여줘", OperationMode.SIDE_BY_SIDE),
        ("환자수와 매출을 합산해 줘", OperationMode.FORBIDDEN_SUM),
        ("환자당 매출 알려줘", OperationMode.PER_PATIENT),
        ("환자수가 매출에 영향 줬어?", OperationMode.CAUSAL),
    ),
)
def test_a3_operation_mode_is_explicit(question: str, operation: OperationMode) -> None:
    assert question_spec_for(question).operation_mode is operation


def test_b1_required_slots_do_not_prioritize_share_of_growth() -> None:
    spec = question_spec_for("리바로 시장 경쟁 구도가 최근 어떻게 변하고 있어?")
    assert spec.required_slots == (
        "comparison_period",
        "current_top_structure",
        "share_gainers",
        "share_losers",
        "competition_change_conclusion",
    )
    assert "share_of_growth" in spec.optional_slots


def test_a2_structured_projection_replaces_unrelated_legacy_block() -> None:
    result = {
        "tool_calls": [
            {
                "tool": "bq_analysis",
                "source": "BQ deterministic evidence",
                "render_data": {
                    "contract_id": "A2",
                    "source_results": [
                        {
                            "source": "UBIST",
                            "period": "2025-06~2026-06",
                            "trend_rate_pct": -4.19,
                            "forecast_krw": 100_000_000,
                            "forecast_label": "예측=추세연장",
                            "forecast_uncertainty_note": "외부 요인을 반영하지 않아 실제 값은 달라질 수 있습니다.",
                        }
                    ],
                    "evidence_refs": ["UBIST.render_data.brand_value_series_10pt"],
                },
            }
        ]
    }
    controlled = apply_answer_control_layer(
        "리바로 시장 앞으로 어떻게 될 것 같아?",
        result,
        "리바로는 매출과 점유율이 함께 낮아져 점유율 3.88% → 3.72%입니다.",
    )
    assert controlled.applied is True
    assert "3.88%" not in controlled.answer
    assert "예측=추세연장" in controlled.answer
    assert "실제 값은 달라질 수" in controlled.answer


def test_c3_projection_uses_source_contract_not_brand_growth() -> None:
    result = {
        "tool_calls": [
            {
                "tool": "bq_analysis",
                "render_data": {
                    "contract_id": "C3",
                    "period": "2026-05",
                    "ubist_sales_krw": 8_100_000_000,
                    "iqvia_sales_krw": 8_500_000_000,
                    "evidence_refs": ["UBIST.value", "IQVIA_NSA.value"],
                },
            }
        ]
    }
    controlled = apply_answer_control_layer(
        "IQVIA랑 UBIST 수치가 다른데 왜?",
        result,
        "리바로 성장률 -4.19%p, 점유율 3.72%",
    )
    assert controlled.applied is True
    assert "-4.19" not in controlled.answer
    assert "유통 단계" in controlled.answer
    assert "직접 합산" in controlled.answer


def test_d3_unsupported_axis_is_honest_and_does_not_substitute_own_sales() -> None:
    result = {
        "tool_calls": [
            {
                "tool": "bq_analysis",
                "render_data": {
                    "contract_id": "D3",
                    "status": "unsupported_axis",
                    "insights": ["현재 CSD 도구는 판매사별 활동 변화를 지원하지 않습니다."],
                    "evidence_refs": ["CSD.render_data.series"],
                },
            }
        ]
    }
    controlled = apply_answer_control_layer(
        "경쟁사 영업활동 변화 있어?",
        result,
        "리바로는 매출과 점유율이 함께 낮아졌습니다.",
    )
    assert controlled.applied is True
    assert "리바로는 매출" not in controlled.answer
    assert "판매사별 활동 변화" in controlled.answer
    assert controlled.degraded is True


def test_b3_missing_launch_identity_is_honest_unsupported() -> None:
    result = {
        "tool_calls": [
            {
                "tool": "bq_analysis",
                "render_data": {
                    "contract_id": "B3",
                    "launch_acceleration_status": "unsupported_missing_launch_date",
                    "growth_ranking": [{"brand": "리바로", "share_delta_pctp": -0.2}],
                    "evidence_refs": ["UBIST.level_top5_trend_series"],
                },
            }
        ]
    }
    controlled = apply_answer_control_layer(
        "리바로 신규 진입자/위협 브랜드 있어?",
        result,
        "리바로 점유율이 하락해 위협입니다.",
    )
    assert controlled.applied is True
    assert "리바로 점유율이 하락" not in controlled.answer
    assert "신규 관찰 시점을 판별하는 기능은 제공하지 않습니다" in controlled.answer
    assert controlled.degraded is True


def test_b1_projection_contains_required_competition_slots() -> None:
    result = {
        "tool_calls": [
            {
                "tool": "bq_analysis",
                "render_data": {
                    "contract_id": "B1",
                    "source": "UBIST",
                    "period": "2025-06~2026-06",
                    "current_top_structure": [
                        {"brand": "로수젯", "rank": 1, "value": 20_000_000_000},
                        {"brand": "리바로", "rank": 2, "value": 12_000_000_000},
                    ],
                    "share_gainers": [{"brand": "리바로", "share_delta_pctp": 0.91}],
                    "share_losers": [{"brand": "로수젯", "share_delta_pctp": -1.0}],
                    "competition_change_conclusion": "점유율 상승 1개, 하락 1개 브랜드로 구도가 재편됐습니다.",
                    "evidence_refs": ["UBIST.level_top5_trend_series"],
                },
            }
        ]
    }

    controlled = apply_answer_control_layer(
        "리바로 시장 경쟁 구도가 최근 어떻게 변하고 있어?",
        result,
        "현재 순위표만 보여드립니다.",
    )

    assert controlled.applied is True
    assert controlled.required_slot_coverage == "5/5"
    assert "2025-06~2026-06" in controlled.answer
    assert "상승: 리바로 +0.91%p" in controlled.answer
    assert "하락: 로수젯 -1.00%p" in controlled.answer
    assert "현재 순위표만" not in controlled.answer


def test_a3_projection_keeps_sources_side_by_side_without_ratio() -> None:
    result = {
        "tool_calls": [
            {
                "tool": "bq_analysis",
                "render_data": {
                    "contract_id": "A3",
                    "patient_count": 1000,
                    "patient_period": "2026",
                    "source_results": [
                        {"source": "UBIST", "period": "2026-05", "sales_krw": 8_000_000_000},
                        {"source": "IQVIA NSA", "period": "2026-Q2", "sales_krw": 9_000_000_000},
                    ],
                    "same_population_basis": False,
                    "evidence_refs": [
                        "HIRA.render_data.items.ptntCnt",
                        "UBIST.render_data.brand_value_series_10pt",
                        "IQVIA NSA.render_data.brand_value_series_10pt",
                    ],
                },
            }
        ]
    }

    controlled = apply_answer_control_layer(
        "리바로 질병 환자수랑 최근 매출 한번에",
        result,
        "환자당 매출은 800만원입니다.",
    )

    assert controlled.applied is True
    assert controlled.required_slot_coverage == "3/3"
    assert "HIRA 2026 환자수 1,000명" in controlled.answer
    assert "UBIST 2026-05 매출" in controlled.answer
    assert "IQVIA NSA 2026-Q2 매출" in controlled.answer
    assert "합산하지" in controlled.answer
    assert "환자당 매출은 800만원" not in controlled.answer


def test_target_intent_without_structured_analysis_is_explicitly_degraded() -> None:
    controlled = apply_answer_control_layer(
        "IQVIA랑 UBIST 수치가 다른데 왜?",
        {"tool_calls": [], "answer_control_required": True},
        "리바로 성장률과 점유율을 설명합니다.",
    )

    assert controlled.applied is True
    assert controlled.degraded is True
    assert controlled.required_slot_coverage == "0/4"
    assert "리바로 성장률" not in controlled.answer
    assert "현재 근거로 확인하지 못했습니다" in controlled.answer


def test_c3_without_structured_analysis_uses_deterministic_source_contract() -> None:
    controlled = apply_answer_control_layer(
        "IQVIA랑 UBIST 수치가 다른데 왜?",
        {"tool_calls": []},
        "리바로 초과성장 -4.19%p, 점유율 3.72%입니다.",
    )

    assert controlled.applied is True
    assert controlled.degraded is False
    assert controlled.required_slot_coverage == "4/4"
    assert "-4.19" not in controlled.answer
    assert "유통 단계" in controlled.answer
    assert "직접 합산" in controlled.answer
    assert len(controlled.question_spec_sha256) == 64
    assert len(controlled.claim_plan_sha256) == 64
    assert len(controlled.evidence_set_sha256) == 64
    assert controlled.selected_branch == "answer_projection"


def test_control_layer_hashes_are_deterministic_for_the_same_projection() -> None:
    first = apply_answer_control_layer(
        "IQVIA랑 UBIST 수치가 다른데 왜?",
        {"tool_calls": []},
        "unused fallback",
    )
    second = apply_answer_control_layer(
        "IQVIA랑 UBIST 수치가 다른데 왜?",
        {"tool_calls": []},
        "different unused fallback",
    )

    assert first.question_spec_sha256 == second.question_spec_sha256
    assert first.claim_plan_sha256 == second.claim_plan_sha256
    assert first.evidence_set_sha256 == second.evidence_set_sha256


def test_competitor_activity_without_structured_analysis_is_honest_unsupported() -> None:
    controlled = apply_answer_control_layer(
        "경쟁사 영업활동 변화 있어?",
        {"tool_calls": []},
        "리바로 처방 매출 추이입니다.",
    )

    assert controlled.applied is True
    assert controlled.degraded is True
    assert "리바로 처방 매출" not in controlled.answer
    assert "경쟁사별 활동 변화" in controlled.answer
    assert "현재 근거로 확인하지 못했습니다" in controlled.answer


def test_new_entrant_without_structured_analysis_is_honest_unsupported() -> None:
    controlled = apply_answer_control_layer(
        "리바로 신규 진입자/위협 브랜드 있어?",
        {"tool_calls": []},
        "리바로 점유율 하락이 위협입니다.",
    )

    assert controlled.applied is True
    assert controlled.degraded is True
    assert "리바로 점유율 하락" not in controlled.answer
    assert "신규 관찰 시점을 판별하는 기능은 제공하지 않습니다" in controlled.answer


@pytest.mark.parametrize(
    "question",
    (
        "리바로 시장 앞으로 어떻게 될 것 같아?",
        "리바로 시장 경쟁 구도가 최근 어떻게 변하고 있어?",
    ),
)
def test_nonfallback_intent_without_structured_analysis_preserves_answer(question: str) -> None:
    controlled = apply_answer_control_layer(
        question,
        {"tool_calls": []},
        "기존 근거 기반 답변",
    )

    assert controlled.applied is False
    assert controlled.answer == "기존 근거 기반 답변"


def test_mixed_scope_without_bq_analysis_preserves_composed_answer() -> None:
    controlled = apply_answer_control_layer(
        "문서와 리바로 시장 전망을 함께 설명해줘",
        {"tool_calls": [], "context_scope": "MIXED"},
        "문서 근거와 시장 근거를 함께 설명합니다.",
    )

    assert controlled.applied is False
    assert controlled.answer == "문서 근거와 시장 근거를 함께 설명합니다."


def test_own_sales_activity_without_d3_analysis_preserves_d1_answer() -> None:
    controlled = apply_answer_control_layer(
        "리바로 영업활동 추이 어때?",
        {"tool_calls": []},
        "리바로 자체 영업활동 추이입니다.",
    )

    assert controlled.applied is False
    assert controlled.answer == "리바로 자체 영업활동 추이입니다."
