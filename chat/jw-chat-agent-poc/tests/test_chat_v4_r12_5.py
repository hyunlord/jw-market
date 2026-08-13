from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from jw_chat_agent_poc.service.v4 import adapters as v4_adapters
from jw_chat_agent_poc.service.v4.charts import build_grounded_charts
from jw_chat_agent_poc.service.v4.clinical import normalize_clinical_study
from jw_chat_agent_poc.service.v4.contracts import (
    EvidenceEnvelope,
    PlannerOutput,
    RequestedAnswerShape,
    SourceResult,
    ToolQueries,
)
from jw_chat_agent_poc.service.v4.gates import apply_v4_gates
from jw_chat_agent_poc.service.v4.deterministic_render import render_deterministic_facts
from jw_chat_agent_poc.service.v4.expansion import (
    build_second_hop_expansion,
    expand_parameter_axes,
)
from jw_chat_agent_poc.service.v4.inspection import build_inspection_detail
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    CoverageLedger,
    DeterministicRender,
    EvidenceRecord,
    EvidenceSet,
    RenderNode,
)
from jw_chat_agent_poc.service.v4.render_clinical import render_clinical
from jw_chat_agent_poc.service.v4.narrative_realization import build_narrative_realization
from jw_chat_agent_poc.service.v4.retrieval_events import (
    public_retrieval_notice,
    retrieval_event_from_result,
)
from jw_chat_agent_poc.service.v4.source_tiers import source_tier
from jw_chat_agent_poc.service.v4.synthesizer import _select_usable_results
from jw_chat_agent_poc.tools.external.client import (
    ExternalApiClient,
    ExternalCall,
    _mcp_tool_spec,
)


def _plan(question: str, *, answer_sources=("hira",)) -> PlannerOutput:
    return PlannerOutput(
        resolved_question=question,
        expanded_intents=(question,),
        answer_sources=answer_sources,
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
        requested_answer_shape=RequestedAnswerShape(
            entities=("E10~E14",),
            measure_or_attribute=("patient_count",),
        ),
    )


def test_a_expands_kcd_and_explicit_year_axes_deterministically() -> None:
    question = "2022년과 2024년 E10~E14 환자수 비교"
    first = expand_parameter_axes(_plan(question), question, observed_on=date(2026, 8, 13))
    second = expand_parameter_axes(_plan(question), question, observed_on=date(2026, 8, 13))

    assert first == second
    assert first.trace["axes"] == {
        "kcd_codes": ["E10", "E11", "E12", "E13", "E14"],
        "years": [2022, 2024],
    }
    assert first.plan.tool_queries.hira == tuple(
        f"{code} 환자수 {year}년"
        for code in ("E10", "E11", "E12", "E13", "E14")
        for year in (2022, 2024)
    )


def test_a_second_hop_uses_observed_products_and_records_truncation() -> None:
    plan = _plan("혈우병 치료제 특허현황", answer_sources=("patent",))
    result = SourceResult(
        source="nedrug",
        query="혈우병 치료제",
        status="ok",
        payload={
            "records": [
                {"item_name": "제품가"},
                {"item_name": "제품나"},
                {"item_name": "제품다"},
                {"item_name": "제품라"},
            ]
        },
    )

    expanded = build_second_hop_expansion(plan, plan.resolved_question, (result,), max_queries=3)

    assert expanded is not None
    assert expanded.plan.tool_queries.patent == (
        "제품가 특허현황",
        "제품나 특허현황",
        "제품다 특허현황",
    )
    assert expanded.trace["truncated_count"] == 1
    assert expanded.trace["source"] == "nedrug"


def test_a_disease_patent_expansion_binds_deterministic_product_before_first_wave() -> None:
    question = "혈우병 치료제 특허현황"
    plan = _plan(question, answer_sources=("patent",))

    first = expand_parameter_axes(plan, question, observed_on=date(2026, 8, 13))
    second = expand_parameter_axes(plan, question, observed_on=date(2026, 8, 13))

    assert first == second
    assert first.plan.tool_queries.patent == ("헴리브라 특허현황",)
    assert first.trace["entity_expansion"] == {
        "status": "expanded",
        "source": "hira_disease_anchor_brand",
        "entities": ["헴리브라"],
        "requests": {"patent": ["헴리브라 특허현황"]},
    }


def test_a_disease_patent_expansion_uses_patent_query_when_web_is_primary() -> None:
    question = "혈우병 치료제 특허현황"
    plan = _plan(question, answer_sources=("web",))

    expanded = expand_parameter_axes(plan, question, observed_on=date(2026, 8, 13))

    assert expanded.plan.tool_queries.patent == ("헴리브라 특허현황",)
    assert expanded.trace["entity_expansion"]["entities"] == ["헴리브라"]


def test_a_expanded_disease_product_reaches_mfds_as_product_and_ingredient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jw_chat_agent_poc.agent_loop import factory
    from jw_chat_agent_poc.service import general_view_routing
    from jw_chat_agent_poc.tools.external.client import ExternalCall

    question = "혈우병 치료제 특허현황"
    expanded = expand_parameter_axes(
        _plan(question, answer_sources=("patent",)),
        question,
        observed_on=date(2026, 8, 13),
    )
    mfds_requests: list[tuple[str, str | None]] = []

    def external_call(tool: str, source: str) -> ExternalCall:
        return ExternalCall(
            tool=tool,
            source=source,
            status="no_data",
            summary_text=f"{tool} no data",
            render_data={"items": []},
        )

    class Resolver:
        def resolve(self, query: str, *, allow_default: bool) -> SimpleNamespace:
            assert allow_default is False
            assert "헴리브라" in query
            return SimpleNamespace(
                canonical_brand="헴리브라",
                molecule_en=("emicizumab",),
            )

    class External:
        timeout_s = 12

        def mfds_patent(
            self,
            ingredient: str,
            *,
            item_name: str | None = None,
        ) -> ExternalCall:
            mfds_requests.append((ingredient, item_name))
            return external_call("mfds_patent", "식품의약품안전처")

        def mfds_fda_orangebook(self, _ingredient: str) -> ExternalCall:
            return external_call("mfds_fda_orangebook", "FDA Orange Book")

        def web_search(self, _query: str, *, topic: str = "general") -> ExternalCall:
            assert topic == "news"
            return external_call("web_search", "Tavily")

    monkeypatch.setattr(
        factory,
        "build_chat_agent_dependencies",
        lambda **_kwargs: SimpleNamespace(
            external=External(),
            resolver=Resolver(),
            query_layer=None,
        ),
    )
    monkeypatch.setattr(
        general_view_routing.GeneralViewService,
        "from_env",
        lambda _resolver: SimpleNamespace(),
    )
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "tavily")

    v4_adapters.build_source_adapters()["patent"](
        expanded.plan.tool_queries.patent[0]
    )

    assert mfds_requests == [("emicizumab", "헴리브라")]


def test_a_mfds_serialization_keeps_expanded_product_and_ingredient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = ExternalApiClient(mode="fixture")
    captured: dict[str, str] = {}

    def capture(
        tool: str,
        params: dict[str, str],
        *,
        xml: bool = False,
    ) -> ExternalCall:
        assert tool == "mfds_patent"
        assert xml is True
        captured.update(params)
        return ExternalCall(
            tool=tool,
            source="식품의약품안전처",
            status="ok",
            summary_text="captured",
            render_data={"request": params, "items": [{"patent_no": "10-1"}]},
        )

    monkeypatch.setattr(client, "_fixture_or_live", capture)

    result = client.mfds_patent("emicizumab", item_name="헴리브라")
    spec = _mcp_tool_spec("mfds_patent", captured)

    assert result.status == "ok"
    assert captured["item_name"] == "헴리브라"
    assert captured["ingr_name"] == "emicizumab"
    assert spec["arguments"]["item_name"] == "헴리브라"
    assert spec["arguments"]["ingr_name"] == "emicizumab"


def test_a_single_kcd_and_abbreviated_year_range_expand_without_substitution() -> None:
    question = "21년부터 25년도 상병코드 D593의 입원 외래별 년도별 환자수를 비교해줘"

    expanded = expand_parameter_axes(
        _plan(question),
        question,
        observed_on=date(2026, 8, 13),
    )

    assert expanded.trace["axes"] == {
        "kcd_codes": ["D593"],
        "years": [2021, 2022, 2023, 2024, 2025],
    }
    assert len(expanded.plan.tool_queries.hira) == 5
    assert all("D593" in query for query in expanded.plan.tool_queries.hira)
    assert tuple(
        year
        for year in range(2021, 2026)
        if any(f"{year}년" in query for query in expanded.plan.tool_queries.hira)
    ) == tuple(range(2021, 2026))


@pytest.mark.parametrize(
    ("question", "tool", "notice_fragment"),
    (
        (
            "2024년 D693 입원 외래별 환자수",
            "hira_disease_hospitalization_outpatient_stats",
            None,
        ),
        (
            "2023년 D50 성별 연령10세구간별 환자수",
            "hira_disease_gender_age_stats",
            None,
        ),
        (
            "2024년 D693 요양기관종별 환자수",
            "hira_disease_institution_class_stats",
            None,
        ),
        (
            "2024년 D693 요양기관소재지별 환자수",
            "hira_disease_area_stats",
            None,
        ),
        (
            "2024년 D693 성별 연령5세구간별 내원일수",
            None,
            "성별·연령5세구간별",
        ),
        (
            "2024년 D693 진료년월 기준 월별 환자수 추이",
            None,
            "진료년월별",
        ),
    ),
)
def test_b_hira_axis_route_never_substitutes_an_unrequested_aggregation(
    question: str,
    tool: str | None,
    notice_fragment: str | None,
) -> None:
    route_factory = getattr(v4_adapters, "_hira_stat_route", None)

    assert callable(route_factory)
    route = route_factory(question)
    assert route.tool == tool
    if notice_fragment is None:
        assert route.scope_notice is None
    else:
        assert notice_fragment in route.scope_notice


def test_b_hira_scope_limit_preserves_its_exact_public_reason() -> None:
    reason = (
        "요청하신 성별·연령5세구간별 집계는 현재 연결된 HIRA 조회에서 "
        "지원되지 않아 다른 집계축으로 대체하지 않았습니다."
    )
    result = SourceResult(
        source="hira",
        query="2024년 D693 성별 연령5세구간별 내원일수",
        status="scope_limit",
        notice=reason,
    )

    event = retrieval_event_from_result(result)

    assert public_retrieval_notice(event) == reason
    assert "성분명" not in public_retrieval_notice(event)


def test_b_synthesizer_keeps_every_expanded_hira_axis() -> None:
    plan = _plan("E10~E14 환자수")
    results = tuple(
        SourceResult(source="hira", query=f"{code} 환자수", status="ok", payload={"calls": []})
        for code in ("E10", "E11", "E12", "E13", "E14")
    )

    assert _select_usable_results(plan, results) == results


def test_b_hira_repair_keeps_each_code_bound_to_its_value() -> None:
    results = tuple(
        SourceResult(
            source="hira",
            query=f"{code} 환자수",
            status="ok",
            payload={
                "calls": [
                    {
                        "render_data": {
                            "request": {"sickCd": code, "year": "2024"},
                            "items": [
                                {
                                    "inpatOpat": "외래",
                                    "ptntCnt": value,
                                    "units": {"ptntCnt": "명"},
                                }
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
        for code, value in (("E10", "50895"), ("E11", "3585979"), ("E12", "1300"))
    )

    gated = apply_v4_gates("E10~E12 환자수", "확인된 값을 정리합니다.", results)

    assert "E10 외래 환자수 50,895명" in gated.text
    assert "E11 외래 환자수 3,585,979명" in gated.text
    assert "E12 외래 환자수 1,300명" in gated.text
    assert [item["subject"] for item in gated.trace["requested_hira_surface"]["expected"]] == [
        "E10",
        "E11",
        "E12",
    ]


def test_c_web_is_tier_one_when_not_the_primary_answer_source() -> None:
    assert source_tier(_plan("리바로젯 특허현황", answer_sources=("patent",)), "web") == 1


def test_c_clinical_projection_preserves_protocol_detail_fields() -> None:
    normalized = normalize_clinical_study(
        {
            "protocolSection": {
                "identificationModule": {"nctId": "NCT00000001", "briefTitle": "시험"},
                "designModule": {"studyType": "INTERVENTIONAL", "enrollmentInfo": {"count": 42}},
                "armsInterventionsModule": {
                    "interventions": [{"type": "DRUG", "name": "약물A", "description": "10 mg"}]
                },
                "sponsorCollaboratorsModule": {
                    "leadSponsor": {"name": "주관사"},
                    "collaborators": [{"name": "협력사"}],
                },
                "contactsLocationsModule": {
                    "locations": [{"facility": "서울병원", "country": "Korea, Republic of"}]
                },
                "outcomesModule": {
                    "primaryOutcomes": [{"measure": "1차 지표", "timeFrame": "12주"}],
                    "secondaryOutcomes": [{"measure": "2차 지표"}],
                },
                "descriptionModule": {"briefSummary": "간략 요약", "detailedDescription": "상세 설명"},
                "eligibilityModule": {
                    "eligibilityCriteria": "선정 및 제외 기준",
                    "sex": "ALL",
                    "minimumAge": "18 Years",
                    "maximumAge": "75 Years",
                },
            },
            "hasResults": True,
        }
    )

    assert normalized["intervention_details"][0]["description"] == "10 mg"
    assert normalized["collaborators"] == ["협력사"]
    assert normalized["facilities"] == ["서울병원"]
    assert normalized["primary_outcomes"][0]["measure"] == "1차 지표"
    assert normalized["secondary_outcomes"][0]["measure"] == "2차 지표"
    assert normalized["brief_summary"] == "간략 요약"
    assert normalized["eligibility_criteria"] == "선정 및 제외 기준"
    assert normalized["sex"] == "ALL"
    assert normalized["minimum_age"] == "18 Years"


def test_c_clinical_renderer_keeps_all_records_and_adds_record_details() -> None:
    records = tuple(
        EvidenceRecord(
            evidence_id=f"ct:{index}",
            source="clinicaltrials",
            result_kind="clinical",
            payload={
                "nct_id": f"NCT{index:08d}",
                "brief_title": f"시험 {index}",
                "overall_status": "COMPLETED",
                "interventions": ["약물A"],
                "primary_outcomes": [{"measure": f"지표 {index}"}],
                "brief_summary": f"요약 {index}",
            },
        )
        for index in range(1, 24)
    )
    evidence = EvidenceSet(
        source="clinicaltrials",
        query_spec=("Pitavastatin AND Ezetimibe",),
        retrieved_at="2026-08-13T00:00:00Z",
        coverage=CoverageLedger(records_received=23, records_unique=23, records_relevant=23),
        records=records,
    )

    nodes, _required = render_clinical(evidence, single=False)

    rendered = {record_id for node in nodes for record_id in node.record_ids}
    assert rendered == {record.evidence_id for record in records}
    assert all("외 " not in node.text for node in nodes)
    assert any("1차 평가변수" in node.text and "간략 요약" in node.text for node in nodes)


def test_d_chart_values_are_bound_to_the_same_rendered_records() -> None:
    records = tuple(
        EvidenceRecord(
            evidence_id=f"mart:{period}",
            source="mart",
            result_kind="mart",
            payload={"period": period, "brand": "리바로젯", "sales": value, "unit": "억원"},
        )
        for period, value in (("2025", 100.0), ("2026", 124.54))
    )
    evidence = EvidenceSet(
        source="mart",
        retrieved_at="2026-08-13T00:00:00Z",
        coverage=CoverageLedger(records_received=2, records_unique=2, records_rendered=2),
        records=records,
    )

    charts = build_grounded_charts((evidence,), tuple(record.evidence_id for record in records))

    assert charts[0]["chart_type"] == "line"
    assert charts[0]["x"]["values"] == ["2025", "2026"]
    assert charts[0]["series"][0]["values"] == [100.0, 124.54]
    assert charts[0]["series"][0]["record_ids"] == ["mart:2025", "mart:2026"]


def test_f_inspection_uses_backend_counts_and_sanitizes_internal_values() -> None:
    plan = _plan("리바로젯 특허현황", answer_sources=("patent",))
    result = SourceResult(
        source="patent",
        query="리바로젯 특허현황 http://internal.svc/json?api_key=secret",
        status="ok",
        payload={"records": [{"patent_number": "10-1"}, {"patent_number": "10-2"}]},
        elapsed_ms=1200,
    )
    evidence = EvidenceSet(
        source="patent",
        retrieved_at="2026-08-13T00:00:00Z",
        coverage=CoverageLedger(records_received=2, records_unique=2, records_rendered=1),
        records=(
            EvidenceRecord(
                evidence_id="patent:1",
                source="patent",
                result_kind="patent",
                payload={"patent_number": "10-1"},
            ),
            EvidenceRecord(
                evidence_id="patent:2",
                source="patent",
                result_kind="patent",
                payload={"patent_number": "10-2"},
            ),
        ),
    )
    rendered = DeterministicRender(
        profile="patent_portfolio",
        nodes=(RenderNode(block_id="patent:records", record_ids=("patent:1",), text="표"),),
        coverage=evidence.coverage,
        structured_claims=(
            {"arguments": [{"record_id": "patent:1"}]},
        ),
    )

    detail = build_inspection_detail(plan, (result,), (evidence,), rendered)
    call = detail["calls"][0]

    assert call["counts"] == {
        "returned": 2,
        "parsed": 2,
        "envelope": 2,
        "rendered": 1,
        "narrated": 1,
    }
    assert call["unused_count"] == 1
    assert call["status"] == "완료"
    serialized = str(detail)
    assert "internal.svc" not in serialized
    assert "secret" not in serialized


def test_f_inspection_binds_nested_hira_counts_to_each_call() -> None:
    plan = _plan("E10 환자수", answer_sources=("hira",))
    result = SourceResult(
        source="hira",
        query="E10 환자수",
        status="ok",
        payload={
            "calls": [
                {
                    "tool": "hira_disease_name_code",
                    "render_data": {
                        "request": {"sickCd": "E10", "searchText": "E10"},
                        "items": [{"sickCd": "E10", "sickNm": "1형 당뇨병"}],
                    },
                },
                {
                    "tool": "hira_disease_hospitalization_outpatient_stats",
                    "render_data": {
                        "request": {"sickCd": "E10", "year": "2024"},
                        "items": [
                            {"sickCd": "E10", "inpatOpat": "입원", "ptntCnt": "2989"},
                            {"sickCd": "E10", "inpatOpat": "외래", "ptntCnt": "50895"},
                        ],
                    },
                },
            ]
        },
        elapsed_ms=1200,
    )
    evidence = EvidenceSet(
        source="hira",
        retrieved_at="2026-08-13T00:00:00Z",
        coverage=CoverageLedger(records_received=2, records_unique=2),
        records=(
            EvidenceRecord(
                evidence_id="hira:1:1",
                source="hira",
                result_kind="external_record",
                payload={"request": {"sickCd": "E10"}},
            ),
            EvidenceRecord(
                evidence_id="hira:1:2",
                source="hira",
                result_kind="external_record",
                payload={"request": {"sickCd": "E10", "year": "2024"}},
            ),
        ),
    )
    rendered = DeterministicRender(profile="market_analysis")
    answer = (
        "1형 당뇨병(E10)의 2024년 입원 환자수는 2,989명, "
        "외래 환자수는 50,895명입니다."
    )

    detail = build_inspection_detail(
        plan,
        (result,),
        (evidence,),
        rendered,
        answer_text=answer,
    )
    call = detail["calls"][0]

    assert call["counts"] == {
        "returned": 3,
        "parsed": 3,
        "envelope": 3,
        "rendered": 3,
        "narrated": 3,
    }
    assert call["request_parameters"] == {
        "query": "E10 환자수",
        "calls": [
            {"sickCd": "E10", "searchText": "E10"},
            {"sickCd": "E10", "year": "2024"},
        ],
    }
    assert call["unused_count"] == 0


def test_d_derived_metrics_are_recomputed_from_bound_records() -> None:
    records = (
        EvidenceRecord(
            evidence_id="mart:a",
            source="mart",
            result_kind="mart",
            payload={
                "brand": "A",
                "market_share": 20.0,
                "competitive_market_share": 20.0,
                "overall_market_share": 10.0,
                "sales_share": 20.0,
                "volume_share": 10.0,
                "brand_growth": 12.0,
                "market_growth": 5.0,
                "share_change_contribution": 7.0,
            },
        ),
        EvidenceRecord(
            evidence_id="mart:b",
            source="mart",
            result_kind="mart",
            payload={"brand": "B", "market_share": 10.0},
        ),
    )
    evidence = EvidenceSet(
        source="mart",
        retrieved_at="2026-08-13T00:00:00Z",
        coverage=CoverageLedger(records_received=2, records_unique=2, records_rendered=2),
        records=records,
    )

    realization = build_narrative_realization(
        (evidence,), tuple(record.evidence_id for record in records)
    )
    proofs = {item.operator_id: item for item in realization.recomputations}

    assert proofs["CER"].expected == 2.0
    assert proofs["PRICE_MIX_INDEX"].expected == 2.0
    assert proofs["GROWTH_DECOMP"].expected["recomputed_growth"] == 12.0
    assert proofs["CONCENTRATION_CR5"].expected == 30.0
    assert set(proofs["PEER_ZSCORE"].expected) == {"mart:a", "mart:b"}


def test_d_direct_confirmation_is_inline_and_keeps_source_scoped_relations() -> None:
    records = tuple(
        EvidenceRecord(
            evidence_id=f"patent:{index}",
            source="patent",
            result_kind="patent",
            payload={
                "patent_number": f"10-{index}",
                "status": "소멸" if index == 1 else "존속",
            },
        )
        for index in (1, 2)
    )
    evidence = EvidenceSet(
        source="patent",
        retrieved_at="2026-08-13T00:00:00Z",
        coverage=CoverageLedger(records_received=2, records_unique=2),
        records=records,
    )

    realization = build_narrative_realization(
        (evidence,),
        tuple(record.evidence_id for record in records),
    )
    surface = "\n".join(node.text for node in realization.nodes)

    assert "## [직접 확인]" not in surface
    assert "- [직접 확인]" in surface
    assert "식품의약품안전처 의약품 특허목록" in surface


def test_d_missing_source_fields_are_not_rendered_as_internal_raw_field_names() -> None:
    record = EvidenceRecord(
        evidence_id="clinicaltrials:NCT05151731",
        source="clinicaltrials",
        result_kind="clinical",
        payload={
            "nct_id": "NCT05151731",
            "brief_title": "시험 디자인",
            "overall_status": "COMPLETED",
        },
    )
    evidence = EvidenceSet(
        source="clinicaltrials",
        retrieved_at="2026-08-13T00:00:00Z",
        coverage=CoverageLedger(records_received=1, records_unique=1),
        records=(record,),
    )
    plan = _plan("NCT05151731 시험 디자인", answer_sources=("clinicaltrials",))

    rendered = render_deterministic_facts(
        plan,
        (evidence,),
        observed_on=date(2026, 8, 13),
    )
    surface = "\n".join(node.text for node in rendered.nodes)

    assert "요청 필드 보강" not in surface
    assert "patent_no" not in surface
    assert "invention_title" not in surface


def test_d_patent_coverage_heading_is_user_facing() -> None:
    record = EvidenceRecord(
        evidence_id="patent:10-1",
        source="patent",
        result_kind="patent",
        payload={
            "lane": "kr_primary",
            "product": "헴리브라",
            "ingredient": "emicizumab",
            "patent_no": "10-1",
            "status": "등록",
        },
    )
    evidence = EvidenceSet(
        source="patent",
        retrieved_at="2026-08-13T00:00:00Z",
        coverage=CoverageLedger(records_received=1, records_unique=1),
        records=(record,),
        query_manifest=(
            {
                "lane": "kr_primary",
                "records_received": 1,
                "records_unique": 1,
                "product_patent_rows": 1,
            },
        ),
    )

    rendered = render_deterministic_facts(
        _plan("혈우병 치료제 특허현황", answer_sources=("patent",)),
        (evidence,),
        observed_on=date(2026, 8, 13),
    )
    surface = "\n".join(node.text for node in rendered.nodes)

    assert "## 조사 범위와 완전성" not in surface
    assert "## 국내 특허 조회 범위" in surface
