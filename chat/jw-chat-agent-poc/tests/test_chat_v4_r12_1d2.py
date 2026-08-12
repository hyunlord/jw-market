from __future__ import annotations

from datetime import date
import re
import time

from jw_chat_agent_poc.service.v4.contracts import (
    SOURCE_NAMES,
    PlannerOutput,
    SourceResult,
    ToolQueries,
)
from jw_chat_agent_poc.service.v4.executor import ParallelSourceExecutor
from jw_chat_agent_poc.service.v4.lossless_spine import (
    build_evidence_sets,
    compose_lossless_answer,
    render_deterministic_facts,
)
from jw_chat_agent_poc.service.v4.runtime import _soft_deadline_exempt_sources


def _plan(question: str, *, answer_sources: tuple[str, ...]) -> PlannerOutput:
    return PlannerOutput(
        resolved_question=question,
        expanded_intents=(question,),
        answer_sources=answer_sources,
        tool_queries=ToolQueries(
            **{source: (f"{question} {source}",) for source in SOURCE_NAMES}
        ),
        linking_plan="first hop is sufficient",
    )


def _policy_result() -> SourceResult:
    return SourceResult(
        source="hira",
        query="리바로젯 급여기준",
        status="ok",
        payload={
            "calls": [
                {
                    "tool": "hira_reimbursement_detail",
                    "status": "ok",
                    "safe_url": "https://www.hira.or.kr/criterion/101",
                    "render_data": {
                        "notice_number": "고시 제2026-101호",
                        "title": "리바로젯 급여기준",
                        "raw_text": "투여대상 환자\n제외기준 없음",
                        "source_url": "https://www.hira.or.kr/criterion/101",
                    },
                }
            ]
        },
    )


def _openfda_result() -> SourceResult:
    return SourceResult(
        source="openfda",
        query="리바로젯 label safety",
        status="ok",
        payload={
            "calls": [
                {
                    "tool": "openfda_label_search",
                    "status": "live",
                    "summary_text": "label result",
                    "render_data": {
                        "items": [{"title": "Pitavastatin label"}],
                    },
                    "safe_url": "https://open.fda.gov/apis/drug/label/",
                },
                {
                    "tool": "openfda_label_search",
                    "status": "no_data",
                    "summary_text": "search_drug_labels MCP returned no results",
                    "render_data": {"items": []},
                },
            ]
        },
    )


def _web_result() -> SourceResult:
    return SourceResult(
        source="web",
        query="리바로젯 급여기준",
        status="ok",
        payload={
            "calls": [
                {
                    "tool": "web_search",
                    "status": "live",
                    "summary_text": "web result",
                    "render_data": {
                        "title": "리바로젯 급여 관련 보도",
                        "snippet": "급여 관련 공개 보도",
                    },
                }
            ]
        },
    )


def test_explicit_domain_sources_are_exempt_from_soft_deadline_and_trace_is_typed() -> None:
    def adapter(source: str, query: str) -> SourceResult:
        time.sleep({"hira": 0.005, "patent": 0.08}.get(source, 0.2))
        return SourceResult(source=source, query=query, status="ok", payload={"source": source})

    executor = ParallelSourceExecutor(
        adapters={
            source: (lambda query, source=source: adapter(source, query))
            for source in SOURCE_NAMES
        },
        per_tool_timeout_s=0.3,
        total_timeout_s=0.3,
    )
    outcome = executor.execute_with_trace(
        _plan("리바로젯 특허현황", answer_sources=("hira",)),
        session_id="r12-1d2-soft-deadline",
        answer_sources=("hira",),
        soft_deadline_s=0.02,
        soft_deadline_exempt_sources=("patent",),
    )

    assert next(result for result in outcome.results if result.source == "patent").status == "ok"
    web = next(result for result in outcome.results if result.source == "web")
    assert web.status == "timeout"
    assert web.notice == "정답 근거 도착 후 soft deadline으로 미포함"
    web_trace = next(item for item in outcome.trace["tools"] if item["source"] == "web")
    patent_trace = next(item for item in outcome.trace["tools"] if item["source"] == "patent")
    assert web_trace["exclusion_reason"] == "soft_deadline_after_answer_quorum"
    assert web_trace["notice"] == web.notice
    assert patent_trace["soft_deadline_exempt"] is True


def test_domain_signal_mapping_treats_generic_clinical_as_clinical_and_patent() -> None:
    assert _soft_deadline_exempt_sources("리바로젯 특허현황") == ("patent",)
    assert _soft_deadline_exempt_sources("리바로젯 제네릭 임상현황") == (
        "clinicaltrials",
        "patent",
    )
    assert _soft_deadline_exempt_sources("리바로젯 급여기준") == ()


def test_failures_are_surfaced_with_public_source_and_specific_reason() -> None:
    plan = _plan("리바로젯 급여기준", answer_sources=("hira",))
    results = (
        _policy_result(),
        SourceResult(
            source="patent",
            query="리바로젯 급여기준 특허",
            status="timeout",
            notice="정답 근거 도착 후 soft deadline으로 미포함",
        ),
        SourceResult(
            source="web",
            query="리바로젯 급여기준",
            status="empty",
            notice="exceeds your plan's set usage limit",
        ),
    )
    rendered = render_deterministic_facts(
        plan,
        build_evidence_sets(plan, results, observed_on=date(2026, 8, 13)),
        observed_on=date(2026, 8, 13),
    )
    composed = compose_lossless_answer(
        rendered,
        "## 핵심 답\n급여기준을 확인했습니다.",
        synthesis_trace={"status": "synthesized"},
        mode="inject",
    )

    assert "## 미확인 요소" in composed.text
    assert "식품의약품안전처 의약품 특허목록" in composed.text
    assert "응답 시간 내 도착하지 않아 이번 답변에서 제외" in composed.text
    assert "웹 뉴스" in composed.text
    assert "사용량 한도 초과" in composed.text
    assert "soft deadline" not in composed.text


def test_quota_empty_result_keeps_provider_quota_reason_in_trace() -> None:
    executor = ParallelSourceExecutor(
        adapters={
            source: lambda query, source=source: SourceResult(
                source=source,
                query=query,
                status="empty",
                notice="exceeds your plan's set usage limit",
            )
            for source in SOURCE_NAMES
        }
    )
    outcome = executor.execute_with_trace(
        _plan("리바로젯 특허현황", answer_sources=("web",)),
        session_id="r12-1d2-quota-trace",
        source_filter=("web",),
    )

    assert outcome.trace["tools"][0]["exclusion_reason"] == "provider_quota"


def test_policy_profile_keeps_primary_and_all_nonempty_auxiliary_sources_bound() -> None:
    plan = _plan("리바로젯 급여기준", answer_sources=("hira",))
    evidence_sets = build_evidence_sets(
        plan,
        (_policy_result(), _openfda_result(), _web_result()),
        observed_on=date(2026, 8, 13),
    )
    rendered = render_deterministic_facts(
        plan,
        evidence_sets,
        observed_on=date(2026, 8, 13),
    )
    rendered_ids = {
        record_id for node in rendered.nodes for record_id in node.record_ids
    }
    evidence_ids = {
        record.evidence_id
        for evidence_set in evidence_sets
        for record in evidence_set.records
    }
    composed = compose_lossless_answer(
        rendered,
        "## 핵심 답\n급여기준을 확인했습니다.",
        synthesis_trace={"status": "synthesized"},
        mode="inject",
    )

    assert rendered.profile == "policy_document"
    assert rendered_ids == evidence_ids
    assert rendered.coverage.records_rendered == len(rendered_ids)
    assert rendered.coverage.records_unique == len(evidence_ids)
    assert composed.trace["rendered_table_rows"] == composed.trace["lossless_records_rendered"]
    assert composed.trace["lossless_records_rendered"] == len(evidence_ids)
    assert "## FDA 보조 자료" in composed.text
    assert "## 웹 뉴스 보조 자료" in composed.text
    assert composed.text.index("## 고시 정보") < composed.text.index("## FDA 보조 자료")
    assert composed.text.index("## FDA 보조 자료") < composed.text.index("## 웹 뉴스 보조 자료")
    assert re.search(
        r"(?i)(?:openfda_label_search|search_drug_labels|web_search|mcp_[a-z0-9_]+)",
        composed.text,
    ) is None
