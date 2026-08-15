from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.file_search_client import UploadedFileSearchResult
from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, ToolQueries
from jw_chat_agent_poc.service.v4.document_lane import (
    build_document_source_result,
    render_document_overview,
)
from jw_chat_agent_poc.service.v4.evidence_sets import build_evidence_sets
from jw_chat_agent_poc.service.v4.inspection import build_inspection_detail
from jw_chat_agent_poc.service.v4.lossless_contracts import DeterministicRender
from jw_chat_agent_poc.service.v4.runtime import V4Runtime
from jw_chat_agent_poc.service.v4.synthesizer import _synthesis_messages


def _plan() -> PlannerOutput:
    return PlannerOutput(
        resolved_question="리바로 시장을 설명해줘",
        expanded_intents=("시장 설명",),
        tool_queries=ToolQueries(
            mart=("리바로",),
            nedrug=("리바로",),
            hira=("리바로",),
            openfda=("리바로",),
            clinicaltrials=("pitavastatin",),
            web=("리바로",),
            patent=("리바로",),
        ),
        linking_plan="없음",
    )


def _uploaded() -> UploadedFileSearchResult:
    return UploadedFileSearchResult(
        file_context=(
            "[1] Disease Analysis RA.pdf (page=1)\n"
            "섹션: Marketed and Pipeline Drugs\n"
            "Copyright 2026 Example Publisher\n"
            "1\n"
            "이 문서는 당뇨망막병증 시장과 파이프라인을 분석합니다.\n\n"
            "[2] Disease Analysis RA.pdf (page=3)\n"
            "섹션: Key Takeaways\n"
            "바이오시밀러 경쟁과 신규 기전 후보를 핵심으로 다룹니다."
        ),
        file_sources=("Disease Analysis RA.pdf",),
        errors=(),
        file_source_items=(
            {
                "file_name": "Disease Analysis RA.pdf",
                "document_id": 91,
                "i_page": 1,
                "section_title": "Marketed and Pipeline Drugs",
            },
            {
                "file_name": "Disease Analysis RA.pdf",
                "document_id": 91,
                "i_page": 3,
                "section_title": "Key Takeaways",
            },
        ),
        has_active_file=True,
    )


def test_p3_1_active_document_is_fanned_into_general_v4_question(monkeypatch) -> None:
    captured = {}

    class Runtime:
        def answer(self, question, **kwargs):
            captured["question"] = question
            captured["supplemental_results"] = kwargs["supplemental_results"]
            return SimpleNamespace(
                text="문서와 시장 근거를 함께 설명했습니다.",
                charts=(),
                timing={},
                trace={"v4": True},
                sources=("내부 데이터마트", "업로드 문서"),
                conversation_id="p3-general",
            )

    monkeypatch.setattr(service_app, "_delegated_file_context", lambda *_a, **_k: (
        _uploaded().file_context,
        _uploaded().file_source_items,
        True,
        "",
        (),
    ))
    monkeypatch.setattr(service_app, "_get_v4_runtime", Runtime)

    answer = service_app._run_v4_final_answer(
        service_app.SessionStore(),
        None,
        "리바로 시장을 설명해줘",
        "p3-general",
    )

    assert answer.text.startswith("문서와 시장")
    assert len(captured["supplemental_results"]) == 1
    document = captured["supplemental_results"][0]
    assert document.source == "document"
    assert document.payload["returned_chunk_count"] == 2
    assert document.payload["used_chunk_count"] == 2


def test_p3_2_document_overview_strips_display_noise_without_dumping_chunks() -> None:
    result = build_document_source_result("pdf 설명해줘", _uploaded())
    answer = render_document_overview(result)

    assert "Disease Analysis RA.pdf" in answer
    assert "Marketed and Pipeline Drugs" in answer
    assert "Key Takeaways" in answer
    assert "당뇨망막병증 시장과 파이프라인" in answer
    assert "Copyright" not in answer
    assert "섹션:" not in answer
    assert "[1]" not in answer
    assert "\n1\n" not in answer
    assert (
        "[출처: 업로드 문서 · Disease Analysis RA.pdf · "
        "Marketed and Pipeline Drugs · p.1]"
    ) in answer


def test_p3_2_live_document_noise_is_not_exposed() -> None:
    uploaded = UploadedFileSearchResult(
        file_context=(
            "[1] Datamonitor_DM-DiabetesType2-2026-02-23.pdf "
            "(document_id=117843) (page=8)\n"
            "섹션: Source:\n"
            "# Treatment\n\n"
            "[2] TEMP_DOCUMENT_5845.pdf (page=1)\n"
            "[DA] 문서: TEMP_DOCUMENT_5845.pdf | p.1 "
            "# Disease Analysis: Diabetes Type 2 Last Reviewed: 17 Nov, 2025\n\n"
            "[3] TEMP_DOCUMENT_5845.pdf (page=108)\n"
            "<!-- Start of picture text --> Citeline powers a full suite<br>of services.\n"
            "Copyright 2026 Citeline\n"
        ),
        file_sources=("Datamonitor_DM-DiabetesType2-2026-02-23.pdf",),
        errors=(),
        file_source_items=(
            {
                "file_name": "Datamonitor_DM-DiabetesType2-2026-02-23.pdf",
                "document_id": 117843,
                "i_page": 8,
                "section_title": "Source:",
            },
        ),
        has_active_file=True,
    )

    answer = render_document_overview(build_document_source_result("pdf 설명해줘", uploaded))

    assert "Datamonitor_DM-DiabetesType2-2026-02-23.pdf" in answer
    assert "Disease Analysis: Diabetes Type 2" in answer
    assert "document_id" not in answer
    assert "TEMP_DOCUMENT" not in answer
    assert "Start of picture text" not in answer
    assert "Copyright" not in answer
    assert "<br>" not in answer
    assert "Source:" not in answer


def test_p3_3_document_lane_uses_shared_evidence_and_inspection_contract() -> None:
    result = build_document_source_result("리바로 시장을 설명해줘", _uploaded())
    evidence_sets = build_evidence_sets(_plan(), (result,), observed_on=date(2026, 8, 14))
    inspection = build_inspection_detail(
        _plan(),
        (result,),
        evidence_sets,
        DeterministicRender(profile="market_analysis"),
        answer_text="업로드 문서와 시장 자료를 함께 비교했습니다.",
    )

    assert len(evidence_sets) == 1
    assert evidence_sets[0].source == "document"
    assert len(evidence_sets[0].records) == 2
    call = inspection["calls"][0]
    assert call["source_label"] == "업로드 문서"
    assert call["request_parameters"]["query"] == "리바로 시장을 설명해줘"
    assert call["counts"]["returned"] == 2
    assert call["counts"]["used"] == 2
    assert call["document_names"] == ["Disease Analysis RA.pdf"]

    messages = _synthesis_messages(_plan(), (result,), ())
    prompt = messages[-1]["content"]
    assert '"compare_with_other_sources_in_the_same_paragraph": true' in prompt
    assert '"per_source_paragraph_dump_forbidden": true' in prompt


def test_p3_active_document_with_zero_hits_remains_visible_to_inspection(monkeypatch) -> None:
    captured = {}

    class Runtime:
        def answer(self, question, **kwargs):
            captured["supplemental_results"] = kwargs["supplemental_results"]
            return SimpleNamespace(
                text="시장 근거만 답변했습니다.", charts=(), timing={}, trace={},
                sources=("내부 데이터마트",), conversation_id="p3-empty",
            )

    monkeypatch.setattr(service_app, "_delegated_file_context", lambda *_a, **_k: (
        None, (), True, "", (),
    ))
    monkeypatch.setattr(service_app, "_get_v4_runtime", Runtime)

    service_app._run_v4_final_answer(
        service_app.SessionStore(), None, "리바로 시장을 설명해줘", "p3-empty"
    )

    assert captured["supplemental_results"][0].source == "document"
    assert captured["supplemental_results"][0].status == "empty"


def test_p3_document_result_survives_the_complete_v4_runtime_path() -> None:
    plan = _plan()
    document = build_document_source_result("리바로 시장을 설명해줘", _uploaded())

    class Planner:
        def plan(self, _question, _turns, *, budget_s):
            return plan

        def link(self, *_args, **_kwargs):
            return None

    class Executor:
        def execute(self, _plan, **_kwargs):
            return ()

    class Synthesizer:
        def synthesize(self, _plan, results, _turns, *, budget_s):
            assert any(result.source == "document" for result in results)
            return (
                "업로드 문서는 당뇨망막병증 시장을 다룹니다. "
                "[출처: 업로드 문서]"
            )

    answer = V4Runtime(
        planner=Planner(),
        executor=Executor(),
        synthesizer=Synthesizer(),
    ).answer(
        "리바로 시장을 설명해줘",
        conversation_id="p3-runtime",
        turns=(),
        supplemental_results=(document,),
    )

    document_calls = [
        call
        for call in answer.trace["inspection_detail"]["calls"]
        if call["source_label"] == "업로드 문서"
    ]
    assert len(document_calls) == 1
    assert document_calls[0]["counts"]["returned"] == 2
    assert document_calls[0]["counts"]["used"] == 2
    assert "업로드 문서" in answer.sources
