from __future__ import annotations

from datetime import UTC, datetime
import json

import pytest

from jw_chat_agent_poc.service.v4.contracts import (
    Citation,
    EvidenceEnvelope,
    PlannerOutput,
    SourceResult,
    ToolQueries,
)
from jw_chat_agent_poc.service.v4.gates import apply_v4_gates
from jw_chat_agent_poc.service.v4.reason_code_enforcement import enforce_reason_codes
from jw_chat_agent_poc.service.v4.adapters import (
    _CANONICAL_DEEP_ANALYSIS_SQL,
    _analysis_timestamp,
    _deep_analysis_call_from_row,
    _strategic_mart_calls,
)
from jw_chat_agent_poc.service.v4.runtime import (
    _bind_session_state_contract,
    _derive_session_state,
    _results_from_session_state,
)
from jw_chat_agent_poc.service.v4.session_state import SessionState
from jw_chat_agent_poc.service.v4.synthesizer import (
    V4Synthesizer,
    _cause_table_packets,
    _comparison_facts,
    _mart_block,
    _synthesis_messages,
)


def _mart_comparison(
    *,
    eligible_attributions: tuple[str, ...] = ("observed_association",),
) -> SourceResult:
    return SourceResult(
        source="mart",
        query="리바로젯과 리피토 매출 비교",
        status="ok",
        payload={
            "calls": [
                {
                    "entity_bundle": {
                        "anchor": "리바로젯",
                        "period_start": "2025-09",
                        "period_end": "2026-06",
                        "same_period_and_denominator": True,
                        "members": [
                            {
                                "brand": "리바로젯",
                                "role": "target",
                                "sales_delta_억원": 13.54,
                                "render_data": {
                                    "brand_value_series_10pt": [
                                        {"period": "2025-09", "value_억원": 111.004},
                                        {"period": "2026-06", "value_억원": 124.544},
                                    ]
                                },
                            },
                            {
                                "brand": "리피토",
                                "role": "competitor",
                                "sales_delta_억원": -13.49,
                                "render_data": {
                                    "brand_value_series_10pt": [
                                        {"period": "2025-09", "value_억원": 52.49},
                                        {"period": "2026-06", "value_억원": 39.00},
                                    ]
                                },
                            },
                        ],
                    }
                }
            ]
        },
        evidence=EvidenceEnvelope(
            kind="mart",
            entity_match="EXACT",
            source_scope="KR",
            time_match="MATCH",
            subject_grain="brand",
            eligible_attributions=eligible_attributions,
        ),
        citations=(
            Citation(
                source="UBIST",
                query="리바로젯과 리피토 매출 비교",
                retrieved_at=datetime.now(UTC),
            ),
        ),
    )


def _absence(status: str) -> SourceResult:
    return SourceResult(
        source="hira",
        query="마운자로 급여기준",
        status="empty",
        payload={
            "absence_confirmation": {
                "source": "hira",
                "doc_type": "reimbursement",
                "status": status,
                "subject": "마운자로",
            }
        },
        evidence=EvidenceEnvelope(
            kind="hira",
            entity_match="EXACT",
            source_scope="KR",
            time_match="NOT_REQUESTED",
            eligible_claims=(
                "reimbursement",
                "absence_confirmation",
                "absence_confirmation:reimbursement",
            ),
            causal=False,
            metric_type="document_absence",
            product=("마운자로",),
            subject_grain="brand",
        ),
    )


def _mart_single() -> SourceResult:
    return SourceResult(
        source="mart",
        query="리바로 매출",
        status="ok",
        payload={
            "calls": [
                {
                    "summary_text": "리바로 2026-06 UBIST 전략 mart 지표: 매출 85.87억원.",
                    "render_data": {"brand": "리바로", "sales_eok": 85.87},
                }
            ]
        },
        evidence=EvidenceEnvelope(
            kind="mart",
            entity_match="EXACT",
            source_scope="KR",
            time_match="MATCH",
            subject_grain="brand",
        ),
    )


def _plan(question: str) -> PlannerOutput:
    queries = ToolQueries(
        mart=(question,),
        nedrug=(question,),
        hira=(question,),
        openfda=(question,),
        clinicaltrials=(question,),
        web=(question,),
        patent=(question,),
    )
    return PlannerOutput(
        resolved_question=question,
        expanded_intents=(question,),
        tool_queries=queries,
        linking_plan="동일 질문으로 조회",
    )


def test_r11_unsupported_transfer_is_downgraded_to_symmetric_observation() -> None:
    # Given: opposite sales movements without patient- or prescriber-flow evidence.
    result = _mart_comparison()

    # When: synthesis attributes the decrease directly to the other brand.
    gated = apply_v4_gates(
        "리바로젯과 리피토 매출 비교",
        "리바로젯은 +13.54억원, 리피토는 -13.49억원이며 리피토 감소분이 리바로젯으로 이동했습니다.",
        (result,),
    )

    # Then: values and symmetry remain, while direct flow attribution is bounded.
    assert "리바로젯은 +13.54억원" in gated.text
    assert "리피토는 -13.49억원" in gated.text
    assert "반대 방향의 변화가 관측됐습니다" in gated.text
    assert "직접 이동 여부는 현재 자료로 확인되지 않습니다" in gated.text
    assert "감소분이 리바로젯으로 이동" not in gated.text
    assert gated.trace["reason_code_enforcement"]["UNSUPPORTED_TRANSFER_ATTRIBUTION"] == 1


def test_r11_transfer_survives_when_exact_pair_has_flow_evidence() -> None:
    # Given: the exact directed pair is explicitly eligible in the envelope.
    result = _mart_comparison(
        eligible_attributions=("flow:리피토->리바로젯",),
    )
    answer = "리피토 감소분이 리바로젯으로 이동한 것으로 확인됐습니다."

    # When: the reason-code gate runs.
    gated = apply_v4_gates(
        "리바로젯과 리피토 매출 비교",
        answer,
        (result,),
    )

    # Then: eligible flow attribution is not downgraded.
    assert "리피토 감소분이 리바로젯으로 이동" in gated.text
    assert gated.trace["reason_code_enforcement"]["UNSUPPORTED_TRANSFER_ATTRIBUTION"] == 0


@pytest.mark.parametrize(
    "bounded_claim",
    [
        "직접 이동 여부는 현재 자료로 확인되지 않습니다.",
        "브랜드 간 전환 여부는 추가 확인이 필요합니다.",
        "처방 대체를 단정할 근거는 부족합니다.",
    ],
)
def test_r11_bounded_transfer_language_is_not_a_false_positive(
    bounded_claim: str,
) -> None:
    gated = apply_v4_gates(
        "리바로젯과 리피토 매출 비교",
        bounded_claim,
        (_mart_comparison(),),
    )

    assert bounded_claim in gated.text
    assert gated.trace["reason_code_enforcement"]["UNSUPPORTED_TRANSFER_ATTRIBUTION"] == 0


def test_r11_bounded_transfer_answer_is_byte_unchanged() -> None:
    answer = (
        "기준 관측의 매출은 124.54억원입니다. "
        "직접 이동 여부는 현재 자료로 확인되지 않습니다. "
        "[출처: 내부 데이터마트]"
    )

    repaired, trace = enforce_reason_codes(answer, (_mart_comparison(),))

    assert repaired == answer
    assert trace["UNSUPPORTED_TRANSFER_ATTRIBUTION"] == 0


def test_r11_bounded_transfer_clause_does_not_waive_neighbor_overclaim() -> None:
    gated = apply_v4_gates(
        "리바로젯과 리피토 매출 비교",
        "처방 대체를 단정할 근거는 부족하지만 시장 잠식이 나타났습니다.",
        (_mart_comparison(),),
    )

    assert "단정할 근거는 부족" in gated.text
    assert "시장 잠식이 나타났습니다" not in gated.text
    assert "직접 이동 여부는 현재 자료로 확인되지 않습니다" in gated.text
    assert gated.trace["reason_code_enforcement"]["UNSUPPORTED_TRANSFER_ATTRIBUTION"] == 1


@pytest.mark.parametrize(
    "claim",
    [
        "리피토 감소분이 리바로젯으로 이동했습니다.",
        "리피토에서 리바로젯으로 이동했습니다.",
        "리피토 -> 리바로젯 이동했습니다.",
        "리피토를 리바로젯으로 대체했습니다.",
    ],
)
def test_r11_transfer_syntaxes_require_the_exact_directed_pair(claim: str) -> None:
    result = _mart_comparison(
        eligible_attributions=("flow:리바로젯->리피토",),
    )

    gated = apply_v4_gates(
        "리바로젯과 리피토 매출 비교",
        claim,
        (result,),
    )

    assert claim.rstrip(".") not in gated.text
    assert "직접 이동 여부는 현재 자료로 확인되지 않습니다" in gated.text
    assert gated.trace["reason_code_enforcement"]["UNSUPPORTED_TRANSFER_ATTRIBUTION"] == 1


def test_r11_grounded_and_unsupported_transfer_pairs_are_checked_independently() -> None:
    result = _mart_comparison(
        eligible_attributions=("flow:리피토->리바로젯",),
    )
    answer = (
        "리피토에서 리바로젯으로 이동했습니다. "
        "리바로젯에서 리피토로 이동했습니다."
    )

    gated = apply_v4_gates(
        "리바로젯과 리피토 매출 비교",
        answer,
        (result,),
    )

    assert "리피토에서 리바로젯으로 이동했습니다" in gated.text
    assert "리바로젯에서 리피토로 이동했습니다" not in gated.text
    assert "직접 이동 여부는 현재 자료로 확인되지 않습니다" in gated.text


def test_r11_transfer_repair_preserves_grounded_markdown_and_sources() -> None:
    result = _mart_comparison()
    answer = (
        "## 핵심 답\n"
        "리바로젯 매출은 124.54억원입니다. "
        "리피토 감소분이 리바로젯으로 이동했습니다.\n\n"
        "## 출처\n- UBIST"
    )

    gated = apply_v4_gates(
        "리바로젯과 리피토 매출 비교",
        answer,
        (result,),
    )

    assert "## 핵심 답\n" in gated.text
    assert "리바로젯 매출은 124.54억원입니다" in gated.text
    assert "## 출처\n" in gated.text
    assert "내부 데이터마트" in gated.text
    assert "감소분이 리바로젯으로 이동" not in gated.text


def test_r11_transfer_observation_uses_the_claimed_pair_not_the_first_pair() -> None:
    result = _mart_comparison()
    bundle = result.payload["calls"][0]["entity_bundle"]
    bundle["members"] = [
        {
            "brand": "가상증가",
            "role": "competitor",
            "render_data": {
                "brand_value_series_10pt": [
                    {"period": "2025-09", "value_억원": 10.00},
                    {"period": "2026-06", "value_억원": 20.00},
                ]
            },
        },
        {
            "brand": "가상감소",
            "role": "competitor",
            "render_data": {
                "brand_value_series_10pt": [
                    {"period": "2025-09", "value_억원": 20.00},
                    {"period": "2026-06", "value_억원": 10.00},
                ]
            },
        },
        *bundle["members"],
    ]

    gated = apply_v4_gates(
        "리바로젯과 리피토 매출 비교",
        "리피토 감소분이 리바로젯으로 이동했습니다.",
        (result,),
    )

    assert "리바로젯" in gated.text
    assert "리피토" in gated.text
    assert "가상증가" not in gated.text
    assert "가상감소" not in gated.text


@pytest.mark.parametrize("status", ["doc_not_found", "coverage_unknown"])
def test_r11_unconfirmed_absence_cannot_surface_non_reimbursed(status: str) -> None:
    # Given: document absence that does not prove reimbursement status.
    result = _absence(status)

    # When: synthesis overclaims non-reimbursement.
    gated = apply_v4_gates(
        "마운자로 급여기준",
        "마운자로는 현재 급여기준이 없습니다(비급여). [출처: HIRA]",
        (result,),
    )

    # Then: the answer uses the bounded HIRA lookup wording.
    assert "현재 조회한 HIRA 세부 급여기준에서는 별도 기준을 찾지 못했습니다" in gated.text
    assert "이 결과만으로 비급여 여부를 확정할 수는 없습니다" in gated.text
    assert "급여기준이 없습니다(비급여)" not in gated.text
    assert gated.trace["reason_code_enforcement"]["ABSENCE_OVERCLAIM"] == 1


def test_r11_already_bounded_absence_language_is_not_a_false_positive() -> None:
    answer = (
        "현재 조회한 HIRA 세부 급여기준에서는 별도 기준을 찾지 못했습니다. "
        "이 결과만으로 비급여 여부를 확정할 수는 없습니다."
    )

    gated = apply_v4_gates(
        "마운자로 급여기준",
        answer,
        (_absence("doc_not_found"),),
    )

    assert gated.text.startswith(answer)
    assert gated.trace["reason_code_enforcement"]["ABSENCE_OVERCLAIM"] == 0


def test_r11_unconfirmed_approval_absence_cannot_be_stated_as_certain() -> None:
    result = SourceResult(
        source="nedrug",
        query="가상약 허가",
        status="empty",
        payload={
            "absence_confirmation": {
                "source": "nedrug",
                "doc_type": "approval",
                "status": "doc_not_found",
                "subject": "가상약",
            }
        },
        evidence=EvidenceEnvelope(
            kind="nedrug",
            entity_match="EXACT",
            source_scope="KR",
            time_match="NOT_REQUESTED",
            eligible_claims=("approval",),
            product=("가상약",),
            subject_grain="brand",
        ),
    )

    gated = apply_v4_gates(
        "가상약 허가",
        "가상약은 허가 문서가 없습니다.",
        (result,),
    )

    assert "허가 문서를 찾지 못했습니다" in gated.text
    assert "허가 부재를 확정할 수는 없습니다" in gated.text
    assert gated.trace["reason_code_enforcement"]["ABSENCE_OVERCLAIM"] == 1


def test_r11_confirmed_non_reimbursed_can_keep_absence_claim() -> None:
    # Given: official coverage evidence confirms non-reimbursement.
    result = _absence("confirmed_non_reimbursed")
    answer = "마운자로는 현재 급여기준이 없습니다(비급여). [출처: HIRA]"

    # When: the answer is gated.
    gated = apply_v4_gates("마운자로 급여기준", answer, (result,))

    # Then: the confirmed statement remains eligible.
    assert "급여기준이 없습니다(비급여)" in gated.text
    assert gated.trace["reason_code_enforcement"]["ABSENCE_OVERCLAIM"] == 0


def test_r11_confirmed_absence_only_authorizes_the_matching_subject() -> None:
    result = _absence("confirmed_non_reimbursed")

    gated = apply_v4_gates(
        "리바로 급여기준",
        "리바로는 현재 급여기준이 없습니다(비급여).",
        (result,),
    )

    assert "리바로는 현재 급여기준이 없습니다(비급여)" not in gated.text
    assert "비급여 여부를 확정할 수는 없습니다" in gated.text
    assert gated.trace["reason_code_enforcement"]["ABSENCE_OVERCLAIM"] == 1


def test_r11_confirmed_absence_is_downgraded_when_answer_contradicts_itself() -> None:
    result = _absence("confirmed_non_reimbursed")
    answer = (
        "마운자로는 현재 급여기준이 없습니다(비급여). "
        "다만 현재 자료에서는 비급여 여부가 확인되지 않았습니다."
    )

    gated = apply_v4_gates("마운자로 급여기준", answer, (result,))

    assert "급여기준이 없습니다(비급여)" not in gated.text
    assert "비급여 여부를 확정할 수는 없습니다" in gated.text
    assert gated.trace["reason_code_enforcement"]["ABSENCE_OVERCLAIM"] == 1


def test_r11_absence_certainty_without_typed_record_is_bounded() -> None:
    result = SourceResult(
        source="hira",
        query="리바로 급여기준",
        status="empty",
        payload={"items": []},
    )

    gated = apply_v4_gates(
        "리바로 급여기준",
        "리바로는 비급여입니다.",
        (result,),
    )

    assert "리바로는 비급여입니다" not in gated.text
    assert "비급여 여부를 확정할 수는 없습니다" in gated.text
    assert gated.trace["reason_code_enforcement"]["ABSENCE_OVERCLAIM"] == 1


def test_r11_absence_contradiction_downgrades_the_confirmed_clause() -> None:
    # Given: an unconfirmed record and a self-contradictory answer.
    result = _absence("doc_not_found")
    answer = (
        "마운자로는 비급여입니다. "
        "다만 현재 자료에서는 비급여 여부가 확인되지 않았습니다."
    )

    # When: the answer is gated.
    gated = apply_v4_gates("마운자로 급여기준", answer, (result,))

    # Then: certainty is downgraded and the contradiction disappears.
    assert "마운자로는 비급여입니다" not in gated.text
    assert "비급여 여부를 확정할 수는 없습니다" in gated.text
    assert gated.trace["reason_code_enforcement"]["ABSENCE_OVERCLAIM"] == 1


def test_r11_absence_repair_preserves_grounded_neighbor_clause() -> None:
    result = _absence("doc_not_found")
    answer = (
        "마운자로는 비급여입니다, 협상 결렬은 2025-02-03에 보도되었습니다."
    )

    gated = apply_v4_gates("마운자로 급여기준", answer, (result,))

    assert "비급여 여부를 확정할 수는 없습니다" in gated.text
    assert "협상 결렬은 2025-02-03에 보도되었습니다" in gated.text
    assert gated.trace["reason_code_enforcement"]["ABSENCE_OVERCLAIM"] == 1


@pytest.mark.parametrize(
    "leak",
    [
        "확인된 수치",
        "관련 자료를 병렬 조회했습니다",
        "전략 mart에서 조회했습니다",
        "해당 주장은 현재 근거 자격으로 확인되지 않았습니다",
        "리바로 2026-06 UBIST 전략 mart 지표: 매출 85.87억원, MS 3.72%, 순위 6위",
    ],
)
def test_r11_internal_release_tokens_never_reach_final_surface(leak: str) -> None:
    # Given: a leaked progress/placeholder clause beside a grounded public fact.
    result = _mart_single()
    answer = f"{leak}. 리바로 매출은 85.87억원입니다."

    # When: final gates run.
    gated = apply_v4_gates("리바로 매출 알려줘", answer, (result,))

    # Then: the internal clause is absent and the grounded sentence survives.
    assert leak not in gated.text
    assert "리바로 매출은 85.87억원입니다" in gated.text
    assert gated.trace["reason_code_enforcement"]["INTERNAL_TOKEN_LEAK"] >= 1


def test_r11_numeric_copy_repairs_to_grounded_prose_without_placeholder() -> None:
    # Given: an invented sales value and one verified mart display value.
    result = _mart_single()

    # When: numeric-copy repair runs.
    gated = apply_v4_gates(
        "리바로 매출 알려줘",
        "## 핵심 답\n리바로 매출은 99.99억원입니다.",
        (result,),
    )

    # Then: the verified value is rendered naturally with no release token.
    assert "99.99" not in gated.text
    assert "85.87억원" in gated.text
    assert "확인된 수치" not in gated.text
    assert "근거 자격으로 확인되지 않았습니다" not in gated.text


def test_r11_numeric_copy_removes_only_the_invented_metric_clause() -> None:
    result = _mart_single()

    gated = apply_v4_gates(
        "리바로 매출과 성장률 알려줘",
        "리바로 매출은 85.87억원이며 성장률은 99.99%입니다.",
        (result,),
    )

    assert "리바로 매출은 85.87억원" in gated.text
    assert "99.99" not in gated.text
    assert "성장률은 85.87%" not in gated.text
    assert "확인된 수치" not in gated.text


def test_r11_comparison_facts_delta_is_allowed_by_numeric_copy_gate() -> None:
    # Given: a delta computed from the typed entity bundle, not stored as a raw field.
    result = _mart_comparison()
    payload = result.payload
    payload["calls"][0]["entity_bundle"]["members"][0].pop("sales_delta_억원")
    payload["calls"][0]["entity_bundle"]["members"][1].pop("sales_delta_억원")
    result = result.model_copy(update={"payload": payload})

    # When: the computed display deltas appear in the answer.
    gated = apply_v4_gates(
        "리바로젯과 리피토 매출 비교",
        "리바로젯 매출은 +13.54억원, 리피토 매출은 -13.49억원 변했습니다.",
        (result,),
    )

    # Then: both code-derived values remain eligible.
    assert "+13.54억원" in gated.text
    assert "-13.49억원" in gated.text
    assert gated.trace["mart_numeric_copy_only"]["blocked"] is False


def test_r11_comparison_delta_uses_displayed_start_and_end_values() -> None:
    # Given: raw values whose independent two-decimal display changes the subtraction.
    result = _mart_comparison()
    bundle = result.payload["calls"][0]["entity_bundle"]
    member = bundle["members"][0]
    member["render_data"]["brand_value_series_10pt"] = [
        {"period": "2025-09", "value_억원": 111.004},
        {"period": "2026-06", "value_억원": 124.545},
    ]

    # When: COMPARISON_FACTS are built.
    facts = _comparison_facts((result.model_copy(update={"payload": result.payload}),))

    # Then: 111.00 -> 124.55 yields +13.55, never raw-delta +13.54.
    target = facts["brand_deltas"][0]
    assert target["start"] == "111.00억원"
    assert target["end"] == "124.55억원"
    assert target["delta"] == "+13.55억원"


def test_r11_inherited_year_recomputes_growth_and_share_from_that_period() -> None:
    result = SourceResult(
        source="mart",
        query="리바로 2023년 매출은 왜 늘었어?",
        status="ok",
        payload={
            "calls": [
                {
                    "render_data": {
                        "market_size_series": [
                            {"period": "2022-12", "value_억원": 1000.0},
                            {"period": "2023-12", "value_억원": 1081.30},
                        ]
                    }
                },
                {
                    "entity_bundle": {
                        "anchor": "리바로",
                        "requested_period": "2023",
                        "period_start": "2022-12",
                        "period_end": "2023-12",
                        "same_period_and_denominator": True,
                        "members": [
                            {
                                "brand": "리바로",
                                "role": "target",
                                "share_delta_pctp": 9.99,
                                "render_data": {
                                    "brand_value_series_10pt": [
                                        {"period": "2022-12", "value_억원": 100.0},
                                        {"period": "2023-12", "value_억원": 106.4},
                                    ]
                                },
                            }
                        ],
                    }
                },
            ]
        },
    )

    facts = _comparison_facts((result,))

    assert facts["period_start"] == "2022-12"
    assert facts["period_end"] == "2023-12"
    assert facts["share_direction"]["brand_growth"] == "+6.40%"
    assert facts["share_direction"]["market_growth"] == "+8.13%"
    assert facts["share_direction"]["share_delta"] == "-0.16%p"
    assert "점유율 방향은 하락입니다" in facts["share_direction"]["statement"]


@pytest.mark.parametrize(
    ("source", "payload", "answer", "expected", "forbidden"),
    [
        (
            "patent",
            {"items": [{"patent_expiry": "2024-06-30"}]},
            "해당 특허는 2024년 6월 만료를 앞두고 있습니다.",
            "만료일은 이미 경과했습니다",
            "앞두고 있습니다",
        ),
        (
            "clinicaltrials",
            {
                "items": [
                    {
                        "start_date": "2026-07-01",
                        "recruitment_status": "RECRUITING",
                    }
                ]
            },
            "시험은 2026년 7월 시작을 앞두고 있습니다.",
            "시작일이 도래했으며 현재 모집상태는 RECRUITING입니다",
            "앞두고 있습니다",
        ),
    ],
)
def test_r11_as_of_date_rewrites_past_dates_out_of_future_tense(
    source: str,
    payload: dict[str, object],
    answer: str,
    expected: str,
    forbidden: str,
) -> None:
    # Given: a payload date that is earlier than the response date.
    result = SourceResult(
        source=source,
        query="시점 확인",
        status="ok",
        payload=payload,
        evidence=EvidenceEnvelope(
            kind="patent" if source == "patent" else "clinical",
            entity_match="EXACT",
            source_scope="GLOBAL",
            time_match="MATCH",
            eligible_claims=("patent",) if source == "patent" else ("recruitment_status",),
        ),
    )

    # When: the final answer is validated against the observed date.
    gated = apply_v4_gates("시점 알려줘", answer, (result,))

    # Then: future tense is replaced by a dated current/past statement.
    assert expected in gated.text
    assert forbidden not in gated.text
    assert gated.trace["reason_code_enforcement"]["AS_OF_DATE"] == 1


def test_r11_as_of_date_repair_preserves_grounded_neighbor_clause() -> None:
    result = SourceResult(
        source="patent",
        query="특허 만료",
        status="ok",
        payload={"items": [{"patent_expiry": "2024-06-30"}]},
        evidence=EvidenceEnvelope(
            kind="patent",
            entity_match="EXACT",
            source_scope="GLOBAL",
            time_match="MATCH",
            eligible_claims=("patent",),
        ),
    )
    answer = (
        "해당 특허는 2024년 6월 만료를 앞두고 있으며, "
        "후속 특허는 2028년까지 유효합니다."
    )

    gated = apply_v4_gates("특허 만료를 알려줘", answer, (result,))

    assert "만료일은 이미 경과했습니다" in gated.text
    assert "후속 특허는 2028년까지 유효합니다" in gated.text
    assert gated.trace["reason_code_enforcement"]["AS_OF_DATE"] == 1


def test_r11_as_of_date_repair_preserves_sentence_separator() -> None:
    result = SourceResult(
        source="patent",
        query="특허 만료",
        status="ok",
        payload={"items": [{"patent_expiry": "2024-06-30"}]},
        evidence=EvidenceEnvelope(
            kind="patent",
            entity_match="EXACT",
            source_scope="GLOBAL",
            time_match="MATCH",
            eligible_claims=("patent",),
        ),
    )
    answer = (
        "전략 업데이트는 유지합니다. "
        "해당 특허는 2024년 6월 만료를 앞두고 있습니다."
    )

    gated = apply_v4_gates("특허 만료를 알려줘", answer, (result,))

    assert "유지합니다. 해당 특허" in gated.text


def test_r11_as_of_date_repair_preserves_preceding_numeric_clause() -> None:
    result = SourceResult(
        source="patent",
        query="리바로 특허 만료",
        status="ok",
        payload={"items": [{"patent_expiry": "2024-06-30"}]},
        evidence=EvidenceEnvelope(
            kind="patent",
            entity_match="EXACT",
            source_scope="GLOBAL",
            time_match="MATCH",
            eligible_claims=("patent",),
        ),
    )

    gated = apply_v4_gates(
        "리바로 매출과 특허 만료",
        "리바로 매출은 85.87억원이고 특허는 2024년 6월 만료될 예정입니다.",
        (_mart_single(), result),
    )

    assert "리바로 매출은 85.87억원" in gated.text
    assert "만료일은 이미 경과했습니다" in gated.text
    assert "예정입니다" not in gated.text


def test_r11_two_semantic_repairs_are_recorded_without_auto_repair() -> None:
    # Given: one answer would need both transfer and date-semantic repairs.
    comparison = _mart_comparison()
    patent = SourceResult(
        source="patent",
        query="특허 만료",
        status="ok",
        payload={"items": [{"patent_expiry": "2024-06-30"}]},
        evidence=EvidenceEnvelope(
            kind="patent",
            entity_match="EXACT",
            source_scope="GLOBAL",
            time_match="MATCH",
            eligible_claims=("patent",),
        ),
    )
    answer = (
        "리피토 감소분이 리바로젯으로 이동했습니다. "
        "해당 특허는 2024년 6월 만료를 앞두고 있습니다."
    )

    # When: reason-code enforcement detects both repairs.
    gated = apply_v4_gates(
        "리바로젯 이동과 특허 만료를 알려줘",
        answer,
        (comparison, patent),
    )

    # Then: semantic text is left for review while both findings are recorded.
    trace = gated.trace["reason_code_enforcement"]
    assert trace["review_only"] is True
    assert trace["semantic_repair_candidates"] == 2
    assert "리피토 감소분이 리바로젯으로 이동했습니다" in gated.text
    assert "만료를 앞두고 있습니다" in gated.text


def test_r11_session_state_round_trips_explicit_anchor_and_filter_fields() -> None:
    state = SessionState(
        canonical_entities=("리바로", "리바로젯"),
        primary_entity="리바로",
        mentioned_related_entities=("리바로젯",),
        record_type="clinical_trial",
        status_filter=("active",),
        country_filter=("KR",),
    )

    restored = SessionState.from_value(state.public_dict())

    assert restored.primary_entity == "리바로"
    assert restored.mentioned_related_entities == ("리바로젯",)
    assert restored.record_type == "clinical_trial"
    assert restored.status_filter == ("active",)
    assert restored.country_filter == ("KR",)


def test_r11_related_entity_does_not_replace_inherited_primary_entity() -> None:
    previous = SessionState(
        canonical_entities=("리바로",),
        primary_entity="리바로",
        comparison_anchor="리바로",
    )
    result = SourceResult(
        source="mart",
        query="리바로와 리바로젯 비교",
        status="ok",
        payload={
            "calls": [
                {
                    "entity_bundle": {
                        "anchor": "리바로",
                        "members": [
                            {"brand": "리바로"},
                            {"brand": "리바로젯"},
                        ],
                    }
                }
            ]
        },
    )

    state = _derive_session_state(
        "재심사 언제 끝나?",
        _plan("리바로 재심사 언제 끝나?"),
        (result,),
        previous=previous,
    )

    assert state.primary_entity == "리바로"
    assert state.mentioned_related_entities == ("리바로젯",)
    assert state.comparison_anchor == "리바로"


def test_r11_clinical_record_status_and_country_filters_are_explicit() -> None:
    result = SourceResult(
        source="clinicaltrials",
        query="당뇨망막병증 임상 국내 진행 중",
        status="ok",
        payload={
            "items": [
                {
                    "product_name": "아일리아",
                    "study_id": "NCT00000001",
                    "recruitment_status": "RECRUITING",
                    "country": "Korea, Republic of",
                }
            ]
        },
    )

    state = _derive_session_state(
        "당뇨망막병증 임상 중 국내 진행 중인 것만",
        _plan("당뇨망막병증 임상 중 국내 진행 중인 것만"),
        (result,),
        previous=None,
    )

    assert state.record_type == "clinical_trial"
    assert state.status_filter == ("active",)
    assert state.country_filter == ("KR",)


def test_r11_empty_active_kr_clinical_set_precedes_adjacent_evidence() -> None:
    class Client:
        def complete(
            self,
            _messages: object,
            *,
            budget_s: float,
            max_tokens: int,
        ) -> str:
            del budget_s, max_tokens
            return (
                "## 핵심 답\n특허와 바이오시밀러 동향을 확인했습니다.\n\n"
                "## 근거와 맥락\n인접 자료입니다.\n\n"
                "## 종합 인사이트\n추가 확인이 필요합니다.\n\n"
                "## 미확인 요소\n활성 임상은 확인되지 않았습니다.\n\n"
                "## 출처\n- 특허 자료"
            )

    clinical = SourceResult(
        source="clinicaltrials",
        query="당뇨망막병증 임상 국내 진행 중",
        status="empty",
        payload={"items": []},
    )
    patent = SourceResult(
        source="patent",
        query="당뇨망막병증 특허",
        status="ok",
        payload={"items": [{"title": "인접 특허 동향"}]},
    )
    state = SessionState(
        record_type="clinical_trial",
        status_filter=("active",),
        country_filter=("KR",),
    )

    answer = V4Synthesizer(Client()).synthesize(
        _plan("당뇨망막병증 임상 중 국내 진행 중인 것만"),
        (clinical, patent),
        (),
        state=state,
    )

    assert answer.startswith("## 핵심 답\n확인된 국내 active 임상시험은 없었습니다.")
    assert "## 인접 동향" in answer
    assert "특허와 바이오시밀러 동향" in answer


def test_r11_old_clinical_scope_does_not_leak_into_new_reimbursement_topic() -> None:
    class Client:
        def complete(self, _messages: object, *, budget_s: float, max_tokens: int) -> str:
            del budget_s, max_tokens
            return "## 핵심 답\n아일리아 급여기준을 확인했습니다."

    old_state = SessionState(
        primary_entity="리바로",
        record_type="clinical_trial",
        status_filter=("active",),
        country_filter=("KR",),
    )
    clinical = SourceResult(
        source="clinicaltrials",
        query="이전 임상",
        status="empty",
        payload={"items": []},
    )
    hira = SourceResult(
        source="hira",
        query="아일리아 급여기준",
        status="ok",
        payload={"items": [{"document": "급여기준"}]},
    )

    answer = V4Synthesizer(Client()).synthesize(
        _plan("아일리아 급여기준 알려줘"),
        (clinical, hira),
        (),
        state=old_state,
    )

    assert "확인된 국내 active 임상시험은 없었습니다" not in answer


def test_r11_active_kr_notice_uses_requested_subset_inside_ok_result() -> None:
    class Client:
        def complete(self, _messages: object, *, budget_s: float, max_tokens: int) -> str:
            del budget_s, max_tokens
            return "## 핵심 답\n해외 종료 임상만 확인했습니다."

    clinical = SourceResult(
        source="clinicaltrials",
        query="당뇨망막병증 임상 국내 진행 중",
        status="ok",
        payload={
            "items": [
                {
                    "study_id": "NCT00000002",
                    "recruitment_status": "COMPLETED",
                    "country": "United States",
                }
            ]
        },
    )

    answer = V4Synthesizer(Client()).synthesize(
        _plan("당뇨망막병증 임상 중 국내 진행 중인 것만"),
        (clinical,),
        (),
    )

    assert answer.startswith("## 핵심 답\n확인된 국내 active 임상시험은 없었습니다.")
    assert "## 인접 동향" in answer


def test_r11_followup_plan_is_bound_to_inherited_session_contract() -> None:
    state = SessionState(
        canonical_entities=("리바로",),
        primary_entity="리바로",
        record_type="clinical_trial",
        status_filter=("active",),
        country_filter=("KR",),
        time_window=("2023",),
    )

    bound = _bind_session_state_contract(
        _plan("그 중 결과 알려줘"),
        "그 중 결과 알려줘",
        state,
    )

    for required in ("리바로", "임상시험", "진행 중", "국내", "2023"):
        assert required in bound.resolved_question
        assert required in bound.tool_queries.clinicaltrials[0]


def test_r11_explicit_topic_switch_does_not_over_inherit_old_filters() -> None:
    state = SessionState(
        canonical_entities=("리바로",),
        primary_entity="리바로",
        record_type="clinical_trial",
        status_filter=("active",),
        country_filter=("KR",),
        time_window=("2023",),
    )

    bound = _bind_session_state_contract(
        _plan("아일리아 급여기준 알려줘"),
        "아일리아 급여기준 알려줘",
        state,
    )

    assert bound.resolved_question == "아일리아 급여기준 알려줘"
    assert "리바로" not in bound.resolved_question
    assert "임상시험" not in bound.resolved_question
    assert "진행 중" not in bound.resolved_question
    assert "국내" not in bound.resolved_question
    assert "2023" not in bound.resolved_question


def test_r11_explicit_related_entity_does_not_inherit_the_old_primary() -> None:
    state = SessionState(
        canonical_entities=("리바로", "크레스토"),
        primary_entity="리바로",
        mentioned_related_entities=("크레스토",),
        record_type="market_metric",
    )

    bound = _bind_session_state_contract(
        _plan("크레스토 재심사 언제 끝나?"),
        "크레스토 재심사 언제 끝나?",
        state,
    )

    assert bound.resolved_question == "크레스토 재심사 언제 끝나?"
    assert "리바로" not in bound.resolved_question


def test_r11_strategic_mart_calls_use_the_inherited_explicit_year() -> None:
    class Layer:
        def __init__(self) -> None:
            self.periods: list[str] = []

        def market_scope(self, brand: str) -> dict[str, object]:
            return {
                "source": "UBIST",
                "render_data": {"market_id": "ml_statins"},
            }

        def brand_metric(
            self,
            brand: str,
            metric: str,
            period: str,
            **_: object,
        ) -> dict[str, object]:
            self.periods.append(period)
            return {"source": "UBIST", "render_data": {"period": period}}

        def top_brands(self, *_: object, **__: object) -> dict[str, object]:
            raise LookupError

        def cause_card_data(self, *_: object, **__: object) -> dict[str, object]:
            return {}

    layer = Layer()

    _strategic_mart_calls(layer, "리바로", "리바로 2023년 매출은 왜 늘었어?")

    assert layer.periods == ["2023", "2023", "2023", "2023"]


def test_r11_restored_result_projects_explicit_session_contract() -> None:
    state = SessionState(
        canonical_entities=("리바로", "리바로젯"),
        primary_entity="리바로",
        mentioned_related_entities=("리바로젯",),
        record_type="market_metric",
        status_filter=("active",),
        country_filter=("KR",),
        last_numeric_facts=(
            {"source": "mart", "path": "calls[0].sales", "value": 85.87},
        ),
    )

    restored = _results_from_session_state(state)

    assert len(restored) == 1
    payload = restored[0].payload
    assert payload["anchor_brand"] == "리바로"
    assert payload["primary_entity"] == "리바로"
    assert payload["mentioned_related_entities"] == ("리바로젯",)
    assert payload["record_type"] == "market_metric"
    assert payload["status_filter"] == ("active",)
    assert payload["country_filter"] == ("KR",)


def test_r11_every_cause_table_has_subject_grain_and_period_labels() -> None:
    cause = {
        "company_ranking_series": [{"period": "2025-01", "value": 1}],
        "ei_ms": {"series": [{"period": "2025-01", "value": 2}]},
        "growth_contribution": {"series": [{"period": "2025-01", "value": 3}]},
        "analysis_level": "class_2",
        "analysis_level_trend": [{"period": "2025-01", "value": 4}],
        "customer_competition": [{"period": "2025-01", "value": 5}],
    }
    result = SourceResult(
        source="mart",
        query="리바로 매출 원인",
        status="ok",
        payload={
            "calls": [
                {
                    "tool": "cause_card_data",
                    "render_data": cause,
                    "cause_period_anchor": {
                        "period_start": "2025-01",
                        "period_end": "2025-12",
                    },
                }
            ]
        },
    )

    packets = _cause_table_packets(result)

    assert {packet["table"] for packet in packets} == set(cause)
    assert all(packet["subject_grain"] for packet in packets)
    assert all(packet["period"] == {"start": "2025-01", "end": "2025-12"} for packet in packets)
    by_table = {packet["table"]: packet for packet in packets}
    assert by_table["company_ranking_series"]["subject_grain"] == "company"
    assert by_table["ei_ms"]["subject_grain"] == "brand"
    assert by_table["analysis_level_trend"]["subject_grain"] == "class_2"
    assert by_table["customer_competition"]["subject_grain"] == "channel"


def test_r11_cause_alignment_keeps_non_common_period_rows() -> None:
    from jw_chat_agent_poc.service.v4.adapters import align_cause_periods

    payload = {
        "first_series": [
            {"period": "2025-01", "value": 1},
            {"period": "2025-02", "value": 2},
            {"period": "2025-03", "value": 3},
        ],
        "second_series": [
            {"period": "2025-02", "value": 20},
            {"period": "2025-03", "value": 30},
            {"period": "2025-04", "value": 40},
        ],
    }

    aligned, anchor = align_cause_periods(payload)

    assert anchor == {"period_start": "2025-02", "period_end": "2025-03"}
    assert [row["period"] for row in aligned["first_series"]] == [
        "2025-01",
        "2025-02",
        "2025-03",
    ]
    assert [row["period"] for row in aligned["second_series"]] == [
        "2025-02",
        "2025-03",
        "2025-04",
    ]


def test_r11_internal_datamart_contains_every_cause_table_without_display_cap() -> None:
    cause = {
        f"cause_table_{index}": [{"period": "2025-01", "value": index}]
        for index in range(1, 9)
    }
    result = SourceResult(
        source="mart",
        query="리바로 매출 원인",
        status="ok",
        payload={
            "calls": [
                {
                    "tool": "cause_card_data",
                    "render_data": cause,
                    "cause_period_anchor": {
                        "period_start": "2025-01",
                        "period_end": "2025-12",
                    },
                }
            ]
        },
    )

    block = _mart_block(result)

    for index in range(1, 9):
        assert f'"table": "cause_table_{index}"' in block
    assert block.count('"subject_grain": "market"') >= 8
    assert block.count('"start": "2025-01"') >= 8
    assert block.count('"end": "2025-12"') >= 8


def test_r11_deep_analysis_uses_only_canonical_agent2_table() -> None:
    normalized = " ".join(_CANONICAL_DEEP_ANALYSIS_SQL.split()).casefold()

    assert "from cache_deep_analysis_ai_analysis" in normalized
    assert "market_id in ({market_placeholders})" in normalized
    assert "order by greatest" in normalized
    assert "from cache_deep_analysis " not in normalized
    assert "from cache_cause" not in normalized


def test_r11_deep_analysis_timestamp_normalizes_naive_and_aware_values() -> None:
    naive = _analysis_timestamp(datetime(2026, 7, 12, 9, 0))
    aware = _analysis_timestamp("2026-07-12T09:00:00+09:00")

    assert naive is not None and naive.tzinfo is UTC
    assert aware is not None and aware.tzinfo is UTC
    assert aware.isoformat() == "2026-07-12T00:00:00+00:00"


def test_r11_deep_analysis_selects_latest_generated_variant_with_freshness() -> None:
    row = {
        "brand": "리바로",
        "market_id": "ml_statins",
        "ai_analysis_json": json.dumps({"variant": "legacy"}),
        "ai_analysis_short_json": json.dumps({"variant": "short"}),
        "ai_analysis_long_json": json.dumps({"variant": "long"}),
        "updated_at": datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
        "short_generated_at": datetime(2026, 7, 7, 9, 0, tzinfo=UTC),
        "long_generated_at": datetime(2026, 7, 12, 9, 0, tzinfo=UTC),
        "short_generation_status": "complete",
        "long_generation_status": "complete",
    }

    call = _deep_analysis_call_from_row(row, allowed_market_ids=("ml_statins",))

    assert call is not None
    assert call["canonical_table"] == "cache_deep_analysis_ai_analysis"
    assert call["analysis_variant"] == "long"
    assert call["analysis"] == {"variant": "long"}
    assert call["generated_at"].startswith("2026-07-12")
    assert call["freshness_label"] == "내부 심층분석 · 2026-W28 생성분"


def test_r11_deep_analysis_market_mismatch_is_not_injected() -> None:
    row = {
        "brand": "리바로",
        "market_id": "ml_other",
        "ai_analysis_json": json.dumps({"variant": "legacy"}),
        "updated_at": datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
    }

    assert _deep_analysis_call_from_row(row, allowed_market_ids=("ml_statins",)) is None


def test_r11_synthesis_separates_deep_analysis_from_datamart_payload() -> None:
    result = SourceResult(
        source="mart",
        query="리바로 요즘 어때",
        status="ok",
        payload={
            "calls": [
                {"render_data": {"sales_eok": 85.87}},
                {
                    "source": "내부 심층분석",
                    "tool": "agent2_deep_analysis",
                    "canonical_table": "cache_deep_analysis_ai_analysis",
                    "brand": "리바로",
                    "market_id": "ml_statins",
                    "analysis_variant": "long",
                    "analysis": {"phenomenon": {"title": "시장 변화"}},
                    "generated_at": "2026-07-12T09:00:00+00:00",
                    "freshness_label": "내부 심층분석 · 2026-W28 생성분",
                },
            ]
        },
    )

    messages = _synthesis_messages(
        _plan("리바로 요즘 어때"),
        (result,),
        (),
    )
    prompt = json.loads(messages[1]["content"])

    assert len(prompt["internal_deep_analysis"]) == 1
    assert "<INTERNAL_DEEP_ANALYSIS" in prompt["internal_deep_analysis"][0]
    assert "2026-W28 생성분" in prompt["internal_deep_analysis"][0]
    assert "agent2_deep_analysis" not in prompt["internal_datamart"][0]
    assert "85.87" in prompt["internal_datamart"][0]


@pytest.mark.parametrize(
    "question",
    [
        "리바로 재심사 언제 끝나?",
        "리바로 급여기준 알려줘",
        "리바로 허가사항 알려줘",
        "리바로 매출 알려줘",
        "리바로 점유율 알려줘",
        "리바로 환자수 알려줘",
        "리바로 특허 만료 알려줘",
        "리바로 부작용 알려줘",
        "리바로 효능효과 알려줘",
    ],
)
def test_r11_filter_incident_replay_does_not_attach_stale_clinical_scope(
    question: str,
) -> None:
    previous = SessionState(
        canonical_entities=("리바로",),
        primary_entity="리바로",
        record_type="clinical_trial",
        status_filter=("active",),
        country_filter=("KR",),
        time_window=("2023",),
    )

    bound = _bind_session_state_contract(_plan(question), question, previous)

    assert "임상시험" not in bound.resolved_question
    assert "진행 중" not in bound.resolved_question
    assert "2023" not in bound.resolved_question
