from __future__ import annotations

from datetime import UTC, date, datetime
import json

import pytest

from jw_chat_agent_poc.service.v4 import planner as v4_planner
from jw_chat_agent_poc.service.v4 import synthesizer as v4_synthesizer
from jw_chat_agent_poc.service.v4.adapters import _strategic_mart_calls
from jw_chat_agent_poc.service.v4.comparison_facts import build_comparison_facts
from jw_chat_agent_poc.service.v4.contracts import (
    EvidenceEnvelope,
    PlannerOutput,
    SourceResult,
    ToolQueries,
)
from jw_chat_agent_poc.service.v4.gates import apply_v4_gates
from jw_chat_agent_poc.service.v4.planner import V4Planner
from jw_chat_agent_poc.service.v4.reason_code_enforcement import enforce_reason_codes
from jw_chat_agent_poc.service.v4.session_state import SessionState
from jw_chat_agent_poc.service.v4.synthesizer import V4Synthesizer


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
        linking_plan="single wave",
    )


def _comparison_result(
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
    )


class _PlannerClient:
    serving_id = "190"

    def complete(self, _messages: object, *, budget_s: float) -> str:
        del budget_s
        plan = _plan("리바로 2021년~2023년 매출")
        return plan.model_dump_json()


class _SynthClient:
    def __init__(self, answer: str) -> None:
        self.answer = answer

    def complete(
        self,
        _messages: object,
        *,
        budget_s: float,
        max_tokens: int,
    ) -> str:
        del budget_s, max_tokens
        return self.answer


def test_r11b_as_of_date_is_in_dynamic_planner_and_synthesizer_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frozen = date(2026, 8, 12)
    monkeypatch.setattr(v4_planner, "_current_kst_date", lambda: frozen, raising=False)
    monkeypatch.setattr(v4_synthesizer, "_current_kst_date", lambda: frozen, raising=False)

    planner_messages = v4_planner._planner_messages("리바로 최근 3년 매출", ())
    synth_messages = v4_synthesizer._synthesis_messages(
        _plan("리바로 최근 3년 매출"),
        (_comparison_result(),),
        (),
    )

    assert "오늘은 2026-08-12이다" in planner_messages[1]["content"]
    assert "오늘은 2026-08-12이다" in synth_messages[1]["content"]
    assert "오늘은" not in planner_messages[0]["content"]
    assert "오늘은" not in synth_messages[0]["content"]


def test_r11b_relative_three_year_plan_is_anchored_to_request_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        v4_planner,
        "_current_kst_date",
        lambda: date(2026, 8, 12),
        raising=False,
    )

    plan = V4Planner(_PlannerClient()).plan("리바로 최근 3년 매출", ())

    serialized = plan.model_dump_json()
    assert "2023년~2026년" in serialized
    assert "2021년~2023년" not in serialized


def test_r11b_relative_three_year_mart_query_uses_latest_kst_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Layer:
        def __init__(self) -> None:
            self.requests: list[tuple[str, str, int]] = []

        def market_scope(self, _brand: str) -> dict[str, object]:
            return {
                "source": "UBIST",
                "render_data": {"market_id": "ml_statins"},
            }

        def brand_metric(
            self,
            brand: str,
            metric: str,
            period: str,
            *,
            history_points: int = 10,
            **_: object,
        ) -> dict[str, object]:
            self.requests.append((metric, period, history_points))
            return {
                "source": "UBIST",
                "render_data": {
                    "brand": brand,
                    "period": "2026-06",
                    "brand_value_series_10pt": [
                        {"period": "2021-07", "value_억원": 70.0},
                        {"period": "2023-01", "value_억원": 80.0},
                        {"period": "2026-06", "value_억원": 90.0},
                    ],
                    "market_size_series": [
                        {"period": "2021-07", "value_억원": 700.0},
                        {"period": "2023-01", "value_억원": 800.0},
                        {"period": "2026-06", "value_억원": 900.0},
                    ],
                },
            }

        def top_brands(self, *_: object, **__: object) -> dict[str, object]:
            return {
                "source": "UBIST",
                "render_data": {
                    "level_top5_trend_series": [
                        {"brand": "리바로", "company": "JW", "rank": 1},
                        {"brand": "리피토", "company": "Other", "rank": 2},
                    ]
                },
            }

        def cause_card_data(self, *_: object, **__: object) -> dict[str, object]:
            return {}

    monkeypatch.setattr(
        "jw_chat_agent_poc.service.v4.adapters.current_kst_date",
        lambda: date(2026, 8, 12),
    )
    layer = Layer()

    calls = _strategic_mart_calls(
        layer,
        "리바로",
        "리바로 2023년~2026년(최근 3년) 매출",
    )

    assert layer.requests
    assert {period for _, period, _ in layer.requests} == {"latest"}
    assert all(points == 37 for _, _, points in layer.requests)
    sales = next(call for call in calls if call.get("render_data", {}).get("brand") == "리바로")
    sales_periods = {
        row["period"]
        for row in sales["render_data"]["brand_value_series_10pt"]
    }
    assert sales_periods == {"2023-01", "2026-06"}
    bundle = next(call["entity_bundle"] for call in calls if "entity_bundle" in call)
    assert bundle["requested_period"] == "최근 3년"
    assert bundle["period_start"] == "2023-01"
    assert bundle["period_end"] == "2026-06"


def test_r11b_comparison_delta_uses_rounded_display_endpoints_once() -> None:
    facts = build_comparison_facts((_comparison_result(),))

    target = facts["brand_deltas"][0]
    assert target["start"] == "111.00억원"
    assert target["end"] == "124.54억원"
    assert target["delta"] == "+13.54억원"
    assert target["delta_basis"] == "display_end_minus_display_start"
    assert any("+13.54억원" in sentence for sentence in facts["observation_sentences"])
    assert all("억원로" not in sentence for sentence in facts["observation_sentences"])
    assert "13.73" not in json.dumps(facts, ensure_ascii=False)


@pytest.mark.parametrize(
    "answer",
    [
        "아토젯의 매출 증가가 리바로의 처방을 대체했을 가능성이 있으나 확인되지 않았습니다.",
        "리바로젯의 성장은 기존 리피토 처방 환자군의 이동과 연관이 있을 수 있습니다.",
        "리피토 매출 감소가 리바로젯 성장의 원인일 가능성이 있습니다.",
        "리피토의 점유율 하락 때문에 리바로젯 점유율이 상승한 것으로 보입니다.",
        "리피토 처방 감소는 리바로젯 환자군 유입과 연관될 수 있습니다.",
        "리바로젯 성장이 리피토 처방을 잠식했을 가능성을 배제하기 어렵습니다.",
        "리피토의 감소 결과 리바로젯 매출이 증가했을 수 있습니다.",
        "리피토 변화가 리바로젯 증가로 이어졌을 가능성이 있으나 확인되지 않았습니다.",
    ],
)
def test_r11b_structural_cross_brand_transfer_is_replaced_with_observation(
    answer: str,
) -> None:
    repaired, trace = enforce_reason_codes(answer, (_comparison_result(),))

    assert trace["UNSUPPORTED_TRANSFER_ATTRIBUTION"] == 1
    assert "반대 방향의 변화가 관측됐습니다" in repaired
    assert "직접 이동 여부는 현재 자료로 확인되지 않습니다" in repaired
    assert len(repaired) >= len(answer) * 0.9


def test_r11b_live_market_share_transfer_sentence_is_structurally_repaired() -> None:
    answer = (
        "결과적으로 리피토와 같은 기존 단일제 시장의 비중이 리바로젯을 "
        "비롯한 복합제 시장으로 점진적으로 전환되는 흐름이 나타나고 있습니다."
    )

    repaired, trace = enforce_reason_codes(answer, (_comparison_result(),))

    assert trace["UNSUPPORTED_TRANSFER_ATTRIBUTION"] == 1
    assert "반대 방향의 변화가 관측됐습니다" in repaired
    assert "직접 이동 여부는 현재 자료로 확인되지 않습니다" in repaired
    assert len(repaired) >= len(answer) * 0.9


@pytest.mark.parametrize(
    "answer",
    [
        "리바로젯 성장은 리피토 환자군 변화 가능성으로 해석됩니다.",
        "리바로젯 성장은 리피토 처방 변화 가설로 설명될 수 있습니다.",
        "리바로젯 성장은 리피토 처방 변화 가설이 제기될 수 있습니다.",
    ],
)
def test_r11b_structural_hypothesis_language_requires_flow_evidence(
    answer: str,
) -> None:
    repaired, trace = enforce_reason_codes(answer, (_comparison_result(),))

    assert repaired != answer
    assert "직접 이동 여부는 현재 자료로 확인되지 않습니다" in repaired
    assert trace["UNSUPPORTED_TRANSFER_ATTRIBUTION"] == 1


def test_r11b_structural_repair_preserves_full_answer_shape_and_length() -> None:
    model_answer = """## 핵심 답
리바로젯 성장은 리피토 환자군 변화 가능성으로 해석됩니다.

## 근거와 맥락
같은 기간의 브랜드 매출 추이를 비교했습니다.

## 데이터 표
| 브랜드 | 관측 |
|---|---|
| 리바로젯 | 증가 |
| 리피토 | 감소 |

## 종합 인사이트
브랜드별 변화를 같은 기간 기준으로 함께 봐야 합니다.

## 미확인 요소
환자 또는 처방자 수준의 직접 흐름은 확인되지 않았습니다.
"""
    result = _comparison_result()

    synthesized = V4Synthesizer(_SynthClient(model_answer)).synthesize(
        _plan("리바로젯과 리피토 매출 비교"),
        (result,),
        (),
    )
    gated = apply_v4_gates(
        "리바로젯과 리피토 매출 비교",
        synthesized,
        (result,),
    ).text

    assert len(gated) >= len(model_answer) * 0.9
    for heading in (
        "## 핵심 답",
        "## 근거와 맥락",
        "## 데이터 표",
        "## 종합 인사이트",
        "## 미확인 요소",
    ):
        assert heading in gated
    assert "직접 이동 여부는 현재 자료로 확인되지 않습니다" in gated


@pytest.mark.parametrize(
    "answer,result",
    [
        (
            "리바로(Livalo)의 매출은 줄었지만 같은 제품의 장기 추이를 더 확인해야 합니다.",
            _comparison_result(),
        ),
        (
            "스타틴 시장 매출 감소와 전체 점유율 하락이 함께 관측됐습니다.",
            _comparison_result(),
        ),
        (
            "리바로젯과 리피토의 매출 증감 차이를 차례로 설명합니다.",
            _comparison_result(),
        ),
        (
            "리피토 매출 감소가 리바로젯 성장으로 이어졌습니다.",
            _comparison_result(eligible_attributions=("flow:리피토->리바로젯",)),
        ),
        (
            "리바로젯 성장은 리피토 매출 감소의 결과입니다.",
            _comparison_result(eligible_attributions=("flow:리피토->리바로젯",)),
        ),
    ],
)
def test_r11b_structural_transfer_negative_controls_are_preserved(
    answer: str,
    result: SourceResult,
) -> None:
    repaired, trace = enforce_reason_codes(answer, (result,))

    assert repaired == answer
    assert trace["UNSUPPORTED_TRANSFER_ATTRIBUTION"] == 0


def test_r11b_active_kr_empty_contract_uses_exact_korean_surface() -> None:
    question = "대한민국 내에서 현재 진행 중인 당뇨망막병증 임상시험만 알려줘"
    clinical = SourceResult(
        source="clinicaltrials",
        query=question,
        status="empty",
        payload={"items": []},
    )
    adjacent = SourceResult(
        source="patent",
        query="인접 특허",
        status="ok",
        payload={"items": [{"title": "인접 동향"}]},
    )

    answer = V4Synthesizer(_SynthClient("## 핵심 답\n인접 자료입니다.")).synthesize(
        _plan(question),
        (clinical, adjacent),
        (),
    )

    assert answer.startswith("## 핵심 답\n확인된 국내 진행 중 임상시험은 없었습니다.")
    assert "## 인접 동향" in answer
    assert "active 임상시험" not in answer

    gated = apply_v4_gates(question, answer, (clinical, adjacent)).text
    assert gated.startswith("## 핵심 답\n확인된 국내 진행 중 임상시험은 없었습니다.")
    assert "## 인접 동향" in gated


def test_r11b_active_kr_empty_uses_country_scoped_query_when_payload_omits_country() -> None:
    question = "대한민국에서 현재 진행 중인 당뇨망막병증 임상시험 현황을 알려주세요."
    clinical = SourceResult(
        source="clinicaltrials",
        query="Diabetic Retinopathy AND Korea, Republic of",
        status="ok",
        payload={
            "calls": [
                {
                    "render_data": {
                        "payload": {
                            "studies": [
                                {
                                    "NCTId": "NCT03962296",
                                    "overallStatus": "COMPLETED",
                                }
                            ]
                        }
                    }
                }
            ]
        },
    )

    answer = V4Synthesizer(_SynthClient("## 핵심 답\n완료 임상 1건입니다.")).synthesize(
        _plan(question),
        (clinical,),
        (),
    )

    assert answer.startswith("## 핵심 답\n확인된 국내 진행 중 임상시험은 없었습니다.")
    assert "## 인접 동향" in answer


def test_r11b_reexamination_primary_entity_precedes_related_product() -> None:
    question = "리바로 재심사 기간 알려줘"
    nedrug = SourceResult(
        source="nedrug",
        query=question,
        status="ok",
        payload={
            "items": [
                {
                    "ITEM_NAME": "리바로정2밀리그램",
                    "REEXAM_TARGET": "대상 아님",
                },
                {
                    "ITEM_NAME": "리바로젯정",
                    "REEXAM_DATE": "2023-01-01~2029-12-31",
                },
            ]
        },
    )
    state = SessionState(primary_entity="리바로", canonical_entities=("리바로",))
    model_answer = "## 핵심 답\n리바로젯 재심사 기간은 2029년까지입니다."

    answer = V4Synthesizer(_SynthClient(model_answer)).synthesize(
        _plan(question),
        (nedrug,),
        (),
        state=state,
    )

    core, related = answer.split("## 관련 제품", 1)
    assert "리바로는 현재 재심사 대상이 아닙니다" in core
    assert "리바로젯" not in core
    assert "리바로젯" in related
    assert "2029-12-31" in related
    assert "기간이 경과" not in core


def test_r11b_explicit_reexamination_subject_overrides_stale_session_entity() -> None:
    question = "재심사 언제 끝나? 리바로"
    nedrug = SourceResult(
        source="nedrug",
        query="리바로젯 재심사",
        status="ok",
        payload={
            "items": [
                {
                    "ITEM_NAME": "리바로젯정",
                    "REEXAM_DATE": "2023-01-01~2029-12-31",
                }
            ]
        },
    )
    stale_state = SessionState(
        primary_entity="리바로젯",
        canonical_entities=("리바로", "리바로젯"),
    )

    answer = V4Synthesizer(
        _SynthClient("## 핵심 답\n리바로젯 재심사 기간은 2029년까지입니다.")
    ).synthesize(
        _plan(question),
        (nedrug,),
        (),
        state=stale_state,
    )

    core, related = answer.split("## 관련 제품", 1)
    assert "리바로의 재심사 기간을 확인할 수 없습니다" in core
    assert "날짜 부재만으로 기간 경과를 뜻하지는 않습니다" in core
    assert "리바로젯" not in core
    assert "리바로젯" in related
    assert "2029-12-31" in related


def test_r11b_comparison_facts_survive_synthesis_transport_fallback() -> None:
    class FailingSynthClient:
        def complete(
            self,
            _messages: object,
            *,
            budget_s: float,
            max_tokens: int,
        ) -> str:
            del budget_s, max_tokens
            raise TimeoutError("synthetic transport timeout")

    outcome = V4Synthesizer(FailingSynthClient()).synthesize_with_trace(
        _plan("리바로젯과 리피토 매출 비교"),
        (_comparison_result(),),
        (),
    )

    assert outcome.trace["fallback_reason"] == "empty_or_transport_error"
    assert "반대 방향의 변화가 관측됐습니다" in outcome.text
    assert "직접 이동 여부는 현재 자료로 확인되지 않습니다" in outcome.text


def test_r11b_missing_reexamination_fields_do_not_imply_not_subject_or_elapsed() -> None:
    question = "리바로 재심사 기간 알려줘"
    nedrug = SourceResult(
        source="nedrug",
        query=question,
        status="ok",
        payload={
            "items": [
                {
                    "ITEM_NAME": "리바로정2밀리그램",
                    "REEXAM_TARGET": None,
                    "REEXAM_DATE": None,
                }
            ]
        },
    )

    answer = V4Synthesizer(_SynthClient("재심사 기간이 끝났습니다.")).synthesize(
        _plan(question),
        (nedrug,),
        (),
    )

    assert "재심사 기간을 확인할 수 없습니다" in answer
    assert "날짜 부재만으로 기간 경과를 뜻하지는 않습니다" in answer
    assert "대상이 아닙니다" not in answer
    assert "끝났습니다" not in answer


def test_r11b_reexamination_surface_ignores_mismatched_nedrug_evidence() -> None:
    question = "리바로 재심사 기간 알려줘"
    mismatched = SourceResult(
        source="nedrug",
        query=question,
        status="ok",
        payload={
            "items": [
                {
                    "ITEM_NAME": "리바로정2밀리그램",
                    "REEXAM_DATE": "2023-01-01~2029-12-31",
                }
            ]
        },
        evidence=EvidenceEnvelope(
            kind="nedrug",
            entity_match="MISMATCH",
            source_scope="KR",
            time_match="MATCH",
            subject_grain="brand",
        ),
    )
    usable_mart = _comparison_result()
    model_answer = "## 핵심 답\n재심사 기간은 확인되지 않았습니다."

    answer = V4Synthesizer(_SynthClient(model_answer)).synthesize(
        _plan(question),
        (mismatched, usable_mart),
        (),
    )

    assert answer == model_answer
    assert "2029-12-31" not in answer


def test_r11b_reexamination_mixed_missing_records_do_not_preserve_overclaim() -> None:
    question = "리바로 재심사 기간 알려줘"
    nedrug = SourceResult(
        source="nedrug",
        query=question,
        status="ok",
        payload={
            "items": [
                {
                    "ITEM_NAME": "리바로정2밀리그램",
                    "REEXAM_TARGET": None,
                    "REEXAM_DATE": None,
                },
                {
                    "ITEM_NAME": "리바로젯정",
                    "REEXAM_TARGET": None,
                    "REEXAM_DATE": None,
                },
            ]
        },
    )
    model_answer = (
        "리바로 재심사 기간은 끝났고 리바로젯도 재심사 대상이 아닙니다."
    )

    answer = V4Synthesizer(_SynthClient(model_answer)).synthesize(
        _plan(question),
        (nedrug,),
        (),
        state=SessionState(primary_entity="리바로", canonical_entities=("리바로",)),
    )

    assert answer.startswith("## 핵심 답")
    assert "리바로의 재심사 기간을 확인할 수 없습니다" in answer
    assert "끝났" not in answer
    assert "리바로젯도 재심사 대상이 아닙니다" not in answer


def test_r11b_reexamination_repair_preserves_existing_answer_sections() -> None:
    question = "리바로 재심사 기간 알려줘"
    nedrug = SourceResult(
        source="nedrug",
        query=question,
        status="ok",
        payload={
            "items": [
                {
                    "ITEM_NAME": "리바로정2밀리그램",
                    "REEXAM_TARGET": None,
                    "REEXAM_DATE": None,
                }
            ]
        },
    )
    model_answer = """## 핵심 답
재심사 기간이 끝났습니다.

## 근거와 맥락
허가 품목 자료를 확인했습니다.

## 종합 인사이트
추가 공식 자료 확인이 필요합니다.

## 미확인 요소
종료일은 미확인입니다.

## 출처
- 식품의약품안전처
"""

    answer = V4Synthesizer(_SynthClient(model_answer)).synthesize(
        _plan(question),
        (nedrug,),
        (),
    )

    assert "## 근거와 맥락" in answer
    assert "## 종합 인사이트" in answer
    assert "## 미확인 요소" in answer
    assert "허가 품목 자료를 확인했습니다" in answer
    assert "추가 공식 자료 확인이 필요합니다" in answer


def test_r11b_hira_ui_chrome_is_removed_but_notice_body_survives() -> None:
    answer = (
        "< 건강보험심사평가원 보험인정기준 상세내용 인쇄 분류 고시 관련근거 조회수 12 "
        "■ 고시 개정 전체내용 허가사항 및 [일반원칙] 고지혈증 치료제 세부사항 범위 내에서 인정함. "
        "첨부파일 다운로드 자료가 다운되지 않을 경우 담당부서로 연락주시기 바랍니다. "
        "첨부파일명이 한글로 되어있는 경우 다운로드시 확인해 주세요. 닫기"
    )

    repaired, trace = enforce_reason_codes(answer, ())

    assert "보험인정기준 상세내용 인쇄" not in repaired
    assert "첨부파일 다운로드" not in repaired
    assert "닫기" not in repaired
    assert "허가사항 및 [일반원칙] 고지혈증 치료제 세부사항 범위 내에서 인정함" in repaired
    assert trace["INTERNAL_TOKEN_LEAK"] >= 1


def test_r11b_past_clinical_start_uses_separate_current_status_sentence() -> None:
    result = SourceResult(
        source="clinicaltrials",
        query="시험 일정",
        status="ok",
        payload={
            "start_date": "2026-07-01",
            "recruitment_status": "RECRUITING",
        },
    )

    repaired, trace = enforce_reason_codes(
        "이 시험은 2026-07-01 시작 예정이며 모집이 다가옵니다.",
        (result,),
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )

    assert "시험 시작일은 2026-07-01로 시작일이 도래했습니다." in repaired
    assert "현재 모집상태는 RECRUITING입니다." in repaired
    assert "도래했으며" not in repaired
    assert trace["AS_OF_DATE"] == 1
