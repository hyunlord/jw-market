from __future__ import annotations

import hashlib
import json
import threading

import pytest

from jw_chat_agent_poc.service.v4 import adapters as v4_adapters
from jw_chat_agent_poc.service.v4 import llm as v4_llm
from jw_chat_agent_poc.service.v4 import runtime as v4_runtime
from jw_chat_agent_poc.service.v4.contracts import (
    SOURCE_NAMES,
    EvidenceEnvelope,
    PlannerOutput,
    SourceResult,
    ToolQueries,
)
from jw_chat_agent_poc.service.v4.gates import apply_v4_gates
from jw_chat_agent_poc.service.v4.synthesizer import _synthesis_messages


def _plan(question: str) -> PlannerOutput:
    return PlannerOutput(
        resolved_question=question,
        expanded_intents=("시장 원인",),
        answer_sources=("mart",),
        tool_queries=ToolQueries(
            **{source: (f"{question} {source}",) for source in SOURCE_NAMES}
        ),
        linking_plan="single wave",
    )


def _mart_result(*, payload: dict[str, object], grain: str = "brand") -> SourceResult:
    return SourceResult(
        source="mart",
        query="리바로가 최근에 매출이 왜 올랐을까?",
        status="ok",
        payload=payload,
        evidence=EvidenceEnvelope(
            kind="mart",
            entity_match="EXACT",
            source_scope="KR",
            time_match="NOT_REQUESTED",
            eligible_claims=("observed_fact",),
            subject_grain=grain,
        ),
    )


class _StreamResponse:
    def raise_for_status(self) -> None:
        return None

    def iter_lines(self, *, decode_unicode: bool):
        assert decode_unicode is True
        yield 'data: {"model":"genos/190/gemini-3-flash-preview","choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}'
        yield "data: [DONE]"

    def close(self) -> None:
        return None


def test_r9_clients_send_serving_specific_thinking_levels(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, object]] = []

    def fake_post(*_args, **kwargs):
        sent.append(kwargs["json"])
        return _StreamResponse()

    monkeypatch.setattr(v4_llm.requests, "post", fake_post)
    client = v4_llm.GenOSV4Client(
        base_url="https://genos.test/serving/190",
        token=None,
        model="gemini-3-flash-preview",
        timeout_s=5,
        total_budget_s=5,
        thinking_level="LOW",
    )

    assert client.complete([{"role": "user", "content": "plan"}]) == "ok"
    assert sent[0]["google"] == {"thinking_config": {"thinking_level": "LOW"}}
    assert "max_tokens" not in sent[0]
    assert v4_llm.PLANNER_MODEL == "gemini-3-flash-preview"


def test_r9_synthesis_keeps_static_prefix_byte_stable_and_cause_guide_dynamic() -> None:
    result = _mart_result(payload={"calls": []})
    cause = _synthesis_messages(_plan("리바로 매출이 왜 올랐어?"), (result,), ())
    overview = _synthesis_messages(_plan("리바로 요즘 어때?"), (result,), ())

    assert cause[0]["content"] == overview[0]["content"]
    assert "도구로 확인된 원인 후보" not in cause[0]["content"]
    cause_payload = json.loads(cause[1]["content"])
    overview_payload = json.loads(overview[1]["content"])
    assert cause_payload["cause_answer_contract"]["layers"] == [
        "관측",
        "날짜가 확인된 외부 사건",
        "가설",
    ]
    assert cause_payload["cause_answer_contract"]["missing_event_rule"]
    assert any(
        "기인" in phrase
        for phrase in cause_payload["cause_answer_contract"]["forbidden_causal_phrases"]
    )
    assert "cause_answer_contract" not in overview_payload


def test_r9_initial_synthesis_prompt_binds_requested_hira_values() -> None:
    plan = _plan("D693 2024년 입원 환자수와 방문일수 알려줘")
    result = SourceResult(
        source="hira",
        query="D693 2024년 입원 환자수와 방문일수",
        status="ok",
        payload={
            "calls": [
                {
                    "render_data": {
                        "request": {"sickCd": "D693", "year": "2024"},
                        "items": [
                            {
                                "inpatOpat": "입원",
                                "ptntCnt": "1606",
                                "vstDdcnt": "12152",
                            }
                        ],
                    }
                }
            ]
        },
    )

    messages = _synthesis_messages(plan, (result,), ())
    prompt = json.loads(messages[1]["content"])

    assert prompt["required_hira_surface"] == [
        {
            "year": "2024",
            "care_type": "입원",
            "metric": "환자수",
            "value": "1,606명",
        },
        {
            "year": "2024",
            "care_type": "입원",
            "metric": "방문일수",
            "value": "12,152일",
        },
    ]


def test_r9_surface_cleanup_is_sentence_local_and_uses_one_rounding_policy() -> None:
    from jw_chat_agent_poc.service.v4.display import normalize_answer_surface

    dirty = (
        "리바로이 속한 ml_006 시장의 상위 브랜드를 전략 mart에서 조회했습니다. "
        "해당 주장은 현재 근거 자격으로 확인되지 않았습니다.\n\n"
        "리바로 매출은 9,232,262,204.5원이고 시장 규모는 "
        "230,833,352,390.9699원입니다. 처방량은 6,730,094.74 Rx이고 "
        "점유율은 5.3956%입니다.\n\n"
        "매출은 188.4억원에서 210.41억원으로 22.02억원 증가했습니다."
    )

    clean, trace = normalize_answer_surface(dirty)

    assert "전략 mart에서 조회" not in clean
    assert "근거 자격" not in clean
    assert "리바로이" not in clean
    assert "92.32억원" in clean
    assert "약 2,308억원" in clean
    assert "약 673만 Rx" in clean
    assert "5.40%" in clean
    assert "22.01억원 증가" in clean
    assert trace["rounding"] == "ROUND_HALF_UP"
    assert trace["removed_sentences"] == 2


def test_r9_hira_cost_label_does_not_exempt_later_sales_sentence() -> None:
    from jw_chat_agent_poc.service.v4.display import normalize_answer_surface

    dirty = (
        "HIRA 요양급여비용총액은 12,345,678원입니다. "
        "일반 매출은 12,345,678원입니다."
    )

    clean, _trace = normalize_answer_surface(dirty)

    assert "요양급여비용총액은 12,345,678원" in clean
    assert "일반 매출은 0.12억원" in clean


def test_r9_gates_preserve_all_requested_dimensions_with_public_display() -> None:
    payload = {
        "calls": [
            {
                "render_data": {
                    "brand": "리바로젯",
                    "level": level,
                    "measure": "prescription_volume",
                    "value_label": "처방량",
                    "unit_label": "Rx",
                    "query_spec": {
                        "group_by": [level, "period"],
                        "metrics": ["prescription_volume"],
                    },
                    "level_top5_trend_series": [
                        {
                            "name": name,
                            "series": [
                                {"period": "2025-07", "prescription_volume": first},
                                {"period": "2026-06", "prescription_volume": last},
                            ],
                        }
                    ],
                }
            }
            for level, name, first, last in (
                ("specialty", "순환기", 1_821_652.2, 2_157_968.39),
                ("channel", "의원", 2_677_228.15, 3_243_568.08),
            )
        ]
    }
    result = _mart_result(payload=payload)

    gated = apply_v4_gates(
        "리바로젯 진료과별 채널별 처방 추이",
        "조회 결과입니다.",
        (result,),
    )

    assert "진료과별 처방량 추이" in gated.text
    assert "채널별 처방량 추이" in gated.text
    assert "순환기" in gated.text and "의원" in gated.text
    assert "약 216만 Rx" in gated.text
    assert "약 324만 Rx" in gated.text


def test_r9_cause_payload_is_clipped_to_one_common_period() -> None:
    payload = {
        "company_ranking_series": [
            {
                "name": "JW중외제약",
                "from_period": "2025-07",
                "to_period": "2026-06",
                "series": [
                    {"period": "2025-07", "value_억원": 180.00, "ms_pct": 8.00},
                    {"period": "2025-09", "value_억원": 188.40, "ms_pct": 8.20},
                    {"period": "2026-01", "value_억원": 199.00, "ms_pct": 8.70},
                    {"period": "2026-06", "value_억원": 210.41, "ms_pct": 9.10},
                ],
            }
        ],
        "analysis_level_trend": [
            {
                "name": "PTV",
                "series": [
                    {"period": "2025-07", "value_억원": 100.00},
                    {"period": "2025-09", "value_억원": 110.00},
                    {"period": "2026-06", "value_억원": 130.00},
                ],
            }
        ],
        "customer_competition": [
            {
                "name": "의원",
                "series": [
                    {"period": "2025-07", "value_억원": 1_100.00},
                    {"period": "2025-09", "value_억원": 1_200.00},
                    {"period": "2026-06", "value_억원": 1_300.00},
                ],
            }
        ],
        "ei_ms": {
            "brand_value_series_10pt": [
                {"period": "2025-09", "value_억원": 89.29},
                {"period": "2026-06", "value_억원": 85.87},
            ]
        },
    }

    aligned, anchor = v4_adapters.align_cause_periods(payload)

    assert anchor == {"period_start": "2025-09", "period_end": "2026-06"}
    for path in ("company_ranking_series", "analysis_level_trend", "customer_competition"):
        assert [point["period"] for point in aligned[path][0]["series"]] == [
            "2025-09",
            "2026-06",
        ]
    assert aligned["company_ranking_series"][0]["from_period"] == "2025-09"
    assert aligned["company_ranking_series"][0]["value_delta_억원"] == 22.01


def test_r9_cause_alignment_requires_two_shared_observed_months() -> None:
    payload = {
        "company_ranking_series": [
            {
                "from_period": "2025-07",
                "to_period": "2026-06",
                "value_delta_억원": 30.0,
                "series": [
                    {"period": "2025-07", "value_억원": 180.0},
                    {"period": "2026-06", "value_억원": 210.0},
                ],
            }
        ],
        "brand_value_series": [
            {"period": "2025-09", "value_억원": 80.0},
            {"period": "2026-06", "value_억원": 90.0},
        ],
    }

    aligned, anchor = v4_adapters.align_cause_periods(payload)

    assert anchor is None
    assert aligned == payload


def test_r9_evidence_envelope_accepts_trace_only_grain_fields() -> None:
    envelope = EvidenceEnvelope(
        kind="mart",
        entity_match="EXACT",
        source_scope="KR",
        time_match="MATCH",
        subject_grain="brand",
        period_start="2025-09",
        period_end="2026-06",
        parent_entity="ml_006",
        eligible_attributions=("observed_association",),
    )

    assert envelope.subject_grain == "brand"
    assert envelope.period_start == "2025-09"
    assert envelope.eligible_attributions == ("observed_association",)


def test_r9_shadow_classifies_facts_without_mutating_answer() -> None:
    from jw_chat_agent_poc.service.v4.shadow import build_grounding_shadow

    result = _mart_result(
        payload={
            "brand": "리바로",
            "period": "2026-06",
            "sales_억원": 85.87,
        },
        grain="brand",
    )
    answer = (
        "리바로 매출은 85.87억원입니다. [출처: 내부 데이터마트] "
        "시장 매출은 85.87억원입니다. 확인되지 않은 수치는 999.99입니다."
    )
    before = hashlib.sha256(answer.encode()).hexdigest()

    shadow = build_grounding_shadow(answer, (result,))

    assert shadow["answer_sha256"] == before
    assert shadow["counts"]["grounded"] >= 1
    assert shadow["counts"]["ungrounded"] >= 1
    assert shadow["counts"]["grain_mismatch"] >= 1
    assert answer == (
        "리바로 매출은 85.87억원입니다. [출처: 내부 데이터마트] "
        "시장 매출은 85.87억원입니다. 확인되지 않은 수치는 999.99입니다."
    )


def test_r9_shadow_normalizes_public_display_units() -> None:
    from jw_chat_agent_poc.service.v4.shadow import build_grounding_shadow

    result = _mart_result(
        payload={
            "market_size_억원": 2308.33,
            "sales_krw": 9_232_262_204.5,
            "prescription_volume": 6_730_094.74,
        },
        grain="market",
    )
    answer = (
        "시장 규모는 약 2,308억원이고 매출은 92.32억원입니다. "
        "처방량은 약 673만 Rx입니다."
    )

    shadow = build_grounding_shadow(answer, (result,))

    assert shadow["counts"]["grounded"] == 3
    assert shadow["counts"]["ungrounded"] == 0


def test_r9_shadow_executor_saturation_is_fail_open_for_answer_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        v4_runtime,
        "_GROUNDING_SLOTS",
        threading.BoundedSemaphore(value=0),
    )

    assert v4_runtime._submit_grounding_ledger(()) is None
