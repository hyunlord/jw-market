from __future__ import annotations

from datetime import date

import pytest

from jw_chat_agent_poc.service.v4.contracts import (
    PlannerOutput,
    QueryScope,
    SourceResult,
    ToolQueries,
)
from jw_chat_agent_poc.service.v4.deterministic_render import render_deterministic_facts
from jw_chat_agent_poc.service.v4.evidence_set_support import generic_evidence_set
from jw_chat_agent_poc.service.v4.executor import ParallelSourceExecutor
from jw_chat_agent_poc.service.v4.expansion import expand_parameter_axes
from jw_chat_agent_poc.service.v4.inspection import _public_identifiers
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    CoverageLedger,
    EvidenceRecord,
    EvidenceSet,
)
from jw_chat_agent_poc.service.v4.lossless_spine import (
    build_lossless_render,
    compose_lossless_answer,
)
from jw_chat_agent_poc.service.v4.runtime import (
    _exclude_first_hop_queries,
    _query_scope_notice,
)
from jw_chat_agent_poc.service.v4.render_document import DOCUMENT_EXCERPT_LIMIT
from jw_chat_agent_poc.service.v4.synthesizer import _inject_deterministic_market_surface


def _plan(question: str, *, sources: tuple[str, ...]) -> PlannerOutput:
    return PlannerOutput(
        resolved_question=question,
        expanded_intents=(question,),
        answer_sources=sources,
        tool_queries=ToolQueries(
            mart=(question,),
            nedrug=(question,),
            hira=(question,),
            openfda=(question,),
            clinicaltrials=(question,),
            web=(question,),
            patent=(question,),
        ),
        linking_plan="deterministic",
    )


def _set(source: str, *payloads: dict[str, object]) -> EvidenceSet:
    records = tuple(
        EvidenceRecord(
            evidence_id=f"{source}:{index}",
            source=source,
            result_kind=f"{source}_record",
            payload={**payload, "evidence_id": f"{source}:{index}"},
        )
        for index, payload in enumerate(payloads, start=1)
    )
    return EvidenceSet(
        source=source,
        query_spec=(f"{source} internal generated query",),
        retrieved_at="2026-08-17T00:00:00Z",
        coverage=CoverageLedger(
            total_reported=len(records),
            records_received=len(records),
            records_unique=len(records),
            records_relevant=len(records),
        ),
        records=records,
    )


def test_market_profile_dispatches_every_populated_source_set() -> None:
    evidence_sets = (
        _set(
            "mart",
            {
                "brand": "리바로",
                "period": "2026-01",
                "sales_krw": 12_300_000_000,
                "market_share": 5.4,
                "rank": 2,
            },
        ),
        _set(
            "hira",
            {"sickCd": "E10", "year": "2023", "patient_count": 3210},
        ),
        _set(
            "nedrug",
            {
                "item_name": "리바로정",
                "company": "제이더블유중외제약",
                "active_ingredient": "Pitavastatin",
                "approval_date": "2020-01-02",
            },
        ),
        _set(
            "web",
            {
                "title": "시장 동향",
                "publisher": "공개 매체",
                "published_at": "2026-08-17",
                "url": "https://example.com/article",
            },
        ),
    )

    rendered = render_deterministic_facts(
        _plan("리바로 2026년 월별 매출", sources=("mart",)),
        evidence_sets,
        observed_on=date(2026, 8, 17),
    )

    assert rendered.profile == "market_analysis"
    assert rendered.coverage.records_rendered == 4
    assert {record_id for node in rendered.nodes for record_id in node.record_ids} == {
        "mart:1",
        "hira:1",
        "nedrug:1",
        "web:1",
    }
    assert "123" in rendered.text
    assert "12,300,000,000원" not in rendered.text
    assert "환자수" in rendered.text
    assert "리바로정" in rendered.text
    assert "시장 동향" in rendered.text


def test_clinical_portfolio_keeps_all_23_control_records() -> None:
    records = tuple(
        {
            "nct_id": f"NCT{i:08d}",
            "brief_title": f"대조 임상 {i}",
            "overall_status": "RECRUITING",
            "phase": "PHASE3",
            "sponsor": "공개 의뢰자",
        }
        for i in range(1, 24)
    )

    rendered = render_deterministic_facts(
        _plan("리바로젯 제네릭 임상현황", sources=("clinicaltrials",)),
        (_set("clinicaltrials", *records),),
        observed_on=date(2026, 8, 17),
    )

    assert rendered.profile == "clinical_portfolio"
    assert rendered.coverage.records_rendered == 23
    assert len({record_id for node in rendered.nodes for record_id in node.record_ids}) == 23


def test_mart_monthly_series_becomes_one_lossless_record_per_period() -> None:
    result = SourceResult(
        source="mart",
        query="리바로 2026년 월별 매출",
        status="ok",
        payload={
            "calls": [
                {
                    "render_data": {
                        "brand": "리바로",
                        "brand_value_series_10pt": [
                            {
                                "period": "2026-01",
                                "value_억원": 83.03,
                                "ms_pct": 3.81,
                                "rank": 7,
                            },
                            {
                                "period": "2026-02",
                                "value_억원": 84.25,
                                "ms_pct": 3.92,
                                "rank": 6,
                            },
                        ],
                    }
                }
            ]
        },
    )

    evidence_set = generic_evidence_set("mart", (result,), date(2026, 8, 17))

    assert len(evidence_set.records) == 2
    assert [record.payload["period"] for record in evidence_set.records] == [
        "2026-01",
        "2026-02",
    ]
    assert [record.payload["sales_krw"] for record in evidence_set.records] == [
        8_303_000_000,
        8_425_000_000,
    ]
    assert evidence_set.records[0].payload["market_share"] == 3.81
    assert evidence_set.records[0].payload["rank"] == 7


def test_market_fact_surface_is_injected_after_commentary() -> None:
    rendered = render_deterministic_facts(
        _plan("리바로 2026년 월별 매출", sources=("mart",)),
        (_set(
            "mart",
            {"brand": "리바로", "period": "2026-01", "sales_krw": 10_000_000_000},
        ),),
        observed_on=date(2026, 8, 17),
    )

    composed = compose_lossless_answer(
        rendered,
        "## 핵심 답\n리바로의 월별 매출을 확인했습니다.",
        synthesis_trace={"status": "synthesized", "prompt_chars": 1234},
        mode="inject",
    )

    assert composed.text.index("리바로의 월별 매출") < composed.text.index("## 시장 데이터")
    assert composed.trace["facts_injected_after_synthesis"] is True
    assert composed.trace["synthesis_prompt_chars"] == 1234


def test_runtime_can_defer_legacy_market_surface_to_final_composition() -> None:
    result = SourceResult(
        source="mart",
        query="리바로 매출",
        status="ok",
        payload={
            "calls": [
                {
                    "entity_bundle": {
                        "anchor": "리바로",
                        "period_start": "2026-01",
                        "period_end": "2026-01",
                        "same_period_and_denominator": True,
                        "members": [
                            {
                                "brand": "리바로",
                                "company": "JW중외제약",
                                "rank": 1,
                                "role": "target",
                                "render_data": {},
                            }
                        ],
                    }
                }
            ]
        },
    )

    answer, trace = _inject_deterministic_market_surface(
        "## 핵심 답\n리바로 매출을 확인했습니다.",
        (result,),
        question="리바로 매출",
        enabled=False,
    )

    assert answer == "## 핵심 답\n리바로 매출을 확인했습니다."
    assert trace["deferred_to_final_composition"] is True
    assert trace["blocks_injected"] == 0


def test_market_missing_metric_keeps_the_cell_as_unprovided() -> None:
    rendered = render_deterministic_facts(
        _plan("리바로 매출 점유율 순위 성장률", sources=("mart",)),
        (_set(
            "mart",
            {"brand": "리바로", "period": "2026-01", "sales_krw": 10_000_000_000},
        ),),
        observed_on=date(2026, 8, 17),
    )
    composed = compose_lossless_answer(
        rendered,
        "## 핵심 답\n확인했습니다.",
        synthesis_trace={"status": "synthesized"},
        mode="inject",
    )

    assert "점유율" in composed.text
    assert "순위" in composed.text
    assert "성장률" in composed.text
    assert composed.text.count("원천 미제공") >= 3


def test_market_uses_live_yoy_growth_field() -> None:
    rendered = render_deterministic_facts(
        _plan("리바로 월별 매출 성장률", sources=("mart",)),
        (_set(
            "mart",
            {
                "brand": "리바로",
                "period": "2026-01",
                "sales_krw": 10_000_000_000,
                "yoy_growth_pct": "12.5",
            },
        ),),
        observed_on=date(2026, 8, 17),
    )

    assert "12.50%" in rendered.text


def test_hira_table_preserves_patient_type_sex_and_live_cost_field() -> None:
    rendered = render_deterministic_facts(
        _plan("D69 환자수", sources=("hira",)),
        (_set(
            "hira",
            {
                "sickCd": "D69",
                "sickNm": "자반 및 기타 출혈성 병태",
                "year": "2023",
                "inpatOpat": "입원",
                "sex": "남",
                "ptntCnt": "1984",
                "rvdInsupBrdnAmt": "7818608000",
            },
        ),),
        observed_on=date(2026, 8, 17),
    )

    assert "입원" in rendered.text
    assert "남" in rendered.text
    assert "1,984" in rendered.text
    assert "7,818,608,000" in rendered.text


def _hira_gender_result(*, include_breakdown: bool = True) -> SourceResult:
    hospital_rows = [
        {
            "inpatOpat": "입원",
            "sex": None,
            "sickCd": "D69",
            "sickNm": "자반 및 기타 출혈성 병태",
            "ptntCnt": "4431",
            "sexBreakdown": (
                [
                    {"sex": "남", "ptntCnt": "1984"},
                    {"sex": "여", "ptntCnt": "2447"},
                ]
                if include_breakdown
                else []
            ),
        },
        {
            "inpatOpat": "외래",
            "sex": None,
            "sickCd": "D69",
            "sickNm": "자반 및 기타 출혈성 병태",
            "ptntCnt": "60595",
            "sexBreakdown": (
                [
                    {"sex": "남", "ptntCnt": "25371"},
                    {"sex": "여", "ptntCnt": "35224"},
                ]
                if include_breakdown
                else []
            ),
        },
    ]
    age_rows = [
        {
            "age": f"{index * 10}~{index * 10 + 9}세",
            "sex": "남",
            "sickCd": "D69",
            "sickNm": "자반 및 기타 출혈성 병태",
            "ptntCnt": str(1900 + index),
        }
        for index in range(5)
    ]
    return SourceResult(
        source="hira",
        query="D69 환자 통계",
        status="ok",
        payload={
            "calls": [
                {
                    "tool": "hira_disease_hospitalization_outpatient_stats",
                    "render_data": {
                        "request": {"sickCd": "D69", "year": "2023"},
                        "totalCount": 2,
                        "request_limit": 5,
                        "items": hospital_rows,
                    },
                },
                {
                    "tool": "hira_disease_gender_age_stats",
                    "render_data": {
                        "request": {"sickCd": "D69", "year": "2023"},
                        "totalCount": 18,
                        "request_limit": 5,
                        "items": age_rows,
                    },
                },
            ]
        },
    )


def test_hira_nested_sex_breakdown_and_gender_age_are_lossless_facts() -> None:
    evidence_set = generic_evidence_set(
        "hira", (_hira_gender_result(),), date(2026, 8, 17)
    )
    rendered = render_deterministic_facts(
        _plan("23년 D69 환자수 성별", sources=("hira",)),
        (evidence_set,),
        observed_on=date(2026, 8, 17),
    )

    assert len(evidence_set.records) == 9
    for expected in ("1,984", "2,447", "25,371", "35,224"):
        assert expected in rendered.text
    assert "입원" in rendered.text and "외래" in rendered.text
    assert "0~9세" in rendered.text
    assert "원천 18건 중 5건 표시" in rendered.text
    composed = compose_lossless_answer(
        rendered,
        "## 핵심 답\nD69 환자수를 성별로 확인했습니다.",
        synthesis_trace={"status": "synthesized"},
        mode="inject",
    )
    assert (
        "[확인 한계] 이 자료는 주상병 기준 청구 실인원이며, 인구 분모가 없어 "
        "성별·연령별 발생 위험이나 유병률을 판단하지 않습니다."
    ) in composed.text


def test_hira_without_sex_breakdown_keeps_aggregate_rows() -> None:
    evidence_set = generic_evidence_set(
        "hira",
        (_hira_gender_result(include_breakdown=False),),
        date(2026, 8, 17),
    )

    assert any(record.payload.get("ptntCnt") == "4431" for record in evidence_set.records)
    assert any(record.payload.get("ptntCnt") == "60595" for record in evidence_set.records)


def test_hira_only_expansion_preserves_web_fallback_query() -> None:
    plan = _plan("23년 D69 환자수", sources=("hira",))

    expanded = expand_parameter_axes(
        plan,
        "23년 D69 환자수",
        observed_on=date(2026, 8, 17),
    )

    assert expanded.plan.tool_queries.web == plan.tool_queries.web


def test_hira_internal_zero_path_still_executes_preserved_web_query() -> None:
    calls: list[tuple[str, str]] = []

    def adapter(source: str):
        def execute(query: str, **_kwargs: object) -> SourceResult:
            calls.append((source, query))
            return SourceResult(
                source=source,
                query=query,
                status="ok" if source == "web" else "empty",
                payload={"calls": []},
            )

        return execute

    executor = ParallelSourceExecutor(
        adapters={source: adapter(source) for source in (
            "mart",
            "nedrug",
            "hira",
            "openfda",
            "clinicaltrials",
            "web",
            "patent",
        )}
    )
    plan = expand_parameter_axes(
        _plan("23년 D69 환자수", sources=("hira",)),
        "23년 D69 환자수",
        observed_on=date(2026, 8, 17),
    ).plan

    results = executor.execute(
        plan,
        session_id="r42-f10",
        source_filter=("hira", "web"),
    )

    assert {result.source for result in results} == {"hira", "web"}
    assert any(source == "web" for source, _query in calls)


def test_web_quota_is_reported_as_limit_exhaustion() -> None:
    failed = _set("web").model_copy(
        update={"item_failures": ({"status": "quota", "notice": "quota exceeded"},)}
    )

    rendered = render_deterministic_facts(
        _plan("공개 자료", sources=("web",)),
        (failed,),
        observed_on=date(2026, 8, 17),
    )

    assert "한도" in "\n".join(rendered.source_notices)


def test_failure_notices_follow_facts_and_do_not_repeat_internal_queries() -> None:
    failed = _set("nedrug").model_copy(
        update={
            "item_failures": (
                {
                    "query": "23년 상병코드 D69 환자수 의약품 정보 내부 질의",
                    "status": "timeout",
                    "notice": "read timed out",
                },
            )
        }
    )
    rendered = render_deterministic_facts(
        _plan("23년 상병코드 D69의 환자수", sources=("hira",)),
        (
            _set("hira", {"sickCd": "D69", "year": "2023", "patient_count": 100}),
            failed,
        ),
        observed_on=date(2026, 8, 17),
    )
    composed = compose_lossless_answer(
        rendered,
        "## 핵심 답\nD69 환자수는 100명입니다.",
        synthesis_trace={"status": "synthesized"},
        mode="inject",
    )

    assert composed.text.index("D69 환자수는") < composed.text.index("## 조사 범위와 완전성")
    assert composed.text.index("D69 환자수는") < composed.text.index("## 미확인 요소")
    assert "내부 질의" not in composed.text
    assert "aux:" not in composed.text


def test_uppercase_domestic_patent_identifiers_are_public() -> None:
    assert _public_identifiers(
        {"PATENT_NO": "10-1234567", "ITEM_SEQ": "202105578"}
    ) == {"10-1234567", "202105578"}


def test_selection_metadata_survives_into_composition_trace() -> None:
    plan = _plan("리바로 매출", sources=("mart",))
    results = ()
    _evidence_sets, rendered = build_lossless_render(
        plan,
        results,
        observed_on=date(2026, 8, 17),
        source_render_limit=40,
    )
    composed = compose_lossless_answer(
        rendered,
        "## 핵심 답\n조회 결과입니다.",
        synthesis_trace={"status": "synthesized"},
        mode="inject",
    )

    assert composed.trace["selection_rule"] == "leading_records_in_upstream_order"
    assert composed.trace["selection_is_ranked"] is False


def test_second_hop_does_not_reexecute_first_hop_source_query_pairs() -> None:
    first = _plan("리바로젯 임상", sources=("mart", "clinicaltrials"))
    linked = first.model_copy(
        update={
            "tool_queries": first.tool_queries.model_copy(
                update={"clinicaltrials": ("리바로젯 임상", "추가 성분 임상")}
            )
        }
    )

    filtered = _exclude_first_hop_queries(first, linked)

    assert filtered.answer_sources == ("clinicaltrials",)
    assert filtered.tool_queries.mart == ()
    assert filtered.tool_queries.clinicaltrials == ("추가 성분 임상",)


def test_grouped_source_notice_bindings_match_the_public_notice() -> None:
    duplicated_failure = {
        "status": "timeout",
        "notice": "read timed out",
        "query": "리바로 자료",
    }
    failed = _set("nedrug").model_copy(
        update={"item_failures": (duplicated_failure, duplicated_failure)}
    )

    rendered = render_deterministic_facts(
        _plan("리바로 자료", sources=("nedrug",)),
        (failed,),
        observed_on=date(2026, 8, 17),
    )

    grouped = tuple(
        notice for notice in rendered.source_notices if "동일 사유 2건" in notice
    )
    assert len(grouped) == 1
    assert {
        binding["notice"] for binding in rendered.source_notice_bindings
    }.issuperset(grouped)


def test_synthesis_timeout_keeps_the_deterministic_fact_surface() -> None:
    rendered = render_deterministic_facts(
        _plan("리바로 매출", sources=("mart",)),
        (_set("mart", {"brand": "리바로", "period": "2026-01", "sales_krw": 10_000_000_000}),),
        observed_on=date(2026, 8, 17),
    )
    composed = compose_lossless_answer(
        rendered,
        "해설은 생성하지 못했습니다.",
        synthesis_trace={"status": "fallback", "fallback_reason": "timeout"},
        mode="inject",
    )
    assert "## 시장 데이터" in composed.text
    assert "리바로" in composed.text


@pytest.mark.parametrize(
    ("failure", "expected"),
    (
        ({"status": "timeout", "notice": "read timed out"}, "응답 시간 내 도착하지 않아"),
        ({"status": "quota", "notice": "quota exceeded"}, "한도"),
    ),
)
def test_failed_lane_is_reported_by_failure_kind(
    failure: dict[str, str], expected: str
) -> None:
    failed = _set("nedrug").model_copy(update={"item_failures": (failure,)})
    rendered = render_deterministic_facts(
        _plan("리바로 자료", sources=("nedrug",)),
        (failed,),
        observed_on=date(2026, 8, 17),
    )
    assert expected in "\n".join(rendered.source_notices)


def test_identical_lane_failures_are_grouped_with_their_count() -> None:
    failure = {"status": "timeout", "notice": "read timed out"}
    failed = _set("nedrug").model_copy(
        update={"item_failures": (failure, failure)}
    )

    rendered = render_deterministic_facts(
        _plan("리바로 자료", sources=("nedrug",)),
        (failed,),
        observed_on=date(2026, 8, 17),
    )

    assert len(rendered.source_notices) == 1
    assert "동일 사유 2건" in rendered.source_notices[0]


def test_successful_zero_record_lane_is_not_reported_as_not_executed() -> None:
    empty = _set("web")
    rendered = render_deterministic_facts(
        _plan("리바로 공개 자료", sources=("web",)),
        (empty,),
        observed_on=date(2026, 8, 17),
    )
    assert "자료가 0건" in "\n".join(rendered.source_notices)
    assert "레코드" not in "\n".join(rendered.source_notices)
    assert not any(node.block_id == "web:records" for node in rendered.nodes)


def test_unexecuted_lane_uses_query_scope_without_exposing_query_text() -> None:
    plan = _plan("리바로 자료", sources=("web",)).model_copy(
        update={
            "query_scope": QueryScope(
                requested_calls={"web": 1},
                executed_calls={"web": 0},
                omitted_queries={"web": ("내부 생성 질의 원문",)},
            )
        }
    )
    rendered = render_deterministic_facts(plan, (), observed_on=date(2026, 8, 17))
    notices = "\n".join(rendered.source_notices)
    assert "실행되지 않았습니다" in notices
    assert "내부 생성 질의 원문" not in notices


def test_query_scope_runtime_notice_does_not_expose_generated_query() -> None:
    plan = _plan("사용자 질문 원문", sources=("web",)).model_copy(
        update={
            "query_scope": QueryScope(
                requested_calls={"web": 1},
                executed_calls={"web": 0},
                omitted_queries={"web": ("비공개 내부 생성 질의",)},
            )
        }
    )

    notice = _query_scope_notice(plan)

    assert notice is not None
    assert "비공개 내부 생성 질의" not in notice
    assert "사용자 질문 원문" not in notice


def test_query_generation_empty_lane_is_reported_as_not_executed() -> None:
    plan = _plan("D69 환자수", sources=("hira", "web")).model_copy(
        update={
            "tool_queries": _plan(
                "D69 환자수", sources=("hira", "web")
            ).tool_queries.model_copy(
                update={"web": ()}
            )
        }
    )

    rendered = render_deterministic_facts(
        plan,
        (_set("hira", {"sickCd": "D69", "year": "2023", "ptntCnt": "100"}),),
        observed_on=date(2026, 8, 17),
    )

    notices = "\n".join(rendered.source_notices)
    assert "웹 뉴스" in notices
    assert "실행되지 않았습니다" in notices
    assert "질의가 생성되지" in notices


def test_document_duplicate_chunks_render_once_and_preserve_every_record() -> None:
    content = "I. 제안개요 및 추진전략 II. 프로젝트 수행방안 III. 프로젝트 관리방안"
    rendered = render_deterministic_facts(
        _plan("첨부한 제안서의 목차 알려줘", sources=("document",)),
        (
            _set(
                "document",
                {"document_name": "제안서.pptx", "page": 2, "section": "목차", "content": content},
                {"document_name": "제안서.pptx", "page": 4, "section": "목차", "content": content},
                {"document_name": "제안서.pptx", "page": 27, "section": "목차", "content": content},
            ),
        ),
        observed_on=date(2026, 8, 17),
    )

    assert rendered.text.count(content) == 1
    assert "동일 내용 3개 청크" in rendered.text
    assert {record_id for node in rendered.nodes for record_id in node.record_ids} >= {
        "document:1",
        "document:2",
        "document:3",
    }


def test_document_page_and_repeated_footer_chunks_are_hidden_but_counted() -> None:
    rendered = render_deterministic_facts(
        _plan("첨부한 제안서의 목차 알려줘", sources=("document",)),
        (
            _set(
                "document",
                {"document_name": "제안서.pptx", "page": 4, "content": "4 -"},
                {"document_name": "제안서.pptx", "page": 4, "content": "제논 | 2026.01.20"},
                {"document_name": "제안서.pptx", "page": 5, "content": "제논 | 2026.01.20"},
                {"document_name": "제안서.pptx", "page": 6, "content": "제논 | 2026.01.20"},
                {"document_name": "제안서.pptx", "page": 7, "section": "목차", "content": "I. 제안개요"},
            ),
        ),
        observed_on=date(2026, 8, 17),
    )

    assert "4 -" not in rendered.text
    assert "제논 | 2026.01.20" not in rendered.text
    assert "머리글/페이지번호 4건 제외" in rendered.text
    assert len({record_id for node in rendered.nodes for record_id in node.record_ids}) == 5


def test_repeated_dated_business_fact_is_not_guessed_to_be_a_footer() -> None:
    fact = "프로젝트 일정 | 2026.01.20 | 킥오프"
    rendered = render_deterministic_facts(
        _plan("첨부 문서 설명", sources=("document",)),
        (
            _set(
                "document",
                {"document_name": "제안서.pptx", "page": 2, "content": fact},
                {"document_name": "제안서.pptx", "page": 3, "content": fact},
                {"document_name": "제안서.pptx", "page": 4, "content": "추진 전략"},
            ),
        ),
        observed_on=date(2026, 8, 17),
    )

    assert rendered.text.count("프로젝트 일정") == 1
    assert rendered.text.count("킥오프") == 1
    assert "동일 내용 2개 청크" in rendered.text


def test_document_broken_extraction_is_not_rewritten_and_is_explained() -> None:
    broken = (
        "1·2내외부생성형AI모델연동및유연한대응을위한모듈API기반아키텍처설계"
        "당사주요시스템KISSWINK및SSODRM보안솔루션연계환경구축"
    )
    rendered = render_deterministic_facts(
        _plan("어떤 제안인지 설명해줘", sources=("document",)),
        (_set("document", {"document_name": "제안서.pptx", "page": 29, "content": broken}),),
        observed_on=date(2026, 8, 17),
    )

    assert broken in rendered.text
    assert "슬라이드 도형 순서에 따라 문장이 이어질 수 있습니다" in rendered.text


def test_market_analysis_renders_document_records_as_fact_nodes() -> None:
    records = tuple(
        {
            "document_name": "제안서.pptx",
            "page": page,
            "section": "제안 개요",
            "content": f"슬라이드 {page}의 제안 내용",
        }
        for page in (2, 4, 27, 29, 52)
    )
    rendered = render_deterministic_facts(
        _plan("어떤 제안인지 설명해줘", sources=("document",)),
        (_set("document", *records),),
        observed_on=date(2026, 8, 17),
    )

    assert rendered.profile == "market_analysis"
    assert any(node.block_id == "document:records" for node in rendered.nodes)
    assert rendered.coverage.records_rendered == 5


def test_document_fact_surface_survives_synthesis_timeout() -> None:
    rendered = render_deterministic_facts(
        _plan("어떤 제안인지 설명해줘", sources=("document",)),
        (
            _set(
                "document",
                {
                    "document_name": "제안서.pptx",
                    "page": 2,
                    "section": "제안 개요",
                    "content": "AI PB 서비스 구축 제안",
                },
            ),
        ),
        observed_on=date(2026, 8, 17),
    )
    composed = compose_lossless_answer(
        rendered,
        "해설은 생성하지 못했습니다.",
        synthesis_trace={"status": "fallback", "fallback_reason": "timeout"},
        mode="inject",
    )

    assert "제안서.pptx" in composed.text
    assert "2" in composed.text
    assert "제안 개요" in composed.text
    assert "AI PB 서비스 구축 제안" in composed.text


def test_empty_document_lane_reports_zero_without_rendering_a_table() -> None:
    rendered = render_deterministic_facts(
        _plan("첨부 문서 설명", sources=("document",)),
        (_set("document"),),
        observed_on=date(2026, 8, 17),
    )

    assert "자료가 0건" in "\n".join(rendered.source_notices)
    assert not any(node.block_id == "document:records" for node in rendered.nodes)


def test_document_excerpt_is_bounded_and_points_to_inspection() -> None:
    content = "가" * (DOCUMENT_EXCERPT_LIMIT + 10)
    rendered = render_deterministic_facts(
        _plan("첨부 문서 설명", sources=("document",)),
        (_set("document", {"document_name": "제안서.pptx", "page": 2, "content": content}),),
        observed_on=date(2026, 8, 17),
    )

    assert "가" * DOCUMENT_EXCERPT_LIMIT in rendered.text
    assert "가" * (DOCUMENT_EXCERPT_LIMIT + 1) not in rendered.text
    assert "… (전문은 조회 상세)" in rendered.text
