from __future__ import annotations

import pytest

from jw_chat_agent_poc.orchestrator import answer_projection as projection

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
        text_template=f"{slot_id}: 3.72%",
        value_refs=(),
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


def test_no_missing_slots_means_no_limitations_and_rendered_claim_text_only() -> None:
    spec = question_spec_for("IQVIA랑 UBIST 수치가 다른데 왜?")
    claims = tuple(_claim(slot) for slot in spec.required_slots)
    result = finalize_answer(spec, ClaimPlan(tuple(item.slot_id for item in claims)), claims, ())
    assert result.limitations == ()
    assert result.degraded is False
    assert result.answer == "\n\n".join(item.text_template for item in claims)
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
    ("question", "intent"),
    (
        ("리바로젯 주요 경쟁사의 성장률에 대해 표로 정리해줘", AnswerIntent.BRAND_TREND),
        ("리바로젯 경쟁사 성장 표", AnswerIntent.BRAND_TREND),
        ("리바로젯 경쟁사 증감률 표", AnswerIntent.BRAND_TREND),
        ("리바로젯 경쟁 브랜드 YoY 표", AnswerIntent.BRAND_TREND),
        ("리바로 경쟁사 순위 변화 표로 보여줘", AnswerIntent.COMPETITION_CHANGE),
        ("리바로 경쟁사 단가 표로 보여줘", AnswerIntent.BRAND_TREND),
    ),
)
def test_requested_internal_metrics_do_not_fall_through_to_external_lookup(
    question: str,
    intent: AnswerIntent,
) -> None:
    assert question_spec_for(question).intent is intent


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
    assert "80.00억원" in controlled.answer
    assert "90.00억원" in controlled.answer
    assert "8000000000" not in controlled.answer.replace(",", "")
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


def test_own_sales_activity_without_d1_analysis_fails_closed() -> None:
    controlled = apply_answer_control_layer(
        "리바로 영업활동 추이 어때?",
        {"tool_calls": []},
        "리바로 자체 영업활동 추이입니다.",
    )

    assert controlled.applied is True
    assert controlled.answer_status == "unsupported"
    assert "리바로 자체 영업활동 추이입니다." not in controlled.answer
    assert "영업활동 추이 데이터" in controlled.answer


@pytest.mark.parametrize(
    ("question", "contract_id", "data", "coverage", "expected_text"),
    (
        (
            "리바로 시장 규모가 지금 얼마고 어떻게 변해왔어?",
            "A1",
            {
                "source_summaries": [
                    {
                        "source": "UBIST",
                        "start_period": "2025-06",
                        "end_period": "2026-06",
                        "start_market_size_krw": 100_000_000_000,
                        "end_market_size_krw": 110_000_000_000,
                        "market_growth_rate_pct": 10.0,
                    }
                ],
                "channel_shares_pct": {"의원": 70.0, "종병": 30.0},
            },
            "2/2",
            "2026-06 시장 규모 1,100.00억원",
        ),
        (
            "리바로 최근 매출/처방 추이 어때?",
            "C1",
            {
                "source_results": [
                    {
                        "source": "UBIST",
                        "period": "2025-06~2026-06",
                        "brand_start_sales_krw": 10_000_000_000,
                        "brand_end_sales_krw": 12_000_000_000,
                        "brand_growth_pct": 20.0,
                        "market_growth_pct": 10.0,
                        "growth_gap_pctp": 10.0,
                    }
                ]
            },
            "2/2",
            "브랜드 매출 100.00억원 → 120.00억원",
        ),
        (
            "리바로 경쟁 상대는 누구고 우리 위치는 어디야?",
            "B2",
            {
                "source_results": [
                    {
                        "source": "UBIST",
                        "cohort_z_score": -0.267261,
                        "population": 3,
                        "competition_basis": "same market source and period",
                    }
                ]
            },
            "3/3",
            "동일 시장·출처·기간의 3개 브랜드",
        ),
        (
            "리바로 어느 채널/진료과에서 잘 팔려?",
            "C2",
            {
                "distributions": {
                    "channel": {"의원": 70.0, "종병": 30.0},
                    "specialty": {"순환기": 60.0, "내분비": 40.0},
                },
                "axes_are_not_aggregated": True,
            },
            "2/2",
            "채널 구성: 의원 70.00%, 종병 30.00%",
        ),
        (
            "리바로 영업활동이 매출에 영향 줬어?",
            "D2",
            {
                "source_results": [
                    {
                        "source": "UBIST",
                        "period": "2026-01~2026-03",
                        "activity_change_rate_pct": 208.33,
                        "performance_change_rate_pct": 5.0,
                    }
                ],
                "temporal_overlap_not_causation": True,
            },
            "4/4",
            "인과를 단정하지 않습니다",
        ),
        (
            "리바로 관련 최근 이슈 뭐 있어?",
            "E1",
            {
                "news_refs": [
                    {
                        "title": "리바로 기사",
                        "date": "2026-05-01",
                        "source": "약업신문",
                        "url": "https://news.example/1",
                    }
                ]
            },
            "3/3",
            "리바로 기사 (2026-05-01, 약업신문)",
        ),
    ),
)
def test_remaining_structured_intents_use_entitlement_projection(
    question: str,
    contract_id: str,
    data: dict[str, object],
    coverage: str,
    expected_text: str,
) -> None:
    controlled = apply_answer_control_layer(
        question,
        {
            "tool_calls": [
                {
                    "tool": "bq_analysis",
                    "render_data": {
                        "contract_id": contract_id,
                        "evidence_refs": [f"{contract_id}.deterministic_evidence"],
                        **data,
                    },
                }
            ]
        },
        "질문과 무관한 기존 답변",
    )

    assert controlled.applied is True
    assert controlled.degraded is (contract_id == "D2")
    assert controlled.required_slot_coverage == coverage
    assert controlled.selected_branch == "answer_projection"
    assert expected_text in controlled.answer
    assert "질문과 무관한 기존 답변" not in controlled.answer


@pytest.mark.parametrize(
    "question",
    (
        "리바로 시장 규모가 지금 얼마고 어떻게 변해왔어?",
        "리바로 최근 매출/처방 추이 어때?",
        "리바로 경쟁 상대는 누구고 우리 위치는 어디야?",
        "리바로 어느 채널/진료과에서 잘 팔려?",
        "리바로 영업활동이 매출에 영향 줬어?",
        "리바로 질병 환자수랑 최근 매출 한번에",
        "리바로 관련 최근 이슈 뭐 있어?",
    ),
)
def test_remaining_intents_fail_closed_when_structured_evidence_is_missing(question: str) -> None:
    contract_id = {
        "MARKET_SIZE_TREND": "A1",
        "BRAND_TREND": "C1",
        "COMPETITOR_POSITION": "B2",
        "CHANNEL_SPECIALTY": "C2",
        "SALES_IMPACT": "D2",
        "MULTI_SOURCE_SNAPSHOT": "A3",
        "EXTERNAL_LOOKUP": "E1",
    }[question_spec_for(question).intent.value]
    controlled = apply_answer_control_layer(
        question,
        {
            "tool_calls": [],
            "agent_loop_metrics": {
                "deterministic_plan_kind": f"BQ:{contract_id}",
                "bq_analysis_validation": "MISSING_EVIDENCE",
            },
        },
        "질문과 무관한 기존 답변",
    )

    assert controlled.applied is True
    assert controlled.degraded is True
    assert controlled.required_slot_coverage.startswith("0/")
    assert controlled.selected_branch == "answer_projection"
    assert "질문과 무관한 기존 답변" not in controlled.answer
    assert "현재 근거로 확인하지 못했습니다" in controlled.answer


def test_multi_source_snapshot_fail_closed_without_runtime_plan_marker() -> None:
    controlled = apply_answer_control_layer(
        "리바로 질병 환자수랑 최근 매출 한번에",
        {"tool_calls": []},
        "질문과 무관한 기존 답변",
    )

    assert controlled.applied is True
    assert controlled.degraded is True
    assert controlled.intent == "MULTI_SOURCE_SNAPSHOT"
    assert controlled.required_slot_coverage == "0/3"
    assert controlled.selected_branch == "answer_projection"
    assert "질문과 무관한 기존 답변" not in controlled.answer


def test_unclassified_general_question_preserves_existing_passthrough() -> None:
    controlled = apply_answer_control_layer(
        "복약 방법을 알려줘",
        {"tool_calls": []},
        "기존 일반 답변",
    )

    assert controlled.applied is False
    assert controlled.selected_branch == "passthrough"
    assert controlled.answer == "기존 일반 답변"


def test_claim_contract_keeps_value_refs_not_preformatted_display_text() -> None:
    assert "display_text" not in AnswerClaim.__dataclass_fields__
    assert "canonical_value" not in AnswerClaim.__dataclass_fields__
    assert "value_refs" in AnswerClaim.__dataclass_fields__


def test_c2_keeps_available_channel_when_specialty_is_missing() -> None:
    controlled = apply_answer_control_layer(
        "리바로 어느 채널/진료과에서 잘 팔려?",
        {
            "tool_calls": [{
                "tool": "bq_analysis",
                "render_data": {
                    "contract_id": "C2",
                    "distributions": {"channel": {"상급종병": 4.45, "종병": 4.15, "병원": 3.56}},
                    "evidence_refs": ["UBIST.channel.level_segments"],
                },
            }]
        },
        "legacy answer",
    )

    assert controlled.answer_status == "partial"
    assert controlled.required_slot_coverage == "1/2"
    assert "채널 구성" in controlled.answer
    assert "진료과별 분포 데이터" in controlled.answer
    assert "specialty_distribution" not in controlled.answer
    assert "| 항목 | 내용 |" in controlled.answer
    assert "## 출처" in controlled.answer


def test_c2_recovers_channel_distribution_from_query_spec_level_segments() -> None:
    controlled = apply_answer_control_layer(
        "리바로 어느 채널/진료과에서 잘 팔려?",
        {
            "tool_calls": [{
                "tool": "query_spec",
                "source": "UBIST",
                "render_data": {
                    "level_segments": [
                        {"name": "상급종병", "value": 17_300_000_000},
                        {"name": "종병", "value": 20_360_000_000},
                        {"name": "병원", "value": 13_200_000_000},
                    ]
                },
            }],
            "answer_control_required": True,
        },
        "legacy answer",
    )

    assert controlled.answer_status == "partial"
    assert "상급종병" in controlled.answer
    assert "channel_distribution" not in controlled.answer


def test_a2_names_brand_monthly_target_horizon_and_formats_krw() -> None:
    controlled = apply_answer_control_layer(
        "리바로 시장 앞으로 어떻게 될 것 같아?",
        {
            "tool_calls": [{
                "tool": "bq_analysis",
                "render_data": {
                    "contract_id": "A2",
                    "source_results": [{
                        "source": "UBIST",
                        "period": "2021-07~2026-06",
                        "trend_rate_pct": 0.3656581095792125,
                        "forecast_krw": 8_618_859_701.348597,
                        "forecast_label": "예측=추세연장",
                    }],
                    "evidence_refs": ["UBIST.render_data.brand_value_series_10pt"],
                },
            }]
        },
        "legacy answer",
    )

    assert controlled.answer_status == "complete"
    assert "리바로 월 매출" in controlled.answer
    assert "월 복합성장률" in controlled.answer
    assert "2026-07" in controlled.answer
    assert "86.19억원" in controlled.answer
    assert "8618859701" not in controlled.answer.replace(",", "")
    assert "CAGR" not in controlled.answer
    assert "| 항목 | 내용 |" in controlled.answer
    assert "## 출처" in controlled.answer


def test_d1_is_controlled_instead_of_using_passthrough_entitlement_hole() -> None:
    controlled = apply_answer_control_layer(
        "리바로 영업활동 추이 어때?",
        {
            "tool_calls": [{
                "tool": "bq_analysis",
                "render_data": {
                    "contract_id": "D1",
                    "period": "2025-06~2026-06",
                    "activity_trend": [
                        {"period": "2025-06", "product_details": 100},
                        {"period": "2026-06", "product_details": 120},
                    ],
                    "activity_delta": 20,
                    "activity_change_rate_pct": 20.0,
                    "region": "TOTAL",
                    "evidence_refs": ["CSD.render_data.series"],
                },
            }]
        },
        "legacy table and source",
    )

    assert controlled.applied is True
    assert controlled.answer_status == "complete"
    assert controlled.selected_branch == "answer_projection"
    assert "legacy table" not in controlled.answer
    assert "CSD" in controlled.answer
    assert "## 출처" in controlled.answer


def test_chart_entitlement_keeps_only_supported_minimal_artifact_set() -> None:
    controlled = apply_answer_control_layer(
        "리바로 시장 앞으로 어떻게 될 것 같아?",
        {
            "tool_calls": [{
                "tool": "bq_analysis",
                "render_data": {
                    "contract_id": "A2",
                    "source_results": [{
                        "source": "UBIST",
                        "period": "2021-07~2026-06",
                        "trend_rate_pct": 0.37,
                        "forecast_krw": 8_618_859_701.35,
                    }],
                    "evidence_refs": ["UBIST.render_data.brand_value_series_10pt"],
                },
            }]
        },
        "legacy answer",
    )
    charts = [
        {"title": "리바로 매출 추이", "evidence_refs": ["UBIST.render_data.brand_value_series_10pt"]},
        {"title": "시장 매출 추이", "evidence_refs": ["UBIST.render_data.market_size_series"]},
        {"title": "Brand별 점유율", "evidence_refs": []},
        {"title": "HHI 추이", "evidence_refs": ["UBIST.render_data.hhi"]},
    ]

    entitled = projection.entitled_charts(controlled, charts)

    assert [chart["title"] for chart in entitled] == ["리바로 매출 추이"]


@pytest.mark.parametrize(
    ("answer", "charts", "sources", "error"),
    (
        ("channel_distribution", (), ("UBIST",), "internal_identifier_exposed"),
        ("예측값 8618859701.348597원", (), ("UBIST",), "raw_canonical_numeric_exposed"),
        (
            "근거 있는 답변\n\n## 출처\n| 출처 | 기준 기간 |\n| --- | --- |\n| UBIST | 2026-06 |",
            ({"title": "매출 추이", "tooltip": "8618859701.348597", "evidence_refs": ["UBIST.render_data.brand_value_series_10pt"]},),
            ("UBIST",),
            "raw_canonical_numeric_exposed",
        ),
        ("근거 있는 답변", (), (), "required_source_block_missing"),
        (
            "설명 문장 안의 ## 출처 표시는 출처 블록이 아닙니다.",
            (),
            ("UBIST",),
            "required_source_block_missing",
        ),
        (
            "근거 있는 답변\n\n## 출처\n| 출처 | 기준 |\n| --- | --- |\n| UBIST | 2026-06 |",
            ({"title": "HHI 추이", "evidence_refs": ["unentitled"]},),
            ("UBIST",),
            "chart_evidence_unentitled",
        ),
    ),
)
def test_surface_gate_rejects_serialized_contract_violations(
    answer: str,
    charts: tuple[dict[str, object], ...],
    sources: tuple[str, ...],
    error: str,
) -> None:
    with pytest.raises(AnswerGateError, match=error):
        projection.validate_controlled_surface(
            answer=answer,
            charts=charts,
            sources=sources,
            answer_status="complete",
            allowed_evidence_ids=("UBIST.render_data.brand_value_series_10pt",),
            source_block_required=True,
        )
