from __future__ import annotations

from types import SimpleNamespace

from jw_chat_agent_poc.service.v4.contracts import (
    EvidenceEnvelope,
    PlannerOutput,
    SourceResult,
    ToolQueries,
)
from jw_chat_agent_poc.service.v4.gates import apply_v4_gates
from jw_chat_agent_poc.service.v4.runtime import V4Runtime
from jw_chat_agent_poc.service.v4.session_state import SessionState
from jw_chat_agent_poc.service.v4.synthesizer import SynthesisOutcome


def _plan(question: str) -> PlannerOutput:
    return PlannerOutput(
        resolved_question=question,
        expanded_intents=(question,),
        answer_sources=("hira",),
        tool_queries=ToolQueries(
            mart=(question,),
            nedrug=(question,),
            hira=(question,),
            openfda=(question,),
            clinicaltrials=(question,),
            web=(question,),
            patent=(question,),
        ),
        linking_plan="single wave",
    )


def _confirmed_absence() -> SourceResult:
    return SourceResult(
        source="hira",
        query="마운자로 급여기준",
        status="empty",
        payload={
            "calls": [],
            "document_lookup": {
                "document": "reimbursement",
                "outcome": "confirmed_absent",
                "subject": "마운자로",
                "error_code": "REALTIME_NO_EVIDENCE",
            },
        },
    )


def _trusted_web() -> SourceResult:
    return SourceResult(
        source="web",
        query="마운자로 급여 협상",
        status="ok",
        payload={
            "items": [
                {
                    "url": "https://www.yna.co.kr/view/example",
                    "title": "마운자로 급여 협상 결렬 뒤 재신청",
                    "published_date": "2024-10-25",
                }
            ]
        },
    )


class _Planner:
    def plan_with_trace(self, question, _turns, *, budget_s):
        return SimpleNamespace(
            plan=_plan(question),
            trace={"elapsed_ms": 1.0, "usage": {}},
        )

    def link(self, *_args, **_kwargs):
        return None


class _AbsenceSynthesizer:
    def synthesize_with_trace(self, _plan, _results, _turns, *, budget_s):
        return SynthesisOutcome(
            text=(
                "## 핵심 답\n"
                "마운자로는 현재 급여기준이 없습니다(비급여). [출처: HIRA]"
            ),
            trace={"elapsed_ms": 1.0},
        )


def test_r10c_typed_absence_claim_survives_final_claim_gate() -> None:
    class Executor:
        def execute_with_trace(self, _plan, **kwargs):
            results = (
                (_trusted_web(),)
                if kwargs.get("source_filter") == ("web",)
                else (_confirmed_absence(), _trusted_web())
            )
            return SimpleNamespace(
                results=results,
                trace={"elapsed_ms": 1.0, "tools": [], "session_result_reused": False},
            )

    answer = V4Runtime(
        planner=_Planner(),
        executor=Executor(),
        synthesizer=_AbsenceSynthesizer(),
    ).answer("마운자로 급여기준", conversation_id="r10c-absence", turns=())

    hira = next(result for result in answer.trace["tool_results"] if result["source"] == "hira")
    assert hira["payload"]["absence_confirmation"] == {
        "source": "hira",
        "doc_type": "reimbursement",
        "status": "confirmed_absent",
        "subject": "마운자로",
    }
    assert "현재 급여기준이 없습니다(비급여)" in answer.text
    assert "사용 가능한 출처를 확보하지 못했습니다" not in answer.text
    assert "고시 무결과 확인" in answer.text
    assert answer.sources == ("hira",)
    assert answer.trace["gates"]["claim_eligibility_guard"]["blocked"] is False


def test_r10c_first_wave_web_context_skips_supplemental_call() -> None:
    class Executor:
        def __init__(self) -> None:
            self.filters: list[tuple[str, ...] | None] = []

        def execute_with_trace(self, _plan, **kwargs):
            source_filter = kwargs.get("source_filter")
            self.filters.append(source_filter)
            if source_filter is not None:
                raise AssertionError("trusted first-wave web must prevent a supplemental call")
            return SimpleNamespace(
                results=(_confirmed_absence(), _trusted_web()),
                trace={"elapsed_ms": 1.0, "tools": [], "session_result_reused": False},
            )

    executor = Executor()
    answer = V4Runtime(
        planner=_Planner(),
        executor=executor,
        synthesizer=_AbsenceSynthesizer(),
    ).answer("마운자로 급여기준", conversation_id="r10c-first-web", turns=())

    web = next(result for result in answer.trace["tool_results"] if result["source"] == "web")
    assert executor.filters == [None]
    assert web["payload"]["absence_context"]["official_absence"] is True


def test_r10c_absence_context_surfaces_observed_web_publication_date() -> None:
    from jw_chat_agent_poc.service.v4.runtime import _tag_absence_context
    from jw_chat_agent_poc.service.v4.synthesizer import _append_absence_context_surface

    tagged = _tag_absence_context(
        _trusted_web(),
        {
            "source": "hira",
            "document": "reimbursement",
            "subject": "마운자로",
            "query": "마운자로 급여기준",
        },
    )

    answer = _append_absence_context_surface("## 핵심 답\n확인 중입니다.", (tagged,))

    assert "2024-10-25 게시된" in answer
    assert "협상 결렬" in answer
    assert "보도되고 있습니다" in answer


def test_r10c_confirmed_absence_surfaces_without_web_context() -> None:
    from jw_chat_agent_poc.service.v4.runtime import _tag_absence_confirmation
    from jw_chat_agent_poc.service.v4.synthesizer import V4Synthesizer

    confirmed = _tag_absence_confirmation(
        _confirmed_absence(),
        {
            "source": "hira",
            "document": "reimbursement",
            "subject": "마운자로",
            "query": "마운자로 급여기준",
        },
    )

    answer = V4Synthesizer(object()).synthesize(_plan("마운자로 급여기준"), (confirmed,), ())

    assert answer.startswith(
        "## 핵심 답\n마운자로는 현재 급여기준이 없습니다(비급여). [출처: HIRA]"
    )
    assert "확인된 근거가 없어" not in answer


def test_r10c_untyped_empty_result_cannot_support_absence_claim() -> None:
    untyped = _confirmed_absence().model_copy(
        update={
            "evidence": EvidenceEnvelope(
                kind="hira",
                entity_match="EXACT",
                source_scope="KR",
                time_match="NOT_REQUESTED",
                eligible_claims=("reimbursement", "absence_confirmation"),
            )
        }
    )

    gated = apply_v4_gates(
        "마운자로 급여기준",
        "## 핵심 답\n마운자로는 현재 급여기준이 없습니다(비급여). [출처: HIRA]",
        (untyped,),
    )

    assert gated.trace["claim_eligibility_guard"]["blocked"] is True


def test_r10c_reimbursement_absence_cannot_support_approval_absence() -> None:
    from jw_chat_agent_poc.service.v4.runtime import _tag_absence_confirmation

    confirmed = _tag_absence_confirmation(
        _confirmed_absence(),
        {
            "source": "hira",
            "document": "reimbursement",
            "subject": "마운자로",
            "query": "마운자로 급여기준",
        },
    )

    gated = apply_v4_gates(
        "마운자로 급여기준",
        "## 핵심 답\n마운자로는 현재 허가 문서를 확인할 수 없습니다. [출처: HIRA]",
        (confirmed,),
    )

    assert gated.trace["claim_eligibility_guard"]["blocked"] is True
    assert "현재 허가 문서를 확인할 수 없습니다" not in gated.text


def test_r10c_reexamination_date_is_reusable_across_runtime_state() -> None:
    from jw_chat_agent_poc.service.v4.runtime import (
        _derive_session_state,
        _results_from_session_state,
    )

    source = SourceResult(
        source="nedrug",
        query="리바로젯 재심사일",
        status="ok",
        payload={
            "calls": [
                {
                    "render_data": {
                        "ITEM_NAME": "리바로젯정",
                        "REEXAM_DATE": "2021-07-28~2027-07-27",
                    }
                }
            ]
        },
    )

    state = _derive_session_state(
        "리바로젯 재심사일 알려줘",
        _plan("리바로젯 재심사일 알려줘"),
        (source,),
        previous=SessionState(),
    )
    restored = _results_from_session_state(SessionState.from_value(state.public_dict()))

    date_facts = [
        fact
        for fact in state.last_numeric_facts
        if str(fact.get("path", "")).endswith("REEXAM_DATE")
    ]
    assert date_facts == [
        {
            "source": "nedrug",
            "path": "calls[0].render_data.REEXAM_DATE",
            "column": "REEXAM_DATE",
            "row_path": "calls[0].render_data",
            "value": "2021-07-28~2027-07-27",
            "value_type": "date",
        }
    ]
    assert restored[0].payload["last_numeric_facts"][0]["value"] == (
        "2021-07-28~2027-07-27"
    )
