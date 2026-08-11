from __future__ import annotations

import inspect
import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import requests
from fastapi.testclient import TestClient
from pydantic import ValidationError

from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.v4.contracts import (
    SOURCE_NAMES,
    Citation,
    EvidenceEnvelope,
    PlannerOutput,
    SourceResult,
    ToolQueries,
    V4Answer,
)
from jw_chat_agent_poc.service.v4.executor import ParallelSourceExecutor
from jw_chat_agent_poc.service.v4.gates import apply_v4_gates
from jw_chat_agent_poc.service.v4.llm import planner_client, synthesizer_client
from jw_chat_agent_poc.service.v4.planner import V4Planner
from jw_chat_agent_poc.service.v4.runtime import (
    V4Runtime,
    _is_prior_result_reference,
    _preserve_period_in_answer_queries,
)
from jw_chat_agent_poc.service.v4 import adapters as v4_adapters
from jw_chat_agent_poc.service.v4 import llm as v4_llm
from jw_chat_agent_poc.service.v4 import synthesizer as v4_synthesizer
from jw_chat_agent_poc.service.v4.synthesizer import (
    V4Synthesizer,
    _INTERNAL_SURFACE_RE,
    _evidence_fallback,
)


def _plan(**queries: tuple[str, ...]) -> PlannerOutput:
    values = {name: (f"{name} query",) for name in SOURCE_NAMES}
    values.update(queries)
    return PlannerOutput(
        resolved_question="리바로 요즘 어때",
        expanded_intents=("시장", "허가", "임상"),
        tool_queries=ToolQueries(**values),
        linking_plan="first hop is sufficient",
        needs_second_hop=False,
    )


def test_planner_output_requires_all_seven_nonempty_query_lists() -> None:
    payload = {
        "resolved_question": "질문",
        "expanded_intents": ["시장"],
        "tool_queries": {name: [name] for name in SOURCE_NAMES if name != "patent"},
        "linking_plan": "none",
        "needs_second_hop": False,
    }

    with pytest.raises(ValidationError):
        PlannerOutput.model_validate(payload)


def test_mart_adapter_does_not_reenter_legacy_agent_loop() -> None:
    source = inspect.getsource(v4_adapters)

    assert "_answer_direct_agent_loop" not in source
    assert "general_view.answer" in source
    assert "layer.brand_metric" in source


def test_v4_adapter_extracts_identifiers_and_source_specific_queries() -> None:
    assert v4_adapters._nct_id("NCT05151731 선정제외기준 clinical trials") == "NCT05151731"
    assert v4_adapters._hira_code("D69.3 상병 환자수 최근 5년") == "D693"
    assert v4_adapters._ingredient_query("스타틴 계열 최근 안전성 이슈") == "Pitavastatin"
    assert v4_adapters._clinical_query("당뇨망막병증 치료제 최근 임상 동향") == (
        "diabetic retinopathy",
        "condition",
    )


def test_v4_hira_patient_query_wins_over_planner_reimbursement_wording() -> None:
    assert v4_adapters._hira_query_kind(
        "H360 국내 급여 및 환자 통계 최근 5년"
    ) == "patient"
    assert v4_adapters._hira_query_kind("아일리아 급여기준") == "reimbursement"


def test_v4_mart_relevance_rejects_external_only_questions() -> None:
    assert v4_adapters._mart_relevant("리바로 요즘 어때") is True
    assert v4_adapters._mart_relevant("리바로 매출 알려줘") is True
    assert v4_adapters._mart_relevant("리바로 효능효과") is False
    assert v4_adapters._mart_relevant("리바로 특허 언제 만료돼") is False


def test_v4_mart_adapter_always_returns_source_result(monkeypatch) -> None:
    from jw_chat_agent_poc.agent_loop import factory
    from jw_chat_agent_poc.service import general_view_routing

    class Resolver:
        def resolve(self, _query, *, allow_default):
            assert allow_default is False
            return SimpleNamespace(canonical_brand="리바로", molecule_en=("Pitavastatin",))

    class QueryLayer:
        def brand_metric(self, brand, metric, period, *, history_points=10):
            return {"source": "UBIST", "brand": brand, "metric": metric, "period": period}

        def top_brands(self, brand, *, limit, metric):
            return {"source": "UBIST", "brand": brand, "limit": limit, "metric": metric}

    class GeneralView:
        def route(self, _query):
            return general_view_routing.GeneralRoute.EXISTING

    dependencies = SimpleNamespace(
        external=SimpleNamespace(),
        resolver=Resolver(),
        query_layer=QueryLayer(),
    )
    monkeypatch.setattr(factory, "build_chat_agent_dependencies", lambda **_kwargs: dependencies)
    monkeypatch.setattr(
        general_view_routing.GeneralViewService,
        "from_env",
        lambda _resolver: GeneralView(),
    )

    result = v4_adapters.build_source_adapters()["mart"]("리바로 매출 알려줘")

    assert isinstance(result, SourceResult)
    assert result.status == "ok"
    assert result.payload["calls"][0]["metric"] == "sales"


@pytest.mark.parametrize(
    ("query", "expected"),
    (
        ("아일리아주 급여기준", "아일리아"),
        ("애플리버셉트(Aflibercept) 요양급여 적용기준 및 방법", "아일리아"),
    ),
)
def test_v4_reimbursement_subject_normalizes_brand_and_ingredient_queries(
    query: str,
    expected: str,
) -> None:
    assert v4_adapters._reimbursement_subject(query) == expected


def test_v4_fallback_uses_verified_summaries_instead_of_raw_json() -> None:
    results = (
        SourceResult(
            source="clinicaltrials",
            query="NCT05151731",
            status="ok",
            payload={
                "calls": [
                    {
                        "summary_text": "NCT05151731은 2상 무작위배정 이중눈가림 시험입니다.",
                        "render_data": {"secret_internal": "must-not-be-dumped"},
                    }
                ]
            },
        ),
    )

    answer = _evidence_fallback(results)

    assert "2상 무작위배정 이중눈가림" in answer
    assert "secret_internal" not in answer


def test_v4_fallback_writes_hira_patient_counts_as_user_facing_prose() -> None:
    results = (
        SourceResult(
            source="hira",
            query="D693 상병 환자수 최근 5년",
            status="ok",
            payload={
                "calls": [
                    {
                        "render_data": {
                            "items": [
                                {
                                    "year": "2024",
                                    "inpatOpat": "입원",
                                    "ptntCnt": 1606,
                                }
                            ]
                        }
                    }
                ]
            },
        ),
    )

    answer = _evidence_fallback(results)

    assert "2024년 입원 환자수는 1,606명" in answer
    assert "ptntCnt" not in answer


def test_v4_fallback_joins_hira_name_and_split_year_rows() -> None:
    result = SourceResult(
        source="hira",
        query="D693 상병 환자수 최근 5년",
        status="ok",
        payload={
            "calls": [
                {
                    "render_data": {
                        "items": [
                            {"sickCd": "D693", "sickNm": "특발성 혈소판감소성 자반"}
                        ]
                    }
                },
                {
                    "render_data": {
                        "request": {"sickCd": "D693", "year": "2024"},
                        "items": [
                            {"inpatOpat": "입원", "ptntCnt": "1606"},
                            {"inpatOpat": "외래", "ptntCnt": "9231"},
                        ],
                    }
                },
            ]
        },
    )

    answer = _evidence_fallback((result,))

    assert (
        "D693(특발성 혈소판감소성 자반) 2024년 입원 환자수는 "
        "1,606명(청구 실인원), 외래 환자수는 9,231명(청구 실인원)"
    ) in answer
    assert all(field not in answer for field in ("sickCd", "ptntCnt", "value"))


def test_v4_fallback_preserves_all_hira_additive_fields() -> None:
    result = SourceResult(
        source="hira",
        query="D693 상병 환자수 최근 5년과 진료비, 방문일수 알려줘",
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
                                "rvdInsupBrdnAmt": "7193144",
                                "rvdRpeTamtAmt": "8697604",
                                "specCnt": "2547",
                                "vstDdcnt": "12879",
                            }
                        ],
                    }
                }
            ]
        },
    )

    answer = _evidence_fallback((result,))

    assert "환자수는 1,606명(청구 실인원)" in answer
    assert "보험자부담금 7,193,144,000원" in answer
    assert "요양급여비용총액 8,697,604,000원" in answer
    assert "명세서건수 2,547건" in answer
    assert "내원일수 12,879일" in answer
    assert all(
        field not in answer
        for field in ("ptntCnt", "rvdInsupBrdnAmt", "rvdRpeTamtAmt", "specCnt", "vstDdcnt")
    )


def test_v4_fallback_never_lists_unknown_internal_field_names() -> None:
    result = SourceResult(
        source="openfda",
        query="리바로 안전성",
        status="ok",
        payload={"calls": [{"render_data": {"value": 123, "secretField": "raw"}}]},
    )

    answer = _evidence_fallback((result,))

    assert "value" not in answer
    assert "secretField" not in answer
    assert "FDA" in answer


def test_v4_fallback_uses_mart_display_summary_not_raw_won_value() -> None:
    result = SourceResult(
        source="mart",
        query="리바로 매출",
        status="ok",
        payload={
            "calls": [
                {
                    "summary_text": "리바로 매출은 85.87억원입니다.",
                    "render_data": {"value": 8587458961.25, "sales_억원": 85.87},
                }
            ]
        },
    )

    answer = _evidence_fallback((result,))

    assert "85.87억원" in answer
    assert "8587458961.25" not in answer


def test_v4_synthesis_failure_prefers_mart_history_over_snapshot_summary() -> None:
    class Client:
        def complete_detailed(self, _messages, *, budget_s=None, max_tokens=None):
            raise TimeoutError("synthesis deadline")

    plan = _plan().model_copy(
        update={"resolved_question": "리바로젯 매출은 어떻게 변해왔어?"}
    )
    result = SourceResult(
        source="mart",
        query="리바로젯 매출 추이",
        status="ok",
        payload={
            "calls": [
                {
                    "summary_text": "리바로젯 2026-06 매출은 124.54억원입니다.",
                    "render_data": {
                        "brand": "리바로젯",
                        "brand_value_series_10pt": [
                            {"period": "2022-12", "value_억원": "91.20"},
                            {"period": "2023-12", "value_억원": "101.35"},
                            {"period": "2024-12", "value_억원": "110.08"},
                            {"period": "2025-12", "value_억원": "119.77"},
                            {"period": "2026-06", "value_억원": "124.54"},
                        ],
                    },
                }
            ]
        },
    )

    outcome = V4Synthesizer(Client()).synthesize_with_trace(
        plan, (result,), (), budget_s=24.0
    )

    assert (
        "리바로젯 매출은 2022년 12월 91.20억원에서 "
        "2026년 6월 124.54억원으로 4년간 증가했습니다."
    ) in outcome.text
    assert (
        "연도별: 2022년 12월 91.20억원 · 2023년 12월 101.35억원 · "
        "2024년 12월 110.08억원 · 2025년 12월 119.77억원 · "
        "2026년 6월 124.54억원"
    ) in outcome.text
    assert "리바로젯 2026-06 매출은" not in outcome.text
    assert outcome.trace["fallback_reason"] == "empty_or_transport_error"


def test_v4_synthesizer_sends_detail_rows_in_question_first_layout() -> None:
    class Client:
        def __init__(self) -> None:
            self.messages = None

        def complete(self, messages, *, budget_s=None, max_tokens=None) -> str:
            self.messages = messages
            assert budget_s == 15.0
            assert max_tokens == 8192
            return "2024년 D693 외래 환자수는 12,345명입니다. [출처: HIRA]"

    client = Client()
    result = SourceResult(
        source="hira",
        query="D693 상병별 환자수 최근 5년",
        status="ok",
        payload={
            "calls": [
                {
                    "tool": "hira_disease_hospitalization_outpatient_stats",
                    "summary_text": "hira MCP returned totalCount=4",
                    "render_data": {
                        "items": [
                            {"year": "2024", "inpatient": "321", "outpatient": "12,345"}
                        ]
                    },
                }
            ]
        },
    )

    answer = V4Synthesizer(client).synthesize(_plan(), (result,), (), budget_s=15.0)

    prompt = client.messages[1]["content"]
    assert "2024" in prompt
    assert "12,345" in prompt
    assert prompt.index("external_evidence") < prompt.index("user_question")
    assert "12,345명" in answer


def test_v4_synthesizer_retries_internal_surface_once() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, _messages, *, budget_s=None, max_tokens=None) -> str:
            self.calls += 1
            if self.calls == 1:
                return "hira_disease_name_code MCP returned totalCount=1"
            return "D693 환자 통계는 HIRA 근거에서 확인되었습니다. [출처: HIRA]"

    result = SourceResult(
        source="hira",
        query="D693 환자수",
        status="ok",
        payload={"calls": [{"render_data": {"items": [{"year": "2024", "patients": "10"}]}}]},
    )

    answer = V4Synthesizer(Client()).synthesize(_plan(), (result,), (), budget_s=15.0)

    assert "MCP returned" not in answer
    assert "totalCount" not in answer
    assert "hira_disease_name_code" not in answer


def test_v4_synthesizer_replaces_repeated_internal_block_and_adds_hira_footnote() -> None:
    class Client:
        def complete(self, _messages, *, budget_s=None, max_tokens=None) -> str:
            return "설명입니다.\n\nhira_disease_name_code MCP returned totalCount=1"

    result = SourceResult(
        source="hira",
        query="D693 환자수",
        status="ok",
        payload={"calls": [{"render_data": {"items": [{"year": "2024", "patients": "10"}]}}]},
    )

    answer = V4Synthesizer(Client()).synthesize(_plan(), (result,), (), budget_s=15.0)

    assert "MCP returned" not in answer
    assert "totalCount" not in answer
    assert "hira_disease_name_code" not in answer
    assert "주상병 기준 청구 실인원" in answer


def test_v4_synthesizer_retries_once_when_requested_hira_value_is_missing() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = 0
            self.messages = []

        def complete(self, messages, *, budget_s=None, max_tokens=None) -> str:
            self.calls += 1
            self.messages.append(messages)
            if self.calls == 1:
                return "D693 환자 통계가 확인되었습니다. [출처: HIRA]"
            return "D693 2024년 입원 환자수는 1,606명입니다. [출처: HIRA]"

    plan = _plan(hira=("D693 환자수 2024년",)).model_copy(
        update={"resolved_question": "D693 2024년 환자수 알려줘"}
    )
    result = SourceResult(
        source="hira",
        query="D693 환자수 2024년",
        status="ok",
        payload={
            "calls": [
                {
                    "render_data": {
                        "request": {"sickCd": "D693", "year": "2024"},
                        "items": [{"inpatOpat": "입원", "ptntCnt": "1606"}],
                    }
                }
            ]
        },
    )
    client = Client()

    outcome = V4Synthesizer(client).synthesize_with_trace(
        plan, (result,), (), budget_s=30.0
    )

    assert client.calls == 2
    assert "입원 환자수는 1,606명" in outcome.text
    assert outcome.trace["requested_hira_surface_retry"]["attempted"] is True
    assert outcome.trace["requested_hira_surface_retry"]["missing_after"] == []
    retry_prompt = client.messages[1][-1]["content"]
    assert '"metric": "환자수"' in retry_prompt
    assert '"value": "1,606명"' in retry_prompt


def test_v4_synthesizer_does_not_accept_claim_count_as_patient_count_on_retry() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, _messages, *, budget_s=None, max_tokens=None) -> str:
            self.calls += 1
            if self.calls == 1:
                return "H360 환자 통계가 확인되었습니다. [출처: HIRA]"
            return "H360 2024년 입원 환자수는 2,901명입니다. [출처: HIRA]"

    plan = _plan(hira=("H360 환자수 2024년",)).model_copy(
        update={"resolved_question": "H360 2024년 환자수 알려줘"}
    )
    result = SourceResult(
        source="hira",
        query="H360 환자수 2024년",
        status="ok",
        payload={
            "calls": [
                {
                    "render_data": {
                        "request": {"sickCd": "H360", "year": "2024"},
                        "items": [
                            {
                                "inpatOpat": "입원",
                                "ptntCnt": "2402",
                                "specCnt": "2901",
                            }
                        ],
                    }
                }
            ]
        },
    )
    client = Client()

    outcome = V4Synthesizer(client).synthesize_with_trace(
        plan, (result,), (), budget_s=30.0
    )

    assert client.calls == 2
    assert outcome.trace["requested_hira_surface_retry"]["missing_after"] == [
        {
            "year": "2024",
            "care_type": "입원",
            "metric": "환자수",
            "value": "2,402명",
        }
    ]


@pytest.mark.parametrize(
    "leak",
    (
        "ClinicalTrials MCP에서 받은 결과입니다.",
        "MCP backend returned 결과입니다.",
        "SICK_CD=D693",
        "ITEM_SEQ: 200101234",
        "12453782153.7원",
    ),
)
def test_v4_surface_detects_broad_log_field_and_raw_won_patterns(leak: str) -> None:
    assert _INTERNAL_SURFACE_RE.search(leak)


def test_v4_synthesis_prompt_preserves_source_payload_verbatim() -> None:
    result = SourceResult(
        source="nedrug",
        query="아일리아 급여기준",
        status="ok",
        payload={
            "calls": [
                {
                    "render_data": {
                        "ITEM_SEQ": "200101234",
                        "ENTP_SEQ": "vendor-record",
                        "PRDLST_STDR_CODE": "raw-code",
                        "sickCd": "D693",
                        "ptntCnt": "9231",
                        "efficacy": "당뇨병성 황반부종 환자의 시력 개선",
                        "notice": "다운로드 후 담당부서로 연락주시기 바랍니다.",
                    }
                }
            ]
        },
    )

    messages = v4_synthesizer._synthesis_messages(_plan(), (result,), ())
    serialized = messages[-1]["content"]
    system = messages[0]["content"]

    assert '"ITEM_SEQ": "200101234"' in serialized
    assert '"ENTP_SEQ": "vendor-record"' in serialized
    assert '"PRDLST_STDR_CODE": "raw-code"' in serialized
    assert '"sickCd": "D693"' in serialized
    assert '"ptntCnt": "9231"' in serialized
    assert "다운로드 후 담당부서로 연락주시기 바랍니다." in serialized
    assert "## 핵심 답" in system
    assert "한 문단은 최대 4문장" in system
    assert "다운로드 안내문" in system


def test_v4_synthesis_preserves_reexamination_source_fields_verbatim() -> None:
    result = SourceResult(
        source="nedrug",
        query="리바로젯 재심사 종료일",
        status="ok",
        payload={
            "calls": [
                {
                    "status": "live",
                    "render_data": {
                        "items": [
                            {
                                "ITEM_NAME": "리바로젯정2/10밀리그램",
                                "REEXAM_DATE": "2021-07-28~2027-07-27",
                                "REEXAM_TARGET": "재심사대상(6년)",
                            }
                        ]
                    },
                }
            ]
        },
    )

    serialized = v4_synthesizer._synthesis_messages(_plan(), (result,), ())[-1]["content"]

    assert '"ITEM_NAME": "리바로젯정2/10밀리그램"' in serialized
    assert '"REEXAM_DATE": "2021-07-28~2027-07-27"' in serialized
    assert '"REEXAM_TARGET": "재심사대상(6년)"' in serialized


def test_v4_synthesis_labels_hira_patient_fields_and_units_in_korean() -> None:
    result = SourceResult(
        source="hira",
        query="D693 상병 환자수",
        status="ok",
        payload={
            "calls": [
                {
                    "render_data": {
                        "items": [
                            {
                                "sickCd": "D693",
                                "ptntCnt": "1606",
                                "specCnt": "3301",
                                "vstDdcnt": "12152",
                                "rvdRpeTamtAmt": "9986518000",
                            }
                        ]
                    }
                }
            ]
        },
    )

    messages = v4_synthesizer._synthesis_messages(_plan(), (result,), ())
    serialized = messages[-1]["content"]
    system = messages[0]["content"]

    assert '"sickCd": "D693"' in serialized
    assert '"ptntCnt": "1606"' in serialized
    assert '"specCnt": "3301"' in serialized
    assert '"vstDdcnt": "12152"' in serialized
    assert '"rvdRpeTamtAmt": "9986518000"' in serialized
    assert '"ptntCnt": "환자수(명)"' in serialized
    assert '"specCnt": "명세서건수(건)"' in serialized
    assert '"vstDdcnt": "방문일수(일)"' in serialized
    assert '"rvdRpeTamtAmt": "요양급여비용총액(원)"' in serialized
    assert "환자수는 `환자수(명)` 값만 사용" in system


def test_v4_synthesis_does_not_truncate_source_text() -> None:
    source_text = "허가사항 본문 " * 200
    result = SourceResult(
        source="nedrug",
        query="아일리아 효능효과",
        status="ok",
        payload={"calls": [{"render_data": {"efficacy": source_text}}]},
    )

    messages = v4_synthesizer._synthesis_messages(_plan(), (result,), ())
    serialized = messages[-1]["content"]

    assert source_text in serialized
    assert "[excerpt]" not in serialized


def test_v4_synthesizer_traces_raw_payload_character_count() -> None:
    class Client:
        def complete(self, _messages, *, budget_s=None, max_tokens=None) -> str:
            return "## 핵심 답\n확인했습니다."

    payload = {"calls": [{"render_data": {"notice": "다운로드 안내문 원문"}}]}
    result = SourceResult(source="nedrug", query="아일리아 급여기준", status="ok", payload=payload)

    outcome = V4Synthesizer(Client()).synthesize_with_trace(_plan(), (result,), ())

    assert outcome.trace["raw_payload_chars"] == len(
        json.dumps(payload, ensure_ascii=False, default=str)
    )


def test_v4_hira_aggregates_every_additive_field_by_patient_type() -> None:
    raw_items = [
        {
            "inpatOpat": "입원",
            "sex": "남",
            "sickCd": "D693",
            "sickNm": "특발성 혈소판감소성 자반",
            "ptntCnt": "600",
            "rvdInsupBrdnAmt": "1000",
            "rvdRpeTamtAmt": "2000",
            "specCnt": "30",
            "vstDdcnt": "40",
        },
        {
            "inpatOpat": "입원",
            "sex": "여",
            "sickCd": "D693",
            "sickNm": "특발성 혈소판감소성 자반",
            "ptntCnt": "1006",
            "rvdInsupBrdnAmt": "3000",
            "rvdRpeTamtAmt": "4000",
            "specCnt": "50",
            "vstDdcnt": "60",
        },
        {
            "inpatOpat": "외래",
            "sex": "남",
            "sickCd": "D693",
            "sickNm": "특발성 혈소판감소성 자반",
            "ptntCnt": "4000",
            "rvdInsupBrdnAmt": "5000",
            "rvdRpeTamtAmt": "6000",
            "specCnt": "70",
            "vstDdcnt": "80",
        },
        {
            "inpatOpat": "외래",
            "sex": "여",
            "sickCd": "D693",
            "sickNm": "특발성 혈소판감소성 자반",
            "ptntCnt": "5231",
            "rvdInsupBrdnAmt": "7000",
            "rvdRpeTamtAmt": "8000",
            "specCnt": "90",
            "vstDdcnt": "100",
        },
    ]

    aggregated = v4_adapters._aggregate_hira_items(raw_items)

    assert [item["inpatOpat"] for item in aggregated] == ["입원", "외래"]
    assert aggregated[0] | {"sexBreakdown": None} == {
        "inpatOpat": "입원",
        "sex": None,
        "sickCd": "D693",
        "sickNm": "특발성 혈소판감소성 자반",
        "ptntCnt": "1606",
        "rvdInsupBrdnAmt": "4000000",
        "rvdRpeTamtAmt": "6000000",
        "specCnt": "80",
        "vstDdcnt": "100",
        "units": {
            "ptntCnt": "명",
            "rvdInsupBrdnAmt": "원",
            "rvdRpeTamtAmt": "원",
            "specCnt": "건",
            "vstDdcnt": "일",
        },
        "sexBreakdown": None,
    }
    assert aggregated[1]["ptntCnt"] == "9231"
    assert aggregated[1]["rvdInsupBrdnAmt"] == "12000000"
    assert aggregated[1]["rvdRpeTamtAmt"] == "14000000"
    assert aggregated[1]["specCnt"] == "160"
    assert aggregated[1]["vstDdcnt"] == "180"
    assert all(item["sex"] is None for item in aggregated)
    assert not any("total" in key.casefold() for item in aggregated for key in item)


def test_v4_hira_prefers_embedded_raw_rows_over_lossy_public_aggregation() -> None:
    raw_items = [
        {"inpatOpat": "입원", "sex": "남", "ptntCnt": "2", "specCnt": "3"},
        {"inpatOpat": "입원", "sex": "여", "ptntCnt": "5", "specCnt": "7"},
    ]
    render_data = {
        "items": [{"inpatOpat": "입원", "ptntCnt": "7", "specCnt": "3"}],
        "mcp": {"content_text": json.dumps(raw_items, ensure_ascii=False)},
    }

    normalized = v4_adapters._normalize_hira_render_data(render_data)

    assert normalized["items"][0]["ptntCnt"] == "7"
    assert normalized["items"][0]["specCnt"] == "10"


def test_v4_synthesizer_labels_scope_and_excludes_web_without_body() -> None:
    class Client:
        def __init__(self) -> None:
            self.prompt = ""

        def complete(self, messages, *, budget_s=None, max_tokens=None) -> str:
            self.prompt = messages[1]["content"]
            return "확인된 근거로 답변합니다."

    client = Client()
    web = SourceResult(
        source="web",
        query="최근 개정",
        status="ok",
        payload={"calls": [{"render_data": {"title": "로그인", "content": "짧음"}}]},
    )
    fda = SourceResult(
        source="openfda",
        query="리바로 안전성",
        status="ok",
        payload={"calls": [{"render_data": {"items": [{"drug": "Pitavastatin"}]}}]},
    )

    V4Synthesizer(client).synthesize(_plan(), (web, fda), (), budget_s=15.0)

    assert '"source_scope": "US"' in client.prompt
    assert '"source": "web"' not in client.prompt


def test_parallel_executor_calls_every_source_concurrently_and_reuses_session_cache() -> None:
    calls: list[tuple[str, str]] = []

    def adapter(source: str, query: str) -> SourceResult:
        calls.append((source, query))
        time.sleep(0.04)
        return SourceResult(
            source=source,
            query=query,
            status="ok",
            payload={"source": source, "query": query},
            citations=(
                Citation(
                    source=source,
                    query=query,
                    url=f"https://example.test/{source}",
                    retrieved_at=datetime.now(UTC),
                    used=False,
                ),
            ),
        )

    executor = ParallelSourceExecutor(
        adapters={name: (lambda query, source=name: adapter(source, query)) for name in SOURCE_NAMES},
        per_tool_timeout_s=1.0,
        total_timeout_s=2.0,
    )
    started = time.monotonic()
    first = executor.execute(_plan(), session_id="session-a")
    elapsed = time.monotonic() - started
    second = executor.execute(_plan(), session_id="session-a")

    assert elapsed < 0.18
    assert {item.source for item in first} == set(SOURCE_NAMES)
    assert len(calls) == 7
    assert all(item.cache_hit for item in second)
    assert len(calls) == 7


def test_parallel_executor_source_filter_runs_only_selected_source() -> None:
    calls: list[str] = []

    def adapter(source: str, query: str) -> SourceResult:
        calls.append(source)
        return SourceResult(
            source=source,
            query=query,
            status="ok",
            payload={"source": source},
        )

    executor = ParallelSourceExecutor(
        adapters={
            name: (lambda query, source=name: adapter(source, query))
            for name in SOURCE_NAMES
        },
        per_tool_timeout_s=1.0,
        total_timeout_s=2.0,
    )

    outcome = executor.execute_with_trace(
        _plan(),
        session_id="session-web-only",
        source_filter=("web",),
    )

    assert calls == ["web"]
    assert [result.source for result in outcome.results] == ["web"]


def test_parallel_executor_starts_each_source_before_extra_queries() -> None:
    calls: list[str] = []

    def adapter(source: str, query: str) -> SourceResult:
        calls.append(source)
        time.sleep(0.04)
        return SourceResult(source=source, query=query, status="ok", payload={"value": source})

    executor = ParallelSourceExecutor(
        adapters={name: (lambda query, source=name: adapter(source, query)) for name in SOURCE_NAMES},
        per_tool_timeout_s=0.08,
        total_timeout_s=0.2,
    )
    results = executor.execute(
        _plan(mart=tuple(f"mart query {index}" for index in range(8))),
        session_id="session-round-robin",
    )

    assert set(calls[:7]) == set(SOURCE_NAMES)
    assert {item.source for item in results if item.status == "ok"} >= set(SOURCE_NAMES)


def test_parallel_executor_marks_timeout_without_blocking_other_sources() -> None:
    def slow(query: str) -> SourceResult:
        time.sleep(0.2)
        return SourceResult(source="hira", query=query, status="ok", payload={})

    adapters = {
        name: (
            slow
            if name == "hira"
            else lambda query, source=name: SourceResult(
                source=source,
                query=query,
                status="ok",
                payload={"value": source},
            )
        )
        for name in SOURCE_NAMES
    }
    executor = ParallelSourceExecutor(
        adapters=adapters,
        per_tool_timeout_s=0.03,
        total_timeout_s=0.15,
    )

    results = executor.execute(_plan(), session_id="session-timeout")

    hira = next(item for item in results if item.source == "hira")
    assert hira.status == "timeout"
    assert hira.notice == "응답 지연으로 미포함"
    assert sum(item.status == "ok" for item in results) == 6

    gated = apply_v4_gates("질문", "확인된 답변", results)
    assert "응답 지연으로 미포함" not in gated.text
    assert gated.trace["delayed_sources"] == ["hira"]


def test_invalid_planner_json_falls_back_to_all_seven_sources() -> None:
    class InvalidClient:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, _messages, *, budget_s=None) -> str:
            self.calls += 1
            return "not-json"

    client = InvalidClient()
    output = V4Planner(client).plan("리바로 요즘 어때", ())

    assert client.calls == 2
    assert {name for name, _queries in output.tool_queries.items()} == set(SOURCE_NAMES)
    assert all(queries for _name, queries in output.tool_queries.items())


def test_exact_nct_anchor_removes_invented_first_wave_entities() -> None:
    contaminated = _plan().model_copy(
        update={
            "resolved_question": "NCT05151731 시험 디자인",
            "tool_queries": ToolQueries(
                **{
                    source: ("ABBV-123 리바로 NCT05151731",)
                    for source in SOURCE_NAMES
                }
            ),
            "needs_second_hop": False,
        }
    )

    class Client:
        serving_id = "190"

        def complete(self, _messages, *, budget_s=None) -> str:
            return contaminated.model_dump_json()

    plan = V4Planner(Client()).plan("NCT05151731 시험 디자인", ())
    queries = tuple(query for _, values in plan.tool_queries.items() for query in values)

    assert plan.answer_sources == ("clinicaltrials",)
    assert plan.needs_second_hop is True
    assert all("NCT05151731" in query for query in queries)
    assert all("ABBV-123" not in query and "리바로" not in query for query in queries)


def test_exact_nct_link_uses_only_first_result_canonical_entity() -> None:
    contaminated = _plan().model_copy(
        update={
            "resolved_question": "NCT05151731 선정제외기준",
            "tool_queries": ToolQueries(
                **{source: ("ABBV-123 invented disease",) for source in SOURCE_NAMES}
            ),
            "needs_second_hop": False,
        }
    )

    class Client:
        serving_id = "190"

        def complete(self, _messages, *, budget_s=None) -> str:
            return contaminated.model_dump_json()

    planner = V4Planner(Client())
    first = planner.plan("NCT05151731 선정제외기준", ())
    linked = planner.link(
        first,
        (
            SourceResult(
                source="clinicaltrials",
                query="NCT05151731",
                status="ok",
                payload={
                    "protocolSection": {
                        "armsInterventionsModule": {
                            "interventions": [{"name": "Vamikibart"}]
                        }
                    }
                },
            ),
        ),
        (),
    )

    assert linked is not None
    queries = tuple(query for _, values in linked.tool_queries.items() for query in values)
    assert all("ABBV-123" not in query and "invented disease" not in query for query in queries)
    assert any("Vamikibart" in query for query in queries)


def test_exact_product_code_lock_removes_planner_invented_entities() -> None:
    contaminated = _plan().model_copy(
        update={
            "resolved_question": "품목기준코드 201306324 허가사항",
            "tool_queries": ToolQueries(
                **{source: ("가짜약품 가짜회사 당뇨",) for source in SOURCE_NAMES}
            ),
            "needs_second_hop": False,
        }
    )

    class Client:
        serving_id = "190"

        def complete(self, _messages, *, budget_s=None) -> str:
            return contaminated.model_dump_json()

    plan = V4Planner(Client()).plan("품목기준코드 201306324 허가사항", ())
    queries = tuple(query for _, values in plan.tool_queries.items() for query in values)

    assert plan.answer_sources == ("nedrug",)
    assert plan.needs_second_hop is True
    assert all("201306324" in query for query in queries)
    assert all("가짜약품" not in query and "가짜회사" not in query for query in queries)


def test_v4_evidence_envelope_is_typed_and_source_specific() -> None:
    hira = v4_adapters._evidence_envelope(
        "hira",
        "D693 환자수 2024",
        {
            "calls": [
                {
                    "render_data": {
                        "request": {"year": "2024"},
                        "items": [{"ptntCnt": "9231"}],
                    }
                }
            ]
        },
    )
    clinical = v4_adapters._evidence_envelope(
        "clinicaltrials",
        "NCT05151731",
        {
            "protocolSection": {
                "designModule": {"phases": ["PHASE2"]},
                "statusModule": {"overallStatus": "COMPLETED"},
            }
        },
    )
    nedrug = v4_adapters._evidence_envelope(
        "nedrug",
        "리바로 허가",
        {"calls": [{"render_data": {"ITEM_NAME": "리바로정"}}]},
    )

    assert isinstance(hira, EvidenceEnvelope)
    assert hira.kind == "hira"
    assert hira.metric_type == "patient_count"
    assert hira.period == ("2024",)
    assert hira.eligible_claims == ("patient_count",)
    assert hira.causal is False
    assert clinical.kind == "clinical"
    assert clinical.phase == ("PHASE2",)
    assert clinical.recruitment_status == "COMPLETED"
    assert nedrug.kind == "nedrug"
    assert nedrug.product == ("리바로정",)
    assert nedrug.eligible_claims == ("approval",)


def test_v4_evidence_envelope_grants_claims_only_from_matching_payload_fields() -> None:
    reimbursement = v4_adapters._evidence_envelope(
        "hira",
        "아일리아 급여기준",
        {
            "calls": [
                {
                    "tool": "hira_reimbursement_criteria",
                    "render_data": {"criteria": "투여 기준 본문"},
                }
            ]
        },
    )
    patent = v4_adapters._evidence_envelope(
        "nedrug",
        "리바로 특허",
        {
            "calls": [
                {
                    "tool": "nedrug_patent_search",
                    "render_data": {"patent_expiry": "2030-01-01"},
                }
            ]
        },
    )

    assert reimbursement.eligible_claims == ("reimbursement",)
    assert patent.eligible_claims == ("patent",)


def test_v4_patent_source_grants_patent_claim_only_for_matching_payload() -> None:
    patent = v4_adapters._evidence_envelope(
        "patent",
        "리바로 특허 언제 만료돼?",
        {
            "calls": [
                {
                    "tool": "web_search",
                    "render_data": {
                        "items": [
                            {
                                "title": "Livalo patent protection",
                                "snippet": "The patent expires on February 2, 2024.",
                            }
                        ]
                    },
                }
            ]
        },
    )
    unrelated = v4_adapters._evidence_envelope(
        "patent",
        "리바로 특허 언제 만료돼?",
        {
            "calls": [
                {
                    "tool": "web_search",
                    "render_data": {
                        "items": [{"title": "Livalo overview", "snippet": "Product overview."}]
                    },
                }
            ]
        },
    )

    assert patent.eligible_claims == ("patent",)
    assert unrelated.eligible_claims == ()


def test_v4_claim_eligibility_guard_blocks_source_tag_without_matching_claim() -> None:
    result = SourceResult(
        source="nedrug",
        query="리바로 특허",
        status="ok",
        payload={"calls": [{"render_data": {"ITEM_NAME": "리바로정"}}]},
        evidence=EvidenceEnvelope(
            kind="nedrug",
            entity_match="EXACT",
            source_scope="KR",
            time_match="NOT_REQUESTED",
            eligible_claims=("approval", "label"),
            causal=False,
        ),
    )

    gated = apply_v4_gates(
        "리바로 특허 언제 만료돼?",
        "## 핵심 답\n리바로 특허는 2030년에 만료됩니다. [출처: 식품의약품안전처]",
        (result,),
    )

    assert "2030년에 만료" not in gated.text
    assert "현재 근거 자격으로 확인되지 않았습니다" in gated.text
    assert gated.trace["claim_eligibility_guard"]["blocked"] is True
    assert gated.trace["claim_eligibility_guard"]["unsupported_claims"] == ["patent"]


def test_v4_claim_eligibility_guard_keeps_matching_source_claim() -> None:
    result = SourceResult(
        source="nedrug",
        query="리바로 특허",
        status="ok",
        payload={"calls": [{"render_data": {"patent_expiry": "2030-01-01"}}]},
        evidence=EvidenceEnvelope(
            kind="nedrug",
            entity_match="EXACT",
            source_scope="KR",
            time_match="NOT_REQUESTED",
            eligible_claims=("patent",),
            causal=False,
        ),
    )

    gated = apply_v4_gates(
        "리바로 특허 언제 만료돼?",
        "## 핵심 답\n리바로 특허는 2030년에 만료됩니다. [출처: 식품의약품안전처]",
        (result,),
    )

    assert "2030년에 만료" in gated.text
    assert gated.trace["claim_eligibility_guard"]["blocked"] is False


def test_v4_claim_eligibility_guard_blocks_untagged_claim_without_typed_support() -> None:
    payload = {"calls": [{"render_data": {"ITEM_NAME": "리바로정"}}]}
    result = SourceResult(
        source="nedrug",
        query="리바로 허가",
        status="ok",
        payload=payload,
        evidence=v4_adapters._evidence_envelope("nedrug", "리바로 허가", payload),
    )

    gated = apply_v4_gates(
        "리바로 특허 언제 만료돼?",
        "## 핵심 답\n리바로 특허는 2030년에 만료됩니다.",
        (result,),
    )

    assert "2030년에 만료" not in gated.text
    assert "현재 근거 자격으로 확인되지 않았습니다" in gated.text
    assert gated.trace["claim_eligibility_guard"]["blocked"] is True
    assert gated.trace["claim_eligibility_guard"]["unsupported_claims"] == ["patent"]


def test_v4_claim_eligibility_guard_does_not_let_notice_mask_unsupported_claim() -> None:
    payload = {"calls": [{"render_data": {"items": [{"ptntCnt": "9231"}]}}]}
    result = SourceResult(
        source="hira",
        query="D693 환자수",
        status="ok",
        payload=payload,
        evidence=v4_adapters._evidence_envelope("hira", "D693 환자수", payload),
    )

    gated = apply_v4_gates(
        "리바로 특허 언제 만료돼?",
        (
            "## 핵심 답\n리바로 특허는 2030년에 만료됩니다.\n"
            "- HIRA 환자수는 주상병 기준 청구 실인원이며 유병률과 다릅니다."
        ),
        (result,),
    )

    assert "2030년에 만료" not in gated.text
    assert gated.trace["claim_eligibility_guard"]["blocked"] is True
    assert gated.trace["claim_eligibility_guard"]["unsupported_claims"] == ["patent"]


def test_v4_claim_eligibility_guard_removes_only_the_unsupported_sentence() -> None:
    result = SourceResult(
        source="hira",
        query="D693 환자수",
        status="ok",
        payload={"calls": [{"render_data": {"items": [{"ptntCnt": "1606"}]}}]},
        evidence=EvidenceEnvelope(
            kind="hira",
            entity_match="EXACT",
            source_scope="KR",
            time_match="MATCH",
            eligible_claims=("patient_count",),
            causal=False,
        ),
    )

    gated = apply_v4_gates(
        "D693 환자수 알려줘",
        (
            "## 핵심 답\n"
            "D693 2024년 입원 환자수는 1,606명입니다. [출처: HIRA] "
            "관련 특허는 2030년에 만료됩니다. [출처: HIRA]"
        ),
        (result,),
    )

    assert "입원 환자수는 1,606명" in gated.text
    assert "2030년에 만료" not in gated.text
    assert gated.trace["claim_eligibility_guard"]["blocked_sentences"] == 1


def test_v4_claim_eligibility_guard_keeps_reimbursement_sentence_in_mixed_block() -> None:
    result = SourceResult(
        source="hira",
        query="아일리아 급여기준",
        status="ok",
        payload={"calls": [{"render_data": {"criteria": "투여대상 및 투여횟수"}}]},
        evidence=EvidenceEnvelope(
            kind="hira",
            entity_match="EXACT",
            source_scope="KR",
            time_match="NOT_REQUESTED",
            eligible_claims=("reimbursement",),
            causal=False,
        ),
    )

    gated = apply_v4_gates(
        "아일리아 급여기준 알려줘",
        (
            "## 핵심 답\n"
            "아일리아 급여기준은 투여대상과 투여횟수를 명시합니다. [출처: HIRA] "
            "효능효과는 망막질환 전반입니다. [출처: HIRA]"
        ),
        (result,),
    )

    assert "급여기준은 투여대상과 투여횟수" in gated.text
    assert "효능효과는 망막질환 전반" not in gated.text


def test_v4_claim_eligibility_guard_keeps_hira_reimbursement_exclusion_criteria() -> None:
    result = SourceResult(
        source="hira",
        query="아일리아 급여기준",
        status="ok",
        payload={"calls": [{"render_data": {"criteria": "투여대상 및 제외기준"}}]},
        evidence=EvidenceEnvelope(
            kind="hira",
            entity_match="EXACT",
            source_scope="KR",
            time_match="NOT_REQUESTED",
            eligible_claims=("reimbursement",),
            causal=False,
        ),
    )

    gated = apply_v4_gates(
        "아일리아 급여기준 알려줘",
        (
            "## 근거와 맥락\n"
            "초기 3회 투여 후 효과가 없는 경우는 제외기준에 해당합니다. [출처: HIRA] "
            "효능효과는 망막질환 전반입니다. [출처: HIRA]"
        ),
        (result,),
    )

    assert "초기 3회 투여 후 효과가 없는 경우는 제외기준" in gated.text
    assert "효능효과는 망막질환 전반" not in gated.text


def test_v4_hira_reimbursement_source_includes_notice_number() -> None:
    result = SourceResult(
        source="hira",
        query="아일리아 급여기준",
        status="ok",
        payload={
            "calls": [
                {
                    "render_data": {
                        "criteria": "투여대상 및 제외기준",
                        "notice_number": "고시 제2024-235호",
                    }
                }
            ]
        },
        evidence=EvidenceEnvelope(
            kind="hira",
            entity_match="EXACT",
            source_scope="KR",
            time_match="NOT_REQUESTED",
            eligible_claims=("reimbursement",),
            causal=False,
        ),
        citations=(
            Citation(
                source="HIRA",
                query="아일리아 급여기준",
                url="https://example.invalid/hira-notice",
                retrieved_at=datetime(2026, 8, 11, tzinfo=UTC),
                used=True,
            ),
        ),
    )

    gated = apply_v4_gates(
        "아일리아 급여기준 알려줘",
        "## 핵심 답\n아일리아 급여기준의 투여대상과 제외기준입니다. [출처: HIRA]",
        (result,),
    )

    assert "고시 제2024-235호" in gated.text


def test_v4_clinical_studies_are_classified_per_study_without_mutating_detail() -> None:
    payload = {
        "calls": [
            {
                "render_data": {
                    "payload": {
                        "studies": [
                            {
                                "NCTId": "NCT00000001",
                                "protocolSection": {
                                    "designModule": {"studyType": "INTERVENTIONAL"},
                                    "armsInterventionsModule": {
                                        "interventions": [{"type": "DRUG", "name": "candidate"}]
                                    },
                                },
                            },
                            {
                                "NCTId": "NCT00000002",
                                "protocolSection": {
                                    "designModule": {"studyType": "OBSERVATIONAL"},
                                    "armsInterventionsModule": {"interventions": []},
                                },
                            },
                            {
                                "NCTId": "NCT00000003",
                                "protocolSection": {
                                    "designModule": {"studyType": "INTERVENTIONAL"},
                                    "armsInterventionsModule": {
                                        "interventions": [{"type": "DEVICE", "name": "device"}]
                                    },
                                },
                            },
                        ]
                    }
                }
            }
        ]
    }
    result = SourceResult(
        source="clinicaltrials",
        query="뇌경색 임상시험",
        status="ok",
        payload=payload,
    )

    messages = v4_synthesizer._synthesis_messages(_plan(), (result,), ())
    prompt = json.loads(messages[-1]["content"])
    packet = prompt["external_evidence"][0]

    assert packet["detail"] == payload
    assert packet["study_classification"] == [
        {
            "study_id": "NCT00000001",
            "study_type": "INTERVENTIONAL",
            "intervention_types": ["DRUG"],
            "answer_section": "PRIMARY_DRUG_INTERVENTIONAL",
        },
        {
            "study_id": "NCT00000002",
            "study_type": "OBSERVATIONAL",
            "intervention_types": [],
            "answer_section": "ADJACENT_OBSERVATIONAL",
        },
        {
            "study_id": "NCT00000003",
            "study_type": "INTERVENTIONAL",
            "intervention_types": ["DEVICE"],
            "answer_section": "ADJACENT_NON_DRUG_INTERVENTIONAL",
        },
    ]
    assert "인접 연구를 종합 인사이트에서 다시 요약하거나 해석하지 않는다" in messages[0]["content"]


def test_v4_trend_query_requests_history_from_query_layer() -> None:
    calls: list[tuple[str, str, str, int]] = []

    class Layer:
        def brand_metric(self, brand, metric, period, *, history_points=10):
            calls.append((brand, metric, period, history_points))
            return {"metric": metric, "display": "85.87억원"}

        def top_brands(self, brand, *, limit, metric):
            return {"brand": brand, "limit": limit, "metric": metric}

    payloads = v4_adapters._strategic_mart_calls(
        Layer(),
        "리바로",
        "리바로 시장 규모가 지금 얼마고 어떻게 변해왔어",
    )

    assert payloads
    assert calls == [("리바로", "sales", "latest", 60)]


def test_v4_observational_evidence_cannot_confirm_cause() -> None:
    result = SourceResult(
        source="hira",
        query="환자수 감소 원인",
        status="ok",
        payload={"value": "감소"},
        evidence=EvidenceEnvelope(
            kind="hira",
            entity_match="EXACT",
            source_scope="KR",
            time_match="NOT_REQUESTED",
            eligible_claims=("patient_count", "association"),
            causal=False,
        ),
    )

    gated = apply_v4_gates(
        "환자수 감소 원인이 뭐야",
        "환자수 감소 원인으로 확인되었습니다.",
        (result,),
    )

    assert "원인으로 확인되었습니다" not in gated.text
    assert "구체적 원인은 확인되지 않았습니다" in gated.text
    assert gated.trace["causal_claim_guard"]["blocked"] is True


def test_v4_clients_use_their_scoped_genos_endpoints_and_tokens(monkeypatch) -> None:
    monkeypatch.setenv("GENOS_BASE_URL", "https://genos.example/api/gateway/rep/serving/163")
    monkeypatch.setenv("GENOS_SERVING_ID", "202")
    monkeypatch.setenv("GENOS_FINAL_SERVING_ID", "202")
    monkeypatch.setenv("GENOS_PLANNER_SERVING_ID", "190")
    monkeypatch.setenv("GENOS_SYNTH_SERVING_ID", "202")
    monkeypatch.setenv("GENOS_SYNTH_MODEL", "gemini-3.1-pro-preview")
    monkeypatch.setenv("GENOS_BEARER_TOKEN", "common-token")
    monkeypatch.setenv("GENOS_FINAL_BEARER_TOKEN", "final-token")
    monkeypatch.setenv("GENOS_PLANNER_BEARER_TOKEN", "planner-token")
    monkeypatch.setenv("V4_SYNTHESIZER_BEARER_TOKEN", "synthesizer-token")

    planner = planner_client()._client
    synthesizer = synthesizer_client()._client

    assert planner.base_url.endswith("/serving/190")
    assert planner.token == "planner-token"
    assert planner.timeout_s == 18
    assert synthesizer.base_url.endswith("/serving/202")
    assert synthesizer.token == "synthesizer-token"
    assert synthesizer.model == "gemini-3.1-pro-preview"
    assert synthesizer.timeout_s == 60
    assert synthesizer.total_budget_s == 64


def test_v4_synthesizer_defaults_to_pro_202_and_warns_when_env_is_missing(
    monkeypatch, caplog
) -> None:
    monkeypatch.setenv("GENOS_BASE_URL", "https://genos.example/api/gateway/rep/serving/163")
    monkeypatch.setenv("GENOS_FINAL_SERVING_ID", "202")
    monkeypatch.delenv("GENOS_SYNTH_SERVING_ID", raising=False)
    monkeypatch.delenv("GENOS_SYNTH_MODEL", raising=False)
    monkeypatch.delenv("V4_SYNTHESIZER_SERVING_ID", raising=False)
    monkeypatch.delenv("V4_SYNTHESIZER_MODEL", raising=False)

    client = synthesizer_client()._client

    assert client.base_url.endswith("/serving/202")
    assert client.model == "gemini-3.1-pro-preview"
    assert "GENOS_SYNTH_SERVING_ID is unset" in caplog.text


def test_v4_synthesizer_transport_preserves_finish_reason_and_usage(monkeypatch) -> None:
    captured = {}

    class Response:
        url = "https://genos.example/serving/202/chat/completions"

        def raise_for_status(self) -> None:
            return None

        def iter_lines(self, *, decode_unicode):
            assert decode_unicode is True
            yield 'data: {"model":"genos/202/gemini-3.1-pro-preview","choices":[{"delta":{"content":"답변"}}]}'
            yield 'data: {"model":"genos/202/gemini-3.1-pro-preview","choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":4}}'
            yield "data: [DONE]"

        def close(self) -> None:
            captured["closed"] = True

    def post(url, *, headers, json, stream, timeout):
        captured.update(url=url, headers=headers, json=json, stream=stream, timeout=timeout)
        return Response()

    monkeypatch.setattr(v4_llm.requests, "post", post)
    client = v4_llm.GenOSV4Client(
        base_url="https://genos.example/serving/202",
        token="scoped-token",
        model="gemini-3-flash-preview",
        timeout_s=15,
        total_budget_s=20,
    )

    completion = client.complete_detailed(
        [{"role": "user", "content": "질문"}],
        max_tokens=8192,
    )

    assert completion.text == "답변"
    assert completion.finish_reason == "stop"
    assert completion.usage == {"prompt_tokens": 10, "completion_tokens": 4}
    assert completion.serving_id == "202"
    assert completion.model == "gemini-3.1-pro-preview"
    assert captured["json"]["max_tokens"] == 8192
    assert captured["json"]["model"] == "gemini-3-flash-preview"
    assert captured["closed"] is True


def test_v4_synthesizer_uses_grounded_fallback_for_length_cutoff() -> None:
    class Client:
        def complete_detailed(self, _messages, *, budget_s=None, max_tokens=None):
            assert max_tokens == 8192
            return v4_llm.CompletionResult(
                text="잘린 답변입니다",
                finish_reason="length",
                usage={"completion_tokens": 8192},
                elapsed_ms=12_000,
            )

    result = SourceResult(
        source="hira",
        query="D693 환자수",
        status="ok",
        payload={
            "calls": [
                {
                    "render_data": {
                        "request": {"sickCd": "D693", "year": "2024"},
                        "items": [{"inpatOpat": "입원", "ptntCnt": "1606"}],
                    }
                }
            ]
        },
    )

    outcome = V4Synthesizer(Client()).synthesize_with_trace(
        _plan(), (result,), (), budget_s=24.0
    )

    assert "2024년 입원 환자수는 1,606명(청구 실인원)" in outcome.text
    assert outcome.trace["finish_reason"] == "length"
    assert outcome.trace["fallback_reason"] == "length"


def test_hira_year_calls_are_parallel_and_retry_only_failures() -> None:
    attempts: dict[str, int] = {}

    def fetch(_code: str, year: str):
        attempts[year] = attempts.get(year, 0) + 1
        time.sleep(0.04)
        status = "error" if year == "2022" and attempts[year] == 1 else "live"
        return SimpleNamespace(status=status, render_data={"request": {"year": year}})

    started = time.monotonic()
    calls = v4_adapters._parallel_hira_year_calls(
        fetch, "D693", ("2020", "2021", "2022", "2023", "2024")
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.16
    assert [call.render_data["request"]["year"] for call in calls] == [
        "2020", "2021", "2022", "2023", "2024"
    ]
    assert attempts == {"2020": 1, "2021": 1, "2022": 2, "2023": 1, "2024": 1}


def test_v4_hira_recent_range_uses_every_calendar_year() -> None:
    assert v4_adapters._requested_hira_years(
        "D693 상병 환자수 최근 5년", current_year=2026
    ) == ("2022", "2023", "2024", "2025", "2026")
    assert v4_adapters._requested_hira_years(
        "D693 2023년 환자수", current_year=2026
    ) == ("2023",)


def test_hira_synthesis_input_keeps_all_requested_year_calls() -> None:
    calls = [
        {
            "status": "live",
            "render_data": {
                "request": {"sickCd": "D693", "year": str(year)},
                "items": [{"inpatOpat": "외래", "patientCountDisplay": f"{year:,}"}],
            },
        }
        for year in range(2022, 2027)
    ]
    result = SourceResult(
        source="hira",
        query="D693 상병 환자수 최근 5년",
        status="ok",
        payload={"calls": calls},
    )

    messages = v4_synthesizer._synthesis_messages(_plan(), (result,), ())

    for year in range(2022, 2027):
        assert f'"year": "{year}"' in messages[-1]["content"]


def test_mart_synthesis_input_keeps_long_history_fields_after_scalar_metadata() -> None:
    history = [
        {"period": f"2025-{month:02d}", "sales_억원": float(month)}
        for month in range(1, 13)
    ]
    result = SourceResult(
        source="mart",
        query="리바로 연도별 매출액 추이",
        status="ok",
        payload={
            "calls": [
                {
                    "render_data": {
                        **{f"metadata_{index}": index for index in range(20)},
                        "brand_value_series_10pt": history,
                        "series_insight": {"trend_direction": "down"},
                    }
                }
            ]
        },
    )

    messages = v4_synthesizer._synthesis_messages(_plan(), (result,), ())
    prompt = json.loads(messages[-1]["content"])
    mart_block = prompt["internal_datamart"][0]

    assert '"brand_value_series_10pt"' in mart_block
    assert '"2025-12"' in mart_block
    assert '"series_insight"' in mart_block


def test_synthesis_input_keeps_every_raw_payload_key_and_row() -> None:
    late_rows = [
        {"ordinal": index, "body": f"고시 본문 원문 {index}"}
        for index in range(12)
    ]
    payload = {
        "calls": [
            {
                "status": "live",
                "render_data": {
                    **{f"metadata_{index}": f"value-{index}" for index in range(24)},
                    "items": late_rows,
                    "notice": "첨부파일 다운로드 안내도 원문 일부입니다.",
                },
            }
        ],
        "tail_sentinel": "payload-tail-preserved",
    }
    result = SourceResult(
        source="hira",
        query="아일리아 급여기준",
        status="ok",
        payload=payload,
    )

    messages = v4_synthesizer._synthesis_messages(_plan(), (result,), ())
    prompt = json.loads(messages[-1]["content"])
    packet = prompt["external_evidence"][0]

    assert packet["detail"] == payload
    assert packet["detail"]["calls"][0]["render_data"]["items"][-1] == late_rows[-1]
    assert packet["detail"]["tail_sentinel"] == "payload-tail-preserved"


def test_hira_coverage_notices_are_trace_metadata_not_answer_body() -> None:
    result = SourceResult(
        source="hira",
        query="D693 상병 환자수 최근 5년",
        status="ok",
        payload={
            "calls": [],
            "period_coverage": {
                "requested_periods": ["2022", "2023", "2024", "2025", "2026"],
                "periods": [
                    {"period": "2022", "status": "ok"},
                    {"period": "2023", "status": "ok"},
                    {"period": "2024", "status": "ok"},
                    {"period": "2025", "status": "error"},
                    {"period": "2026", "status": "no_data"},
                ],
            },
        },
    )

    notices = v4_synthesizer._coverage_notices((result,))

    assert "2025년은 조회 실패로 값을 확인하지 못했습니다(환자수 0 이 아님)." in notices
    assert "2026년은 조회 완료됐으나 해당 데이터가 없습니다." in notices


def test_hira_coverage_notice_is_not_appended_to_answer() -> None:
    result = SourceResult(
        source="hira",
        query="D693 상병 환자수 최근 5년",
        status="ok",
        payload={
            "calls": [],
            "period_coverage": {
                "requested_periods": ["2022", "2023", "2024", "2025", "2026"],
                "periods": [
                    {"period": "2022", "status": "ok"},
                    {"period": "2023", "status": "ok"},
                    {"period": "2024", "status": "ok"},
                    {"period": "2025", "status": "ok"},
                    {"period": "2026", "status": "no_data"},
                ],
            },
        },
        citations=(
            Citation(
                source="HIRA",
                query="D693 상병 환자수 최근 5년",
                url="https://www.hira.or.kr/",
                retrieved_at=datetime(2026, 8, 11, tzinfo=UTC),
            ),
        ),
    )

    synthesized = v4_synthesizer._finalize_answer(
        "확인된 4개년 값입니다.\n\n## 출처\n- 모델이 만든 출처",
        (result,),
    )
    gated = apply_v4_gates("D693 환자수 최근 5년", synthesized, (result,))

    assert "2026년은 조회 완료됐으나 해당 데이터가 없습니다" not in gated.text
    assert "HIRA 환자수는 주상병 기준 청구 실인원" in gated.text
    assert "모델이 만든 출처" not in gated.text


def test_runtime_preserves_recent_period_in_hira_answer_query() -> None:
    plan = _plan(hira=("D693 보건의료빅데이터개방시스템 환자수 통계",)).model_copy(
        update={
            "resolved_question": "최근 3년간 D693 진단 환자 수는 얼마인가요?",
            "answer_sources": ("hira",),
        }
    )

    enriched = _preserve_period_in_answer_queries(plan)

    assert enriched.tool_queries.hira == (
        "D693 보건의료빅데이터개방시스템 환자수 통계 최근 3년",
    )
    assert enriched.tool_queries.web == plan.tool_queries.web


def test_parallel_executor_soft_deadline_stops_after_answer_source_arrives() -> None:
    def adapter(source: str, query: str) -> SourceResult:
        time.sleep(0.01 if source == "hira" else 0.25)
        return SourceResult(source=source, query=query, status="ok", payload={"source": source})

    executor = ParallelSourceExecutor(
        adapters={name: (lambda query, source=name: adapter(source, query)) for name in SOURCE_NAMES},
        per_tool_timeout_s=1.0,
        total_timeout_s=1.0,
    )
    started = time.monotonic()
    results = executor.execute(
        _plan(),
        session_id="session-soft-deadline",
        answer_sources=("hira",),
        soft_deadline_s=0.06,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.16
    assert next(item for item in results if item.source == "hira").status == "ok"
    assert any(item.notice == "정답 근거 도착 후 soft deadline으로 미포함" for item in results)


def test_planner_does_not_outer_retry_transport_failures() -> None:
    class TimeoutClient:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, _messages, *, budget_s=None) -> str:
            self.calls += 1
            raise requests.Timeout("planner transport timed out")

    client = TimeoutClient()
    output = V4Planner(client).plan("리바로 요즘 어때", ())

    assert client.calls == 1
    assert output.linking_plan == "planner fallback; no second hop: planner transport timed out"


def test_planner_detailed_trace_keeps_usage_and_corrects_obvious_answer_source() -> None:
    class Client:
        serving_id = "190"

        def complete_detailed(self, _messages, *, budget_s, max_tokens):
            assert budget_s > 0
            assert max_tokens > 0
            return v4_llm.CompletionResult(
                text=_plan().model_copy(
                    update={"answer_sources": ("hira", "mart", "web")}
                ).model_dump_json(),
                finish_reason="stop",
                usage={
                    "prompt_tokens": 120,
                    "completion_tokens": 30,
                    "completion_tokens_details": {"reasoning_tokens": 11},
                },
                elapsed_ms=1250.0,
                serving_id="190",
                model="gemini-3-flash-preview",
            )

    outcome = V4Planner(Client()).plan_with_trace(
        "D693 상병 환자수 최근 5년",
        (),
    )

    assert outcome.plan.answer_sources == ("hira",)
    assert outcome.trace["usage"] == {
        "input_tokens": 120,
        "output_tokens": 30,
        "thinking_tokens": 11,
    }
    assert outcome.trace["elapsed_ms"] == 1250.0
    assert outcome.trace["serving_id"] == "190"
    assert outcome.trace["model"] == "gemini-3-flash-preview"


def test_planner_limits_first_wave_to_one_query_per_source() -> None:
    class Client:
        serving_id = "190"

        def complete_detailed(self, _messages, *, budget_s, max_tokens):
            plan = _plan().model_copy(
                update={
                    "tool_queries": ToolQueries(
                        **{
                            source: (f"{source} primary", f"{source} duplicate")
                            for source in SOURCE_NAMES
                        }
                    )
                }
            )
            return v4_llm.CompletionResult(
                text=plan.model_dump_json(),
                finish_reason="stop",
                usage={},
                elapsed_ms=10.0,
            )

    outcome = V4Planner(Client()).plan_with_trace("리바로 요즘 어때", ())

    assert all(len(queries) == 1 for _, queries in outcome.plan.tool_queries.items())


def test_planner_fallback_trace_is_non_null_when_transport_fails() -> None:
    class Client:
        serving_id = "190"

        def complete_detailed(self, _messages, *, budget_s, max_tokens):
            raise requests.Timeout("planner transport timed out")

    outcome = V4Planner(Client()).plan_with_trace("리바로 요즘 어때", ())

    assert outcome.plan.linking_plan.startswith("planner fallback;")
    assert outcome.trace["usage"] == {
        "input_tokens": None,
        "output_tokens": None,
        "thinking_tokens": None,
    }
    assert outcome.trace["status"] == "fallback"


def test_runtime_marks_successful_citations_used() -> None:
    plan = _plan()

    class Planner:
        def plan(self, _question, _turns, *, budget_s):
            return plan

        def link(self, *_args, **_kwargs):
            return None

    class Executor:
        def execute(self, _plan, *, session_id, total_timeout_s, **_kwargs):
            return (
                SourceResult(
                    source="web",
                    query="web query",
                    status="ok",
                    payload={"answer": "근거"},
                    citations=(
                        Citation(
                            source="web",
                            query="web query",
                            url="https://example.test/source",
                            retrieved_at=datetime.now(UTC),
                            used=False,
                        ),
                    ),
                ),
            )

    class Synthesizer:
        def synthesize(self, _plan, results, _turns, *, budget_s):
            assert results[0].citations[0].used is True
            assert results[0].citations[0].source == "웹 자료"
            return "근거 기반 답변"

    answer = V4Runtime(
        planner=Planner(),
        executor=Executor(),
        synthesizer=Synthesizer(),
    ).answer("질문", conversation_id="conversation-a", turns=())

    assert answer.trace["tool_results"][0]["citations"][0]["used"] is True
    assert answer.trace["tool_results"][0]["citations"][0]["source"] == "웹 자료"


def test_runtime_reserves_planner_budget_without_echoing_config_as_actual_serving() -> None:
    plan = _plan()

    class Planner:
        serving_id = "190"

        def plan(self, _question, _turns, *, budget_s):
            assert budget_s >= 18.0
            return plan

        def link(self, *_args, **_kwargs):
            return None

    class Executor:
        def execute(self, _plan, *, session_id, total_timeout_s, **_kwargs):
            return ()

    class Synthesizer:
        def synthesize(self, _plan, _results, _turns, *, budget_s):
            return "근거 기반 답변"

    answer = V4Runtime(
        planner=Planner(),
        executor=Executor(),
        synthesizer=Synthesizer(),
    ).answer("질문", conversation_id="conversation-planner", turns=())

    assert answer.trace["planner_serving"] == "not_applicable"
    assert answer.trace["fallback"] is False


def test_runtime_exposes_synthesizer_usage_metadata() -> None:
    plan = _plan()

    class Planner:
        serving_id = "190"

        def plan(self, _question, _turns, *, budget_s):
            return plan

        def link(self, *_args, **_kwargs):
            return None

    class Executor:
        def execute(self, _plan, **_kwargs):
            return ()

    class Synthesizer:
        def synthesize_with_trace(self, _plan, _results, _turns, *, budget_s):
            assert budget_s >= 59.0
            return v4_synthesizer.SynthesisOutcome(
                text="근거 기반 답변",
                trace={
                    "finish_reason": "stop",
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                    "serving_id": "202",
                    "model": "gemini-3.1-pro-preview",
                },
            )

    answer = V4Runtime(
        planner=Planner(),
        executor=Executor(),
        synthesizer=Synthesizer(),
    ).answer("질문", conversation_id="usage-trace", turns=())

    assert answer.trace["synthesizer"]["finish_reason"] == "stop"
    assert answer.trace["synthesizer"]["usage"]["completion_tokens"] == 20
    assert answer.trace["synth_serving"] == "202"
    assert answer.trace["synth_model"] == "gemini-3.1-pro-preview"


def test_runtime_reserves_total_budget_for_a_complete_synthesis() -> None:
    runtime = V4Runtime(planner=object(), executor=object(), synthesizer=object())

    assert runtime._total_timeout_s == 150.0


def test_runtime_exposes_normalized_usage_and_stage_breakdown() -> None:
    plan = _plan()

    class Planner:
        serving_id = "190"

        def plan_with_trace(self, _question, _turns, *, budget_s):
            return SimpleNamespace(
                plan=plan,
                trace={
                    "status": "ok",
                    "elapsed_ms": 12.0,
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "thinking_tokens": 1,
                    },
                },
            )

        def link(self, *_args, **_kwargs):
            return None

    class Executor:
        def execute_with_trace(self, _plan, **_kwargs):
            return SimpleNamespace(
                results=(),
                trace={
                    "elapsed_ms": 25.0,
                    "quorum_fired": True,
                    "quorum_fire_ms": 6.0,
                    "tools": [],
                },
            )

    class Synthesizer:
        def synthesize_with_trace(self, _plan, _results, _turns, *, budget_s):
            return v4_synthesizer.SynthesisOutcome(
                text="근거 기반 답변",
                trace={
                    "finish_reason": "stop",
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "completion_tokens_details": {"reasoning_tokens": 7},
                    },
                    "elapsed_ms": 30.0,
                },
            )

    answer = V4Runtime(
        planner=Planner(),
        executor=Executor(),
        synthesizer=Synthesizer(),
    ).answer("질문", conversation_id="usage-stage-trace", turns=())

    assert answer.trace["planner_usage"]["input_tokens"] == 10
    assert answer.trace["synth_usage"] == {
        "input_tokens": 100,
        "output_tokens": 20,
        "thinking_tokens": 7,
        "finish_reason": "stop",
        "measurement": "reported",
    }
    assert answer.timing["planner_elapsed_ms"] == 12.0
    assert answer.timing["wave_elapsed_ms"] == 25.0
    assert answer.timing["synth_elapsed_ms"] == 30.0
    assert answer.trace["execution"]["quorum_fired"] is True


def test_runtime_usage_is_explicit_when_synthesizer_does_not_run() -> None:
    usage = v4_synthesizer.SynthesisOutcome(
        text="결정론 답변",
        trace={"status": "fallback", "fallback_reason": "no_evidence"},
    )

    normalized = __import__(
        "jw_chat_agent_poc.service.v4.runtime", fromlist=["_normalized_synth_usage"]
    )._normalized_synth_usage(usage.trace)

    assert normalized == {
        "input_tokens": "not_applicable",
        "output_tokens": "not_applicable",
        "thinking_tokens": "not_applicable",
        "finish_reason": "not_applicable",
        "measurement": "not_applicable",
    }


def test_runtime_emits_public_five_stage_progress_for_linked_answer() -> None:
    first_plan = _plan().model_copy(update={"needs_second_hop": True})
    linked_plan = _plan(web=("연결 검색",))

    class Planner:
        serving_id = "190"

        def plan_with_trace(self, _question, _turns, *, budget_s):
            return SimpleNamespace(
                plan=first_plan,
                trace={"elapsed_ms": 1.0, "usage": {}},
            )

        def link(self, *_args, **_kwargs):
            return linked_plan

    class Executor:
        def execute_with_trace(self, plan, *, progress_callback=None, **_kwargs):
            if progress_callback is not None:
                progress_callback("hira")
                progress_callback("web")
            return SimpleNamespace(
                results=(
                    SourceResult(
                        source="hira",
                        query=plan.tool_queries.hira[0],
                        status="ok",
                        payload={"calls": []},
                    ),
                ),
                trace={"elapsed_ms": 2.0, "tools": []},
            )

    class Synthesizer:
        def synthesize_with_trace(self, *_args, **_kwargs):
            return v4_synthesizer.SynthesisOutcome(
                text="근거 기반 답변",
                trace={"elapsed_ms": 3.0, "usage": {}},
            )

    progress: list[dict[str, str]] = []
    V4Runtime(
        planner=Planner(), executor=Executor(), synthesizer=Synthesizer()
    ).answer(
        "D693 최근 환자수",
        conversation_id="progress",
        turns=(),
        progress_callback=progress.append,
    )

    assert [event["name"] for event in progress] == [
        "질문 해석",
        "조회 계획",
        "자료 수집 중",
        "자료 수집 중",
        "연결 조회",
        "답변 작성 중",
    ]
    assert progress[2]["detail"] == "건강보험심사평가원 완료"
    assert progress[3]["detail"] == "건강보험심사평가원 완료 · 웹 자료 완료"
    assert progress[1]["detail"] == "시장 · 허가 · 임상"
    assert all("MCP" not in event["detail"] for event in progress)


def test_query_plan_progress_limits_expanded_intents_to_five() -> None:
    detail = __import__(
        "jw_chat_agent_poc.service.v4.runtime", fromlist=["_expanded_intents_detail"]
    )._expanded_intents_detail(("시장", "허가", "임상", "환자수", "특허", "급여", "안전성"))

    assert detail == "시장 · 허가 · 임상 · 환자수 · 특허 · 외 2개"


def test_runtime_runs_one_web_gap_fill_for_missing_hira_periods() -> None:
    plan = _plan(hira=("D693 환자수 최근 3년",)).model_copy(
        update={"answer_sources": ("hira",)}
    )

    class Planner:
        serving_id = "190"

        def plan_with_trace(self, *_args, **_kwargs):
            return SimpleNamespace(plan=plan, trace={"elapsed_ms": 1.0, "usage": {}})

        def link(self, *_args, **_kwargs):
            return None

    class Executor:
        def __init__(self) -> None:
            self.filters: list[tuple[str, ...] | None] = []

        def execute_with_trace(self, current_plan, *, source_filter=None, **_kwargs):
            self.filters.append(source_filter)
            if source_filter == ("web",):
                results = (
                    SourceResult(
                        source="web",
                        query=current_plan.tool_queries.web[0],
                        status="ok",
                        payload={"calls": []},
                    ),
                )
            else:
                results = (
                    SourceResult(
                        source="hira",
                        query=current_plan.tool_queries.hira[0],
                        status="ok",
                        payload={
                            "calls": [],
                            "period_coverage": {
                                "requested_periods": ["2024", "2025", "2026"],
                                "periods": [
                                    {"period": "2024", "status": "ok"},
                                    {"period": "2025", "status": "no_data"},
                                    {"period": "2026", "status": "no_data"},
                                ],
                            },
                        },
                    ),
                )
            return SimpleNamespace(results=results, trace={"elapsed_ms": 2.0, "tools": []})

    class Synthesizer:
        def synthesize_with_trace(self, _plan, results, *_args, **_kwargs):
            assert any(result.source == "web" for result in results)
            return v4_synthesizer.SynthesisOutcome(
                text="확인된 공식 통계와 별도 보완 근거입니다.",
                trace={"elapsed_ms": 3.0, "usage": {}},
            )

    executor = Executor()
    answer = V4Runtime(
        planner=Planner(), executor=executor, synthesizer=Synthesizer()
    ).answer("D693 환자수 최근 3년", conversation_id="gap", turns=())

    assert executor.filters == [None, ("web",)]
    assert answer.trace["gap_fill"]["source"] == "hira"
    assert answer.trace["gap_fill"]["missing_periods"] == ["2025", "2026"]
    assert answer.trace["gap_fill"]["attempted"] is True


def test_gap_fill_keeps_only_tier_one_or_two_web_evidence() -> None:
    source = SourceResult(
        source="web",
        query="D693 2025 공식 통계 발표",
        status="ok",
        payload={
            "calls": [
                {
                    "render_data": {
                        "items": [
                            {"url": "https://www.hira.or.kr/report", "content": "공식 " * 80},
                            {"url": "https://news.example.com/story", "content": "언론 " * 80},
                            {"url": "https://university.ac.kr/paper", "content": "학술 " * 80},
                        ]
                    }
                }
            ]
        },
    )

    tagged = __import__(
        "jw_chat_agent_poc.service.v4.runtime", fromlist=["_tag_gap_result"]
    )._tag_gap_result(
        source,
        {"source": "hira", "missing_periods": ["2025"], "query": "D693"},
    )

    items = tagged.payload["calls"][0]["render_data"]["items"]
    assert [item["trust_tier"] for item in items] == ["TIER1", "TIER2"]
    assert all("news.example.com" not in item["url"] for item in items)


def test_runtime_reuses_prior_table_results_for_reference_followup() -> None:
    plan = _plan().model_copy(
        update={
            "resolved_question": "리바로 순위 알려줘",
            "answer_sources": ("mart",),
        }
    )

    class Planner:
        serving_id = "190"

        def plan_with_trace(self, _question, _turns, *, budget_s):
            return SimpleNamespace(
                plan=plan,
                trace={"elapsed_ms": 1.0, "usage": {}},
            )

        def link(self, *_args, **_kwargs):
            return None

    class Executor:
        def __init__(self) -> None:
            self.calls = 0

        def execute_with_trace(self, _plan, **_kwargs):
            self.calls += 1
            return SimpleNamespace(
                results=(
                    SourceResult(
                        source="mart",
                        query="리바로 순위",
                        status="ok",
                        payload={"rank": 6},
                    ),
                ),
                trace={"elapsed_ms": 2.0, "tools": []},
            )

    class Synthesizer:
        def synthesize_with_trace(self, _plan, results, _turns, *, budget_s):
            assert results[0].payload["rank"] == 6
            return v4_synthesizer.SynthesisOutcome(
                text="리바로는 전략시장 내 6위입니다.",
                trace={"elapsed_ms": 3.0, "usage": {}},
            )

    executor = Executor()
    runtime = V4Runtime(
        planner=Planner(),
        executor=executor,
        synthesizer=Synthesizer(),
    )

    runtime.answer("리바로 순위 알려줘", conversation_id="multi-1", turns=())
    followup = runtime.answer(
        "아까 그 순위 몇 위랬지?",
        conversation_id="multi-1",
        turns=(),
    )

    assert executor.calls == 1
    assert followup.trace["execution"]["session_result_reused"] is True
    assert followup.trace["tool_results"][0]["cache_hit"] is True
    assert "이전 조회 재사용" in followup.text


def test_runtime_recognizes_filter_followup_without_postposition() -> None:
    assert _is_prior_result_reference("그 중 국내 진행 중인 것만") is True


def test_runtime_reuses_matching_prior_source_for_filter_followup() -> None:
    first_plan = _plan(clinicaltrials=("당뇨망막병증 임상",)).model_copy(
        update={"answer_sources": ("clinicaltrials",)}
    )
    filter_plan = _plan(
        nedrug=("국내 당뇨망막병증 임상",),
        clinicaltrials=("국내 진행 중 당뇨망막병증 임상",),
        web=("국내 진행 중 당뇨망막병증 임상",),
    ).model_copy(update={"answer_sources": ("nedrug", "clinicaltrials", "web")})

    class Planner:
        serving_id = "190"

        def plan_with_trace(self, question, _turns, *, budget_s):
            plan = filter_plan if "그 중" in question else first_plan
            return SimpleNamespace(plan=plan, trace={"elapsed_ms": 1.0, "usage": {}})

        def link(self, *_args, **_kwargs):
            return None

    class Executor:
        def __init__(self) -> None:
            self.calls = 0

        def execute_with_trace(self, _plan, **_kwargs):
            self.calls += 1
            return SimpleNamespace(
                results=(
                    SourceResult(
                        source="clinicaltrials",
                        query="당뇨망막병증 임상",
                        status="ok",
                        payload={"studies": [{"title": "국내 상태 확인 필요"}]},
                    ),
                ),
                trace={"elapsed_ms": 2.0, "tools": []},
            )

    class Synthesizer:
        def synthesize_with_trace(self, _plan, results, _turns, *, budget_s):
            assert any(result.source == "clinicaltrials" for result in results)
            return v4_synthesizer.SynthesisOutcome(
                text="이전 임상 목록에서 국내 진행 여부를 확인합니다.",
                trace={"elapsed_ms": 3.0, "usage": {}},
            )

    executor = Executor()
    runtime = V4Runtime(planner=Planner(), executor=executor, synthesizer=Synthesizer())
    runtime.answer("당뇨망막병증 임상 동향", conversation_id="filter", turns=())
    followup = runtime.answer(
        "그 중 국내 진행 중인 것만", conversation_id="filter", turns=()
    )

    assert executor.calls == 1
    assert followup.trace["execution"]["session_result_reused"] is True


def test_runtime_does_not_reuse_prior_results_for_a_different_answer_source() -> None:
    mart_plan = _plan().model_copy(
        update={"resolved_question": "리바로 순위", "answer_sources": ("mart",)}
    )
    safety_plan = _plan(openfda=("리바로 이상사례",)).model_copy(
        update={
            "resolved_question": "리바로 이상사례",
            "answer_sources": ("openfda",),
        }
    )

    class Planner:
        serving_id = "190"

        def plan_with_trace(self, question, _turns, *, budget_s):
            plan = safety_plan if "이상사례" in question else mart_plan
            return SimpleNamespace(plan=plan, trace={"elapsed_ms": 1.0, "usage": {}})

        def link(self, *_args, **_kwargs):
            return None

    class Executor:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def execute_with_trace(self, plan, **_kwargs):
            source = plan.answer_sources[0]
            self.calls.append(source)
            return SimpleNamespace(
                results=(
                    SourceResult(
                        source=source,
                        query=f"{source} query",
                        status="ok",
                        payload={"source": source},
                    ),
                ),
                trace={"elapsed_ms": 2.0, "tools": []},
            )

    class Synthesizer:
        def synthesize_with_trace(self, planned, results, _turns, *, budget_s):
            if planned.answer_sources == ("openfda",):
                assert any(result.source == "openfda" for result in results)
            return v4_synthesizer.SynthesisOutcome(
                text="FDA 이상사례 근거를 확인했습니다.",
                trace={"elapsed_ms": 3.0, "usage": {}},
            )

    executor = Executor()
    runtime = V4Runtime(
        planner=Planner(), executor=executor, synthesizer=Synthesizer()
    )
    runtime.answer("리바로 순위", conversation_id="multi-source", turns=())
    followup = runtime.answer(
        "아까 그 약 이상사례는?", conversation_id="multi-source", turns=()
    )

    assert executor.calls == ["mart", "openfda"]
    assert followup.trace["execution"]["session_result_reused"] is False


def test_runtime_runs_at_most_one_linking_hop() -> None:
    first_plan = _plan().model_copy(update={"needs_second_hop": True})
    linked_plan = _plan(web=("linked entity query",))

    class Planner:
        link_calls = 0

        def plan(self, _question, _turns, *, budget_s):
            return first_plan

        def link(self, *_args, **_kwargs):
            self.link_calls += 1
            return linked_plan

    class Executor:
        calls = 0

        def execute(self, plan, *, session_id, total_timeout_s, **_kwargs):
            self.calls += 1
            return (
                SourceResult(
                    source="web",
                    query=plan.tool_queries.web[0],
                    status="ok",
                    payload={"answer": plan.tool_queries.web[0]},
                ),
            )

    class Synthesizer:
        def synthesize(self, _plan, results, _turns, *, budget_s):
            assert len(results) == 2
            return "연결 결과"

    planner = Planner()
    executor = Executor()
    answer = V4Runtime(
        planner=planner,
        executor=executor,
        synthesizer=Synthesizer(),
    ).answer("질문", conversation_id="conversation-link", turns=())

    assert planner.link_calls == 1
    assert executor.calls == 2
    assert answer.trace["second_hop"] is not None


def test_v4_gates_keep_mart_numbers_copy_only_and_require_sources() -> None:
    results = (
        SourceResult(
            source="mart",
            query="리바로 매출",
            status="ok",
            payload={"brand": "리바로", "sales_eok": 85.87, "source": "UBIST"},
            citations=(
                Citation(
                    source="UBIST",
                    query="리바로 매출",
                    url="mart://ubist/brand-metric",
                    retrieved_at=datetime.now(UTC),
                    used=True,
                ),
            ),
        ),
    )
    answer = "리바로 매출은 99.99억원입니다."

    gated = apply_v4_gates("리바로 매출 알려줘", answer, results)

    assert "99.99" not in gated.text
    assert "85.87" in gated.text
    assert "## 출처" in gated.text
    assert gated.trace["mart_numeric_copy_only"]["blocked"] is True


def test_v4_gate_does_not_substitute_originator_sales_for_generic_subset() -> None:
    question = (
        "리바로젯(피타바스타틴 및 에제티미브 복합제)의 제네릭 의약품들 중에서 "
        "매출액이 가장 큰 제품은 무엇인가요?"
    )
    results = (
        SourceResult(
            source="nedrug",
            query="리바로젯 제네릭",
            status="ok",
            payload={
                "calls": [
                    {
                        "status": "live",
                        "render_data": {
                            "items": [
                                {
                                    "ITEM_NAME": "리바로젯정2/10밀리그램",
                                    "ENTP_NAME": "제이더블유중외제약(주)",
                                }
                            ]
                        },
                    }
                ]
            },
        ),
        SourceResult(
            source="mart",
            query="리바로젯 제네릭 제품별 매출액 순위",
            status="ok",
            payload={
                "calls": [
                    {
                        "summary_text": "리바로젯 2026-06 UBIST 전략 mart 지표: 매출 124.54억원.",
                        "render_data": {
                            "brand": "리바로젯",
                            "sales_억원": 124.54,
                        },
                    }
                ]
            },
        ),
    )

    gated = apply_v4_gates(
        question,
        "리바로젯 본품 매출은 124.54억원입니다.",
        results,
    )

    assert "제네릭 제품 목록" in gated.text
    assert "본품 매출을 제네릭 매출로 대체하지 않습니다" in gated.text
    assert "124.54억원" not in gated.text
    assert gated.trace["subset_scope_guard"]["blocked"] is True
    assert gated.trace["mart_numeric_copy_only"]["blocked"] is False

    followup_gated = apply_v4_gates(
        "리바로젯 각 용량별 매출액 중 가장 큰 것은 무엇인가요?",
        "리바로젯 본품 매출은 124.54억원입니다.",
        results,
    )

    assert "제네릭 제품 목록" in followup_gated.text
    assert "124.54억원" not in followup_gated.text
    assert followup_gated.trace["subset_scope_guard"]["blocked"] is True


@pytest.mark.parametrize(
    ("question", "answer"),
    (
        ("리바로 매출 알려줘", "리바로 매출은 99.99억입니다."),
        ("리바로 매출 알려줘", "리바로 매출은 KRW 9,999입니다."),
        ("리바로 점유율 알려줘", "리바로 점유율은 99.99입니다."),
        ("리바로 성장률 알려줘", "리바로 성장률은 99.99입니다."),
    ),
)
def test_v4_gates_reject_invented_mart_numbers_with_implicit_units(
    question: str,
    answer: str,
) -> None:
    results = (
        SourceResult(
            source="mart",
            query=question,
            status="ok",
            payload={
                "sales_eok": 85.87,
                "share_pct": 3.72,
                "growth_pct": 4.1,
                "source": "UBIST",
            },
        ),
    )

    gated = apply_v4_gates(question, answer, results)

    assert "99.99" not in gated.text
    assert "9,999" not in gated.text
    assert gated.trace["mart_numeric_copy_only"]["blocked"] is True


def test_v4_gates_keep_synthesized_mart_prose_with_non_metric_numbers() -> None:
    results = (
        SourceResult(
            source="mart",
            query="리바로 매출",
            status="ok",
            payload={
                "calls": [
                    {
                        "summary_text": "리바로 2026-06 매출은 85.87억원입니다.",
                        "render_data": {
                            "period": "2026-06",
                            "sales_억원": 85.87,
                        },
                    }
                ]
            },
        ),
    )
    answer = (
        "## 핵심 답\n"
        "리바로의 2026년 6월 매출은 85.87억원입니다. [출처: 내부 데이터마트]\n\n"
        "## 종합 인사이트\n"
        "2025년 이후 흐름은 추가 기간 자료와 함께 해석해야 합니다.\n\n"
        "## 출처\n- 내부 데이터마트"
    )

    gated = apply_v4_gates("리바로 매출 알려줘", answer, results)

    assert gated.trace["mart_numeric_copy_only"]["blocked"] is False
    assert "## 핵심 답" in gated.text
    assert "종합 인사이트" in gated.text


def test_v4_gates_render_verified_mart_summary_instead_of_raw_fields() -> None:
    results = (
        SourceResult(
            source="mart",
            query="리바로 매출",
            status="ok",
            payload={
                "calls": [
                    {
                        "summary_text": "리바로 2026-06 UBIST 전략 mart 지표: 매출 85.87억원.",
                        "render_data": {
                            "value": 8587458961.25,
                            "sales_억원": 85.87,
                            "market_value": 230833352390.9699,
                        },
                    }
                ]
            },
        ),
    )

    gated = apply_v4_gates("리바로 매출 알려줘", "리바로 매출은 99.99억원입니다.", results)

    assert "85.87억원" in gated.text
    assert "8587458961.25" not in gated.text
    assert "230833352390.9699" not in gated.text
    assert "원시 필드" not in gated.text
    assert "- value:" not in gated.text


def test_v4_gates_render_existing_mart_history_points_when_synthesis_is_blocked() -> None:
    results = (
        SourceResult(
            source="mart",
            query="리바로 매출 추이",
            status="ok",
            payload={
                "calls": [
                    {
                        "summary_text": "리바로 2026-06 UBIST 전략 mart 지표: 매출 85.87억원.",
                        "render_data": {
                            "brand": "리바로",
                            "brand_value_series_10pt": [
                                {"period": "2021-07", "value_억원": 69.24},
                                {"period": "2021-12", "value_억원": 75.34},
                                {"period": "2022-12", "value_억원": 77.73},
                                {"period": "2026-06", "value_억원": 85.87},
                            ],
                            "market_size_series": [
                                {"period": "2021-07", "value_억원": 1446.74},
                                {"period": "2021-12", "value_억원": 1590.98},
                                {"period": "2022-12", "value_억원": 1743.44},
                                {"period": "2026-06", "value_억원": 2308.33},
                            ],
                        },
                    }
                ]
            },
        ),
    )

    gated = apply_v4_gates(
        "리바로 매출 추이가 어떻게 변해왔어?",
        "리바로 매출은 99.99억원입니다.",
        results,
    )

    assert "| 기간 | 리바로 매출 | 시장 규모 |" in gated.text
    assert (
        "리바로 매출은 2021년 7월 69.24억원에서 2026년 6월 85.87억원으로 "
        "5년간 증가했습니다."
    ) in gated.text
    assert (
        "연도별: 2021년 7월 69.24억원 · 2021년 12월 75.34억원 · "
        "2022년 12월 77.73억원 · 2026년 6월 85.87억원"
    ) in gated.text
    assert "| 2021-07 | 69.24억원 | 1446.74억원 |" in gated.text
    assert "| 2026-06 | 85.87억원 | 2308.33억원 |" in gated.text
    assert "99.99" not in gated.text


def test_v4_gates_use_market_series_for_market_size_trend_fallback() -> None:
    results = (
        SourceResult(
            source="mart",
            query="리바로 시장 규모 추이",
            status="ok",
            payload={
                "calls": [
                    {
                        "render_data": {
                            "brand": "리바로",
                            "brand_value_series_10pt": [
                                {"period": "2022-12", "value_억원": 77.73},
                                {"period": "2026-06", "value_억원": 85.87},
                            ],
                            "market_size_series": [
                                {"period": "2022-12", "value_억원": 1743.44},
                                {"period": "2023-12", "value_억원": 1901.22},
                                {"period": "2024-12", "value_억원": 2077.31},
                                {"period": "2025-12", "value_억원": 2244.08},
                                {"period": "2026-06", "value_억원": 2308.33},
                            ],
                        }
                    }
                ]
            },
        ),
    )

    gated = apply_v4_gates(
        "리바로 시장 규모가 지금 얼마고 어떻게 변해왔어?",
        "리바로 시장 규모는 9999억원입니다.",
        results,
    )

    assert (
        "리바로 전략 시장 규모는 2022년 12월 1743.44억원에서 "
        "2026년 6월 2308.33억원으로 4년간 증가했습니다."
    ) in gated.text
    assert (
        "연도별: 2022년 12월 1743.44억원 · 2023년 12월 1901.22억원 · "
        "2024년 12월 2077.31억원 · 2025년 12월 2244.08억원 · "
        "2026년 6월 2308.33억원"
    ) in gated.text
    assert "리바로 매출은" not in gated.text
    assert "9999" not in gated.text


def test_v4_base_query_removes_patent_planner_suffix() -> None:
    assert v4_adapters._base_query("리바로정 특허권 등재 현황") == "리바로정"


def test_v4_base_query_removes_reexamination_suffix() -> None:
    assert v4_adapters._base_query("리바로젯정 재심사 종료일") == "리바로젯정"


def test_v4_nedrug_patent_query_calls_mfds_without_brand_resolution(monkeypatch) -> None:
    from jw_chat_agent_poc.agent_loop import factory
    from jw_chat_agent_poc.service import general_view_routing
    from jw_chat_agent_poc.tools.external.client import ExternalCall

    called: list[tuple[str, str]] = []

    def external_call(tool: str, *, render_data: dict | None = None) -> ExternalCall:
        return ExternalCall(
            tool=tool,
            source="식품의약품안전처",
            status="live",
            summary_text=f"{tool} 상세 근거",
            render_data=render_data or {"items": [{"result": "확인"}]},
            safe_url="https://example.test/mfds",
        )

    class Resolver:
        def resolve(self, _query, *, allow_default):
            assert allow_default is False
            raise LookupError("brand not in mart resolver")

    class External:
        def mfds_permission_search(self, brand):
            called.append(("search", brand))
            return external_call("mfds_permission_search")

        def mfds_patent(self, ingredient):
            called.append(("patent", ingredient))
            return external_call("mfds_patent")

        def mfds_fda_orangebook(self, ingredient):
            called.append(("orangebook", ingredient))
            return external_call("mfds_fda_orangebook")

    dependencies = SimpleNamespace(
        external=External(),
        resolver=Resolver(),
        query_layer=None,
    )
    monkeypatch.setattr(factory, "build_chat_agent_dependencies", lambda **_kwargs: dependencies)
    monkeypatch.setattr(
        general_view_routing.GeneralViewService,
        "from_env",
        lambda _resolver: SimpleNamespace(),
    )

    result = v4_adapters.build_source_adapters()["nedrug"]("리바로정 특허권 등재 현황")

    assert result.status == "ok"
    assert ("search", "리바로정") in called
    assert ("patent", "Pitavastatin") in called
    assert ("orangebook", "Pitavastatin") in called


def test_v4_reimbursement_summary_is_not_dropped_for_title_case_colons() -> None:
    result = SourceResult(
        source="hira",
        query="아일리아 급여기준",
        status="ok",
        payload={
            "calls": [
                {
                    "summary_text": (
                        "Aflibercept 급여기준의 Neovascularization: Review 항목에서 "
                        "투여대상과 투여횟수를 확인했습니다."
                    )
                }
            ]
        },
    )

    answer = _evidence_fallback((result,))

    assert "Neovascularization: Review" in answer
    assert "투여대상과 투여횟수" in answer


def test_v4_gates_prepend_requested_mart_metric_when_synthesis_omits_it() -> None:
    results = (
        SourceResult(
            source="mart",
            query="리바로 매출",
            status="ok",
            payload={
                "calls": [
                    {
                        "summary_text": "리바로 2026-06 UBIST 전략 mart 지표: 매출 85.87억원, MS 3.72%, 순위 6위.",
                        "render_data": {
                            "value": 8587458961.25,
                            "sales_억원": 85.87,
                            "ms_pct": 3.72,
                            "rank": 6,
                            "brand_value_series_10pt": [
                                {"rank": 6, "value_억원": 85.87}
                            ],
                        },
                    }
                ]
            },
        ),
    )

    gated = apply_v4_gates(
        "리바로 매출 알려줘",
        (
            "리바로는 전략시장 내 6위이며 HHI 262.6243인 시장에서 "
            "경쟁 중인 것으로 확인되었습니다."
        ),
        results,
    )

    assert gated.text.startswith("리바로 2026-06 UBIST 전략 mart 지표: 매출 85.87억원")
    assert gated.trace["requested_metric_surface"]["repaired"] is True
    assert "8587458961.25" not in gated.text


def test_v4_surface_detects_raw_won_values() -> None:
    assert _INTERNAL_SURFACE_RE.search("매출은 8587458961.25 KRW입니다.")


def test_v4_surface_detects_raw_won_value_followed_by_korean_particle() -> None:
    assert _INTERNAL_SURFACE_RE.search("매출은 9085877820.15원을 기록했습니다.")


def test_v4_gate_replaces_raw_won_paragraph_with_display_summary() -> None:
    results = (
        SourceResult(
            source="mart",
            query="리바로 매출",
            status="ok",
            payload={
                "calls": [
                    {
                        "summary_text": "리바로 2026-06 UBIST 전략 mart 지표: 매출 85.87억원, MS 3.72%, 순위 6위.",
                        "render_data": {
                            "value": 8587458961.25,
                            "sales_억원": 85.87,
                        },
                    }
                ]
            },
        ),
    )

    gated = apply_v4_gates(
        "리바로 매출 알려줘",
        "리바로는 8587458961.25원을 기록했습니다.\n\n시장 내 입지는 안정적입니다.",
        results,
    )

    assert gated.text.startswith("리바로 2026-06 UBIST 전략 mart 지표: 매출 85.87억원")
    assert "8587458961.25" not in gated.text
    assert gated.trace["surface_raw_won"]["blocked"] is True


def test_v4_gate_replaces_unsupported_raw_mart_percentages_with_display_summary() -> None:
    results = (
        SourceResult(
            source="mart",
            query="리바로 매출",
            status="ok",
            payload={
                "calls": [
                    {
                        "summary_text": "리바로 2026-06 UBIST 전략 mart 지표: 매출 85.87억원, MS 3.72%, 순위 6위.",
                        "render_data": {
                            "sales_억원": 85.87,
                            "ms_pct": 3.7201985208381596,
                            "share_delta_pct": 0.5774,
                        },
                    }
                ]
            },
        ),
    )

    gated = apply_v4_gates(
        "리바로 매출 알려줘",
        (
            "리바로 매출은 85.87억원이고 점유율은 3.7201985208381596%입니다.\n\n"
            "전월 대비 0.5774%p 상승한 것으로 해석됩니다."
        ),
        results,
    )

    assert "85.87억원" in gated.text
    assert "3.72%" in gated.text
    assert "3.7201985208381596%" not in gated.text
    assert "0.5774%p" not in gated.text
    assert gated.trace["surface_mart_percentage"]["blocked"] is True


def test_v4_gate_detects_parenthesized_raw_mart_units() -> None:
    results = (
        SourceResult(
            source="mart",
            query="리바로 매출",
            status="ok",
            payload={
                "calls": [
                    {
                        "summary_text": "리바로 매출은 85.87억원이고 점유율은 3.72%입니다.",
                        "render_data": {
                            "value": 8587458961.25,
                            "sales_억원": 85.87,
                            "ms_pct": 3.7201985208381596,
                        },
                    }
                ]
            },
        ),
    )

    gated = apply_v4_gates(
        "리바로 매출 알려줘",
        (
            "리바로 매출은 8587458961.25(원)입니다.\n\n"
            "리바로 점유율은 3.7201985208381596(%)입니다."
        ),
        results,
    )

    assert "8587458961.25" not in gated.text
    assert "3.7201985208381596" not in gated.text
    assert gated.trace["surface_raw_won"]["blocked"] is True
    assert gated.trace["surface_mart_percentage"]["blocked"] is True


def test_v4_gate_does_not_duplicate_verified_market_history_during_surface_repair() -> None:
    results = (
        SourceResult(
            source="mart",
            query="리바로 추이",
            status="ok",
            payload={
                "calls": [
                    {
                        "render_data": {
                            "brand": "리바로",
                            "brand_value_series_10pt": [
                                {"period": "2022-12", "value_억원": 1743.44},
                                {"period": "2026-06", "value_억원": 2308.33},
                            ],
                        }
                    }
                ]
            },
        ),
    )
    verified = (
        "리바로 매출은 2022년 12월 1743.44억원에서 "
        "2026년 6월 2308.33억원으로 4년간 증가했습니다. [출처: 내부 데이터마트]\n\n"
        "연도별: 2022년 12월 1743.44억원 · 2026년 6월 2308.33억원\n\n"
        "| 기간 | 리바로 매출 | 시장 규모 |\n"
        "| --- | ---: | ---: |\n"
        "| 2022-12 | 1743.44억원 | 확인되지 않음 |\n"
        "| 2026-06 | 2308.33억원 | 확인되지 않음 |"
    )

    gated = apply_v4_gates(
        "리바로가 어떻게 변해왔어?",
        f"{verified}\n\n원시 값은 230833352390.9699원입니다.",
        results,
    )

    assert gated.text.count("리바로 매출은 2022년 12월") == 1
    assert gated.text.count("연도별: 2022년 12월") == 1
    assert "230833352390.9699원" not in gated.text


def test_v4_gate_binds_requested_hira_values_and_never_uses_claim_count_as_patient_count() -> None:
    result = SourceResult(
        source="hira",
        query="D693 환자수와 진료비, 방문일수 2024년",
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
                                "specCnt": "2547",
                                "rvdInsupBrdnAmt": "7193144000",
                                "rvdRpeTamtAmt": "8697604000",
                                "vstDdcnt": "12879",
                                "units": {
                                    "ptntCnt": "명",
                                    "specCnt": "건",
                                    "rvdInsupBrdnAmt": "원",
                                    "rvdRpeTamtAmt": "원",
                                    "vstDdcnt": "일",
                                },
                            },
                            {
                                "inpatOpat": "외래",
                                "ptntCnt": "9231",
                                "specCnt": "44193",
                                "rvdInsupBrdnAmt": "5112850000",
                                "rvdRpeTamtAmt": "6475810000",
                                "vstDdcnt": "43938",
                                "units": {
                                    "ptntCnt": "명",
                                    "specCnt": "건",
                                    "rvdInsupBrdnAmt": "원",
                                    "rvdRpeTamtAmt": "원",
                                    "vstDdcnt": "일",
                                },
                            },
                        ],
                    }
                }
            ]
        },
        evidence=EvidenceEnvelope(
            kind="hira",
            entity_match="EXACT",
            source_scope="KR",
            time_match="MATCH",
            eligible_claims=("patient_count", "cost"),
            causal=False,
        ),
    )

    gated = apply_v4_gates(
        "D693 2024년 환자수와 진료비, 방문일수 알려줘",
        "D693 2024년 입원 환자수는 2,547명입니다. [출처: HIRA]",
        (result,),
    )

    assert "입원 환자수 1,606명" in gated.text
    assert "보험자부담금 7,193,144,000원" in gated.text
    assert "요양급여비용총액 8,697,604,000원" in gated.text
    assert "방문일수 12,879일" in gated.text
    assert "외래 환자수 9,231명" in gated.text
    assert "요양급여비용총액 6,475,810,000원" in gated.text
    assert "방문일수 43,938일" in gated.text
    assert "환자수는 2,547명" not in gated.text
    assert gated.trace["requested_hira_surface"]["repaired"] is True
    assert gated.trace["requested_hira_surface"]["missing_after_repair"] == []


def test_v4_gate_binds_h360_inpatient_and_outpatient_raw_values() -> None:
    result = SourceResult(
        source="hira",
        query="H360 환자수와 진료비 2024년",
        status="ok",
        payload={
            "calls": [
                {
                    "render_data": {
                        "request": {"sickCd": "H360", "year": "2024"},
                        "items": [
                            {
                                "inpatOpat": "입원",
                                "ptntCnt": "2402",
                                "specCnt": "2901",
                                "rvdInsupBrdnAmt": "6281000000",
                                "rvdRpeTamtAmt": "7583990000",
                                "units": {
                                    "ptntCnt": "명",
                                    "specCnt": "건",
                                    "rvdInsupBrdnAmt": "원",
                                    "rvdRpeTamtAmt": "원",
                                },
                            },
                            {
                                "inpatOpat": "외래",
                                "ptntCnt": "385530",
                                "specCnt": "1056591",
                                "rvdInsupBrdnAmt": "105711400000",
                                "rvdRpeTamtAmt": "126707143000",
                                "units": {
                                    "ptntCnt": "명",
                                    "specCnt": "건",
                                    "rvdInsupBrdnAmt": "원",
                                    "rvdRpeTamtAmt": "원",
                                },
                            },
                        ],
                    }
                }
            ]
        },
        evidence=EvidenceEnvelope(
            kind="hira",
            entity_match="EXACT",
            source_scope="KR",
            time_match="MATCH",
            eligible_claims=("patient_count", "cost"),
            causal=False,
        ),
    )

    gated = apply_v4_gates(
        "H360 2024년 환자수와 진료비 알려줘",
        "H360 환자 통계가 확인되었습니다. [출처: HIRA]",
        (result,),
    )

    assert "입원 환자수 2,402명" in gated.text
    assert "외래 환자수 385,530명" in gated.text
    assert "요양급여비용총액 7,583,990,000원" in gated.text
    assert "요양급여비용총액 126,707,143,000원" in gated.text
    assert "환자수 2,901명" not in gated.text
    assert "환자수 1,056,591명" not in gated.text
    assert gated.trace["requested_hira_surface"]["missing_after_repair"] == []


def test_v4_gate_rejects_inpatient_value_bound_to_outpatient_request() -> None:
    result = SourceResult(
        source="hira",
        query="D693 외래 환자수 2024년",
        status="ok",
        payload={
            "calls": [
                {
                    "render_data": {
                        "request": {"sickCd": "D693", "year": "2024"},
                        "items": [
                            {"inpatOpat": "입원", "ptntCnt": "1606"},
                            {"inpatOpat": "외래", "ptntCnt": "9231"},
                        ],
                    }
                }
            ]
        },
        evidence=EvidenceEnvelope(
            kind="hira",
            entity_match="EXACT",
            source_scope="KR",
            time_match="MATCH",
            eligible_claims=("patient_count",),
            causal=False,
        ),
    )

    gated = apply_v4_gates(
        "D693 2024년 외래 환자수 알려줘",
        "D693 2024년 외래 환자수는 1,606명입니다. [출처: HIRA]",
        (result,),
    )

    assert "외래 환자수 9,231명" in gated.text
    assert "외래 환자수는 1,606명" not in gated.text
    assert len(gated.trace["requested_hira_surface"]["expected"]) == 1
    assert gated.trace["requested_hira_surface"]["misbound_patient_values"] == [
        "1606"
    ]


def test_v4_gates_do_not_treat_unrelated_payload_numbers_as_rank_evidence() -> None:
    results = (
        SourceResult(
            source="mart",
            query="리바로 순위",
            status="ok",
            payload={"sales_eok": 85.87, "row_id": 1},
        ),
    )

    gated = apply_v4_gates("리바로 순위 알려줘", "리바로는 1위입니다.", results)

    assert "1위" not in gated.text
    assert gated.trace["mart_numeric_copy_only"]["blocked"] is True


def test_v4_gates_refuse_source_impersonation_and_cross_source_sum() -> None:
    results = (
        SourceResult(
            source="mart",
            query="리바로 IQVIA",
            status="ok",
            payload={"source": "UBIST", "sales_eok": 85.87},
        ),
    )
    impersonated = apply_v4_gates("리바로를 IQVIA 기준으로 보여줘", "85.87억원", results)
    summed = apply_v4_gates(
        "UBIST 랑 IQVIA 합쳐서 총매출 알려줘",
        "합계는 100억원입니다.",
        results,
    )

    assert "IQVIA 근거를 확보하지 못했습니다" in impersonated.text
    assert "합산하지 않습니다" in summed.text


class _FakeV4Runtime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, int]] = []

    def answer(
        self,
        question: str,
        *,
        conversation_id: str | None,
        turns,
        progress_callback=None,
    ) -> V4Answer:
        self.calls.append((question, conversation_id, len(turns)))
        if progress_callback is not None:
            for name, detail in (
                ("질문 해석", "질문을 해석했습니다"),
                ("조회 계획", "확장 각도 3개 · 조회 경로 7개"),
                ("자료 수집 중", "건강보험심사평가원 완료"),
                ("답변 작성 중", "확인된 근거를 종합합니다"),
            ):
                progress_callback({"name": name, "detail": detail})
        return V4Answer(
            text="V4 자유 답변\n\n## 출처\n- mart",
            charts=(),
            sources=("mart",),
            trace={"v4": True},
            timing={"total_elapsed_ms": 1.0},
            conversation_id=conversation_id or "v4-conversation",
        )


def test_flag_on_chat_answer_bypasses_legacy_answer_and_finalizer(monkeypatch) -> None:
    runtime = _FakeV4Runtime()
    monkeypatch.setenv("V4_PLANNER", "on")
    monkeypatch.setattr(service_app, "_get_v4_runtime", lambda: runtime)

    def legacy_must_not_run(*_args, **_kwargs):
        raise AssertionError("legacy path was called")

    monkeypatch.setattr(service_app, "_answer_question", legacy_must_not_run)
    monkeypatch.setattr(service_app, "_compute_final_answer_with_query_spec", legacy_must_not_run)
    client = TestClient(service_app.create_app())

    response = client.post("/chat/answer", json={"question": "리바로 요즘 어때"})

    assert response.status_code == 200
    assert response.json()["text"].startswith("V4 자유 답변")
    assert runtime.calls == [("리바로 요즘 어때", None, 0)]


def test_flag_off_chat_answer_is_identical_to_legacy_route(monkeypatch) -> None:
    monkeypatch.setenv("V4_PLANNER", "off")
    monkeypatch.setattr(
        service_app,
        "_get_v4_runtime",
        lambda: (_ for _ in ()).throw(AssertionError("V4 imported while off")),
        raising=False,
    )

    class Agent:
        def __init__(self, *, external_mode: str = "live") -> None:
            self.external_mode = external_mode

        def answer(self, question: str, _documents=None, **_kwargs):
            return {"answer": f"legacy:{question}", "sources": [], "tool_calls": []}

    monkeypatch.setattr(
        service_app.GenosClient,
        "stream_answer",
        lambda _self, _question, result: iter((result["answer"],)),
    )
    app = service_app.create_app(agent_factory=lambda external_mode="live": Agent(external_mode=external_mode))
    response = TestClient(app).post("/chat/answer", json={"question": "기존 경로"})

    assert response.status_code == 200
    assert response.json()["text"] == "legacy:기존 경로"


def test_flag_on_chat_session_replays_v4_answer_over_existing_sse(monkeypatch) -> None:
    runtime = _FakeV4Runtime()
    recorded: list[str] = []

    class History:
        def recent_turns(self, _conversation_id: str, _limit: int):
            return ()

        def record_turn(self, **kwargs) -> None:
            recorded.append(kwargs["answer_text"])

    monkeypatch.setenv("V4_PLANNER", "on")
    monkeypatch.setattr(service_app, "_get_v4_runtime", lambda: runtime)
    client = TestClient(service_app.create_app(history_store=History()))

    accepted = client.post("/chat", json={"question": "리바로 요즘 어때"})
    streamed = client.get(
        "/chat/stream",
        params={"session_id": accepted.json()["session_id"]},
    )

    assert accepted.status_code == 200
    assert streamed.status_code == 200
    assert "V4 자유 답변" in streamed.text
    assert "event: done" in streamed.text
    assert recorded == ["V4 자유 답변\n\n## 출처\n- mart"]


def test_flag_on_live_stream_emits_progress_before_running_v4(monkeypatch) -> None:
    runtime = _FakeV4Runtime()
    monkeypatch.setenv("V4_PLANNER", "on")
    monkeypatch.setattr(service_app, "_get_v4_runtime", lambda: runtime)
    app = service_app.create_app()
    client = TestClient(app)

    with client.stream(
        "GET",
        "/chat/stream",
        params={"question": "리바로 요즘 어때"},
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert body.index("event: step") < body.index("V4 자유 답변")
    assert body.index('"name":"질문 해석 중"') < body.index('"name":"질문 해석"')
    assert body.index("질문 해석") < body.index("조회 계획")
    assert body.index("조회 계획") < body.index("자료 수집 중")
    assert body.index("자료 수집 중") < body.index("답변 작성 중")
    assert runtime.calls == [("리바로 요즘 어때", None, 0)]
