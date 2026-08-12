from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from jw_chat_agent_poc.service.v4 import lossless_contracts, lossless_spine
from jw_chat_agent_poc.service.v4.clinical import (
    compile_clinical_query,
    merge_clinical_searches,
    normalize_clinical_study,
)
from jw_chat_agent_poc.service.v4 import adapters as v4_adapters
from jw_chat_agent_poc.service.v4.adapters import _clinical_lossless_external_call
from jw_chat_agent_poc.service.v4.contracts import (
    ClinicalTrialConcept,
    PlannerOutput,
    ToolQueries,
)
from jw_chat_agent_poc.service.v4.planner import (
    _anchor_relative_years,
    _attach_lossless_contracts,
    _limit_first_wave_queries,
    _planner_messages,
)
from jw_chat_agent_poc.service.v4.patent import build_patent_lane_payload
from jw_chat_agent_poc.service.v4.lossless_spine import (
    build_evidence_sets,
    compose_lossless_answer,
    render_deterministic_facts,
)
from jw_chat_agent_poc.service.v4.lossless_contracts import SourceReference
from jw_chat_agent_poc.service.v4.contracts import Citation, SourceResult
from jw_chat_agent_poc.service.v4.gates import apply_v4_gates
from jw_chat_agent_poc.service.v4.executor import ParallelSourceExecutor
from jw_chat_agent_poc.service.v4 import runtime as v4_runtime
from jw_chat_agent_poc.service.v4.runtime import V4Runtime
from jw_chat_agent_poc.service.v4.synthesizer import SynthesisOutcome, _synthesis_messages
from jw_chat_agent_poc.tools.external.clinicaltrials_v2 import (
    ClinicalSearchResult,
    ClinicalTrialsV2Client,
)


def _plan(*clinical_queries: str) -> PlannerOutput:
    return PlannerOutput(
        resolved_question="리바로젯 제네릭 임상현황",
        expanded_intents=("임상현황",),
        answer_sources=("clinicaltrials",),
        tool_queries=ToolQueries(
            mart=("리바로젯",),
            nedrug=("리바로젯",),
            hira=("리바로젯",),
            openfda=("리바로젯",),
            clinicaltrials=clinical_queries,
            web=("리바로젯",),
            patent=("리바로젯",),
        ),
        linking_plan="static clinical queries",
    )


def _study(nct_id: str) -> dict[str, Any]:
    return {
        "hasResults": False,
        "protocolSection": {
            "identificationModule": {
                "nctId": nct_id,
                "briefTitle": f"Brief {nct_id}",
                "officialTitle": f"Official {nct_id}",
            },
            "designModule": {
                "studyType": "INTERVENTIONAL",
                "phases": ["NA"],
                "enrollmentInfo": {"count": 120, "type": "ACTUAL"},
                "designInfo": {
                    "allocation": "RANDOMIZED",
                    "interventionModel": "PARALLEL",
                    "maskingInfo": {"masking": "DOUBLE"},
                },
            },
            "statusModule": {
                "overallStatus": "COMPLETED",
                "startDateStruct": {"date": "2023-01-01", "type": "ACTUAL"},
                "primaryCompletionDateStruct": {
                    "date": "2024-01-01",
                    "type": "ACTUAL",
                },
                "completionDateStruct": {"date": "2024-03-01", "type": "ACTUAL"},
                "studyFirstPostDateStruct": {"date": "2022-12-01", "type": "ACTUAL"},
                "lastUpdatePostDateStruct": {"date": "2024-04-01", "type": "ACTUAL"},
            },
            "conditionsModule": {"conditions": ["Hyperlipidemia"]},
            "armsInterventionsModule": {
                "interventions": [
                    {"type": "DRUG", "name": "Pitavastatin"},
                    {"type": "DRUG", "name": "Ezetimibe"},
                ],
                "armGroups": [
                    {
                        "label": "Comparator",
                        "type": "ACTIVE_COMPARATOR",
                        "interventionNames": ["DRUG: Pitavastatin"],
                    }
                ],
            },
            "sponsorCollaboratorsModule": {
                "leadSponsor": {"name": "JW Pharmaceutical", "class": "INDUSTRY"}
            },
            "contactsLocationsModule": {
                "locations": [
                    {"facility": "Hospital A", "country": "Korea, Republic of"},
                    {"facility": "Hospital B", "country": "United States"},
                ]
            },
        },
    }


def test_clinical_query_compiler_removes_database_filler_terms() -> None:
    concept = ClinicalTrialConcept(
        ingredients=("Pitavastatin", "Ezetimibe"),
        brands=(),
        search_area="intervention",
        match="both",
        countries=(),
        statuses=(),
        source_queries=("Pitavastatin and Ezetimibe combination clinical trials 현황",),
    )

    compiled = compile_clinical_query(concept)

    assert compiled.parameters == {
        "query.intr": "Pitavastatin AND Ezetimibe",
        "pageSize": 100,
        "countTotal": "true",
    }
    assert "clinical trials" not in compiled.expression.casefold()
    assert "combination" not in compiled.expression.casefold()
    assert "현황" not in compiled.expression


def test_clinical_query_compiler_does_not_require_brand_when_ingredients_exist() -> None:
    concept = ClinicalTrialConcept(
        ingredients=("Pitavastatin", "Ezetimibe"),
        brands=("LIVALO", "리바로젯"),
        search_area="intervention",
        match="both",
        source_queries=("리바로젯 병용 임상",),
    )

    compiled = compile_clinical_query(concept)

    assert compiled.expression == "Pitavastatin AND Ezetimibe"
    assert "LIVALO" not in compiled.expression
    assert "리바로젯" not in compiled.expression


def test_first_wave_keeps_all_static_clinical_queries(monkeypatch) -> None:
    monkeypatch.delenv("CHAT_V4_MAX_SOURCE_QUERIES", raising=False)
    clinical_queries = tuple(f"clinical query {index}" for index in range(12))
    plan = _plan(*clinical_queries).model_copy(
        update={
            "tool_queries": ToolQueries(
                **{
                    source: (
                        clinical_queries
                        if source == "clinicaltrials"
                        else (f"{source} first", f"{source} second")
                    )
                    for source in ("mart", "nedrug", "hira", "openfda", "clinicaltrials", "web", "patent")
                }
            )
        }
    )

    limited = _limit_first_wave_queries(plan)

    assert limited.tool_queries.clinicaltrials == clinical_queries
    assert all(
        len(queries) == 1
        for source, queries in limited.tool_queries.items()
        if source != "clinicaltrials"
    )


def test_planner_contract_deduplicates_equivalent_clinical_parameters() -> None:
    plan = _plan(
        "Pitavastatin and Ezetimibe combination clinical trials",
        "Pitavastatin AND Ezetimibe 임상 현황",
        "Ezetimibe clinical trials",
    )

    contracted = _attach_lossless_contracts("리바로젯 제네릭 임상현황", plan)

    assert len(contracted.tool_queries.clinicaltrials) == 2
    assert len(contracted.clinical_query_specs) == 2
    first = compile_clinical_query(contracted.clinical_query_specs[0])
    assert first.expression == "Pitavastatin AND Ezetimibe"
    assert contracted.clinical_query_specs[0].source_queries == (
        "Pitavastatin and Ezetimibe combination clinical trials",
        "Pitavastatin AND Ezetimibe 임상 현황",
    )


def test_planner_contract_preserves_model_supplied_clinical_concept() -> None:
    concept = ClinicalTrialConcept(
        ingredients=("Pitavastatin", "Ezetimibe"),
        search_area="intervention",
        match="both",
        countries=("Korea, Republic of",),
        statuses=("RECRUITING",),
        source_queries=("리바로젯 국내 모집 중 임상",),
    )
    plan = _plan("planner display query").model_copy(
        update={"clinical_query_specs": (concept,)}
    )

    contracted = _attach_lossless_contracts("리바로젯 국내 모집 중 임상", plan)

    assert contracted.clinical_query_specs == (concept,)
    compiled = compile_clinical_query(contracted.clinical_query_specs[0])
    assert compiled.parameters["query.locn"] == "Korea, Republic of"
    assert compiled.parameters["filter.overallStatus"] == "RECRUITING"


def test_planner_contract_keeps_static_queries_missing_model_concepts() -> None:
    supplied = ClinicalTrialConcept(
        ingredients=("Pitavastatin", "Ezetimibe"),
        search_area="intervention",
        match="both",
        source_queries=("planner concept query",),
    )
    plan = _plan(
        "Pitavastatin and Ezetimibe",
        "Rosuvastatin",
        "Atorvastatin",
    ).model_copy(update={"clinical_query_specs": (supplied,)})

    contracted = _attach_lossless_contracts("복합제 임상현황", plan)

    assert len(contracted.clinical_query_specs) == 3
    all_source_queries = {
        source_query
        for concept in contracted.clinical_query_specs
        for source_query in concept.source_queries
    }
    assert {
        "Pitavastatin and Ezetimibe",
        "Rosuvastatin",
        "Atorvastatin",
    }.issubset(all_source_queries)


def test_executor_forwards_clinical_concept_without_changing_other_adapters() -> None:
    concept = ClinicalTrialConcept(
        ingredients=("Pitavastatin", "Ezetimibe"),
        search_area="intervention",
        match="both",
        source_queries=("리바로젯 임상",),
    )
    observed: list[ClinicalTrialConcept] = []

    def default_adapter(query: str) -> SourceResult:
        return SourceResult(source="mart", query=query, status="empty")

    def clinical_adapter(
        query: str,
        *,
        concept: ClinicalTrialConcept,
    ) -> SourceResult:
        observed.append(concept)
        return SourceResult(source="clinicaltrials", query=query, status="empty")

    adapters = {
        "mart": default_adapter,
        "nedrug": lambda query: SourceResult(source="nedrug", query=query, status="empty"),
        "hira": lambda query: SourceResult(source="hira", query=query, status="empty"),
        "openfda": lambda query: SourceResult(source="openfda", query=query, status="empty"),
        "clinicaltrials": clinical_adapter,
        "web": lambda query: SourceResult(source="web", query=query, status="empty"),
        "patent": lambda query: SourceResult(source="patent", query=query, status="empty"),
    }
    plan = _plan("리바로젯 임상").model_copy(
        update={"clinical_query_specs": (concept,)}
    )

    outcome = ParallelSourceExecutor(adapters=adapters).execute(
        plan,
        session_id="clinical-concept",
        source_filter=("clinicaltrials",),
    )

    assert outcome[0].status == "empty"
    assert observed == [concept]


def test_executor_starts_all_static_clinical_queries_concurrently() -> None:
    lock = threading.Lock()
    active = 0
    peak_active = 0

    def clinical_adapter(query: str) -> SourceResult:
        nonlocal active, peak_active
        with lock:
            active += 1
            peak_active = max(peak_active, active)
        try:
            time.sleep(0.04)
            return SourceResult(
                source="clinicaltrials",
                query=query,
                status="empty",
            )
        finally:
            with lock:
                active -= 1

    adapters = {
        source: (
            clinical_adapter
            if source == "clinicaltrials"
            else lambda query, source=source: SourceResult(
                source=source,
                query=query,
                status="empty",
            )
        )
        for source in ("mart", "nedrug", "hira", "openfda", "clinicaltrials", "web", "patent")
    }
    queries = tuple(f"static clinical query {index}" for index in range(12))

    results = ParallelSourceExecutor(
        adapters=adapters,
        per_tool_timeout_s=1.0,
        total_timeout_s=2.0,
    ).execute(
        _plan(*queries),
        session_id="all-static-clinical",
        source_filter=("clinicaltrials",),
    )

    assert len(results) == 12
    assert peak_active == 12


def test_requested_answer_shape_records_api_price_and_horizon() -> None:
    contracted = _attach_lossless_contracts(
        "리바로 원료의약품 API 단가 최근 10년 연도별 추이",
        _plan("pitavastatin"),
    )

    shape = contracted.requested_answer_shape
    assert "api_unit_price" in shape.measure_or_attribute
    assert shape.time_horizon == "최근 10년"
    assert shape.granularity == "year"


def test_requested_answer_shape_records_active_korean_trials() -> None:
    contracted = _attach_lossless_contracts(
        "국내에서 진행 중인 리바로 임상시험",
        _plan("pitavastatin"),
    )

    shape = contracted.requested_answer_shape
    assert "active_clinical_trials" in shape.measure_or_attribute
    assert "country:KR" in shape.entities


@dataclass
class _Response:
    payload: dict[str, Any]

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


def test_clinical_client_paginates_with_100_and_preserves_manifest() -> None:
    requests_seen: list[dict[str, Any]] = []
    pages = iter(
        (
            {"totalCount": 3, "studies": [_study("NCT00000001"), _study("NCT00000002")], "nextPageToken": "p2"},
            {"totalCount": 3, "studies": [_study("NCT00000003")]},
        )
    )

    def get(_url: str, *, params: dict[str, Any], timeout: float) -> _Response:
        requests_seen.append({"params": dict(params), "timeout": timeout})
        return _Response(next(pages))

    compiled = compile_clinical_query(
        ClinicalTrialConcept(
            ingredients=("Pitavastatin", "Ezetimibe"),
            search_area="intervention",
            match="both",
            source_queries=("raw planner query",),
        )
    )

    result = ClinicalTrialsV2Client(get=get, timeout_s=5).search(compiled)

    assert [request["params"]["pageSize"] for request in requests_seen] == [100, 100]
    assert "pageToken" not in requests_seen[0]["params"]
    assert requests_seen[1]["params"]["pageToken"] == "p2"
    assert result.total_reported == 3
    assert result.records_received == 3
    assert result.records_unique == 3
    assert result.page_count == 2
    assert result.pagination_complete is True
    assert [record["nct_id"] for record in result.records] == [
        "NCT00000001",
        "NCT00000002",
        "NCT00000003",
    ]
    assert result.query_manifest["source_queries"] == ["raw planner query"]


def test_clinical_client_discloses_safety_cap_without_silent_truncation() -> None:
    responses = iter(
        (
            {"totalCount": 4, "studies": [_study("NCT00000001"), _study("NCT00000002")], "nextPageToken": "p2"},
        )
    )

    def get(_url: str, *, params: dict[str, Any], timeout: float) -> _Response:
        return _Response(next(responses))

    compiled = compile_clinical_query(
        ClinicalTrialConcept(
            ingredients=("Pitavastatin",),
            search_area="intervention",
            source_queries=("pitavastatin",),
        )
    )

    result = ClinicalTrialsV2Client(get=get, timeout_s=5, record_cap=2).search(compiled)

    assert result.records_received == 2
    assert result.pagination_complete is False
    assert result.partial_reason == "원천 검색 4건 중 안전 상한 2건만 수신한 부분 결과"


def test_clinical_normalizer_preserves_all_named_fields_without_inference() -> None:
    record = normalize_clinical_study(_study("NCT00000001"), matched_queries=("q1",))

    assert record == {
        "nct_id": "NCT00000001",
        "brief_title": "Brief NCT00000001",
        "official_title": "Official NCT00000001",
        "study_type": "INTERVENTIONAL",
        "phases": ["PHASE_NA"],
        "overall_status": "COMPLETED",
        "conditions": ["Hyperlipidemia"],
        "interventions": ["Pitavastatin", "Ezetimibe"],
        "comparators": ["Comparator"],
        "sponsor": "JW Pharmaceutical",
        "enrollment": {"count": 120, "type": "ACTUAL"},
        "start_date": "2023-01-01",
        "primary_completion_date": "2024-01-01",
        "completion_date": "2024-03-01",
        "last_update_date": "2024-04-01",
        "countries": ["Korea, Republic of", "United States"],
        "has_results": False,
        "matched_query": ["q1"],
        "url": "https://clinicaltrials.gov/study/NCT00000001",
    }
    assert "success" not in record
    assert "approval" not in record


def test_clinical_search_union_deduplicates_nct_and_preserves_matching_queries() -> None:
    left = {
        "query_manifest": {"query_id": "q1"},
        "records": [normalize_clinical_study(_study("NCT00000001"), matched_queries=("q1",))],
    }
    right = {
        "query_manifest": {"query_id": "q2"},
        "records": [
            normalize_clinical_study(_study("NCT00000001"), matched_queries=("q2",)),
            normalize_clinical_study(_study("NCT00000002"), matched_queries=("q2",)),
        ],
    }

    merged = merge_clinical_searches((left, right))

    assert [record["nct_id"] for record in merged] == ["NCT00000001", "NCT00000002"]
    assert merged[0]["matched_query"] == ["q1", "q2"]


def test_v4_clinical_adapter_exposes_lossless_counts_and_manifest(monkeypatch) -> None:
    normalized = normalize_clinical_study(_study("NCT00000001"), matched_queries=("raw",))

    def search(self: ClinicalTrialsV2Client, compiled) -> ClinicalSearchResult:
        return ClinicalSearchResult(
            records=(normalized,),
            total_reported=1,
            records_received=1,
            records_unique=1,
            page_count=1,
            pagination_complete=True,
            partial_reason=None,
            query_manifest={
                "query_id": compiled.query_id,
                "source_queries": ["raw"],
                "total_reported": 1,
                "records_received": 1,
                "records_unique": 1,
                "page_count": 1,
                "pagination_complete": True,
                "partial_reason": None,
            },
            elapsed_ms=12.3,
        )

    monkeypatch.setattr(ClinicalTrialsV2Client, "search", search)
    call = _clinical_lossless_external_call(
        "raw",
        ClinicalTrialConcept(ingredients=("Pitavastatin",), source_queries=("raw",)),
        timeout_s=5,
    )

    assert call.status == "live"
    assert call.source == "clinicaltrials_api_v2"
    assert call.render_data["coverage"] == {
        "total_reported": 1,
        "records_received": 1,
        "records_unique": 1,
        "page_count": 1,
        "pagination_complete": True,
        "partial_reason": None,
    }
    assert call.render_data["query_manifest"]["source_queries"] == ["raw"]
    assert call.render_data["payload"]["studies"][0]["nct_id"] == "NCT00000001"


def test_patent_payload_keeps_kr_us_and_news_in_separate_lanes() -> None:
    kr_call = {
        "tool": "mfds_patent",
        "source": "식품의약품안전처",
        "status": "live",
        "safe_url": "https://example.test/mfds",
        "render_data": {
            "items": [
                {
                    "ITEM_NAME": "리바로정2밀리그램",
                    "INGR_ENG_NAME": "Pitavastatin Calcium",
                    "DOMESTIC_PATENT_NO": "10-0777553",
                    "DOMESTIC_PATENT_STATUS": "소멸",
                    "DOMESTIC_END_DATE": "2010-11-12",
                    "PATENTEE": "닛산 가가쿠",
                }
            ]
        },
    }
    us_call = {
        "tool": "mfds_fda_orangebook",
        "source": "FDA Orange Book",
        "status": "live",
        "safe_url": "https://example.test/orange-book",
        "render_data": {
            "items": [
                {
                    "PRT_NAME": "LIVALO",
                    "INGR_NAME": "Pitavastatin Calcium",
                    "KOR_PAT_NO": "8557993",
                    "KOR_STATUS": "소멸",
                    "KOR_EXP_DATE": "2024-02-02 00:00:00",
                    "KOR_APPLICANT": "NISSAN CHEMICAL CORPORATION",
                }
            ]
        },
    }
    news_call = {
        "tool": "web_search",
        "source": "Tavily",
        "status": "live",
        "safe_url": "https://example.test/news",
        "render_data": {
            "items": [
                {
                    "title": "리바로 특허 관련 보도",
                    "url": "https://news.example.test/patent",
                    "snippet": "최근 특허 관련 동향",
                }
            ]
        },
    }

    payload = build_patent_lane_payload(
        kr_calls=(kr_call,),
        us_calls=(us_call,),
        news_calls=(news_call,),
    )

    assert set(payload) == {"kr_primary", "us_secondary", "news"}
    assert payload["kr_primary"]["scope"] == "KR_PRIMARY"
    assert payload["kr_primary"]["records"][0]["patent_no"] == "10-0777553"
    assert payload["us_secondary"]["scope"] == "US_REFERENCE_ONLY"
    assert payload["us_secondary"]["records"][0]["patent_no"] == "8557993"
    assert payload["news"]["scope"] == "CONTEXT_ONLY"
    assert payload["news"]["records"][0]["title"] == "리바로 특허 관련 보도"
    assert all("10-0777553" not in str(record) for record in payload["us_secondary"]["records"])
    assert all("8557993" not in str(record) for record in payload["kr_primary"]["records"])


def test_patent_payload_deduplicates_only_within_each_lane() -> None:
    shared_kr = {
        "tool": "mfds_patent",
        "source": "식품의약품안전처",
        "status": "live",
        "render_data": {
            "items": [
                {
                    "ITEM_NAME": "리바로정",
                    "DOMESTIC_PATENT_NO": "10-0000001",
                    "DOMESTIC_END_DATE": "2030-01-01",
                },
                {
                    "ITEM_NAME": "리바로정",
                    "DOMESTIC_PATENT_NO": "10-0000001",
                    "DOMESTIC_END_DATE": "2030-01-01",
                },
            ]
        },
    }

    payload = build_patent_lane_payload(
        kr_calls=(shared_kr,),
        us_calls=(),
        news_calls=(),
    )

    assert payload["kr_primary"]["records_received"] == 2
    assert payload["kr_primary"]["records_unique"] == 1
    assert payload["us_secondary"]["records"] == []
    assert payload["news"]["records"] == []


def test_patent_adapter_calls_three_authority_lanes_without_nedrug_duplication(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from jw_chat_agent_poc.agent_loop import factory
    from jw_chat_agent_poc.service import general_view_routing
    from jw_chat_agent_poc.service.v4 import adapters as v4_adapters
    from jw_chat_agent_poc.tools.external.client import ExternalCall

    called: list[tuple[str, str]] = []

    def external_call(
        tool: str,
        source: str,
        item: dict[str, Any],
    ) -> ExternalCall:
        return ExternalCall(
            tool=tool,
            source=source,
            status="live",
            summary_text=f"{tool} result",
            render_data={"items": [item]},
            safe_url=f"https://example.test/{tool}",
        )

    class Resolver:
        def resolve(self, _query, *, allow_default):
            assert allow_default is False
            return SimpleNamespace(
                canonical_brand="리바로",
                molecule_en=("Pitavastatin",),
                market_ids=(),
            )

    class External:
        timeout_s = 12

        def mfds_patent(self, ingredient):
            called.append(("kr", ingredient))
            return external_call(
                "mfds_patent",
                "식품의약품안전처",
                {"DOMESTIC_PATENT_NO": "10-0777553", "ITEM_NAME": "리바로정"},
            )

        def mfds_fda_orangebook(self, ingredient):
            called.append(("us", ingredient))
            return external_call(
                "mfds_fda_orangebook",
                "FDA Orange Book",
                {"KOR_PAT_NO": "8557993", "PRT_NAME": "LIVALO"},
            )

        def web_search(self, query, *, topic="general"):
            called.append(("news", f"{topic}:{query}"))
            return external_call(
                "web_search",
                "Tavily",
                {"title": "리바로 특허 관련 보도", "url": "https://news.test/1"},
            )

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
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "tavily")

    result = v4_adapters.build_source_adapters()["patent"]("리바로 특허 만료")

    assert called[0:2] == [("kr", "Pitavastatin"), ("us", "Pitavastatin")]
    assert called[2][0] == "news"
    assert called[2][1].startswith("news:")
    assert result.status == "ok"
    assert set(result.payload["patent_lanes"]) == {
        "kr_primary",
        "us_secondary",
        "news",
    }


def test_clinical_adapter_treats_korean_conjunction_as_combined(
    monkeypatch,
) -> None:
    from jw_chat_agent_poc.agent_loop import factory
    from jw_chat_agent_poc.service import general_view_routing
    from jw_chat_agent_poc.tools.external.client import ExternalCall

    captured: list[ClinicalTrialConcept] = []

    class Resolver:
        def resolve(self, _query, *, allow_default):
            assert allow_default is False
            return SimpleNamespace(
                canonical_brand="리바로젯",
                molecule_en=("Pitavastatin", "Ezetimibe"),
                market_ids=(),
            )

    class External:
        timeout_s = 12

    def clinical_call(
        _query: str,
        concept: ClinicalTrialConcept,
        *,
        timeout_s: float,
    ) -> ExternalCall:
        assert timeout_s == 12
        captured.append(concept)
        return ExternalCall(
            tool="clinicaltrials_v2_lossless_search",
            source="clinicaltrials_api_v2",
            status="no_data",
            summary_text="no records",
            render_data={"payload": {"studies": []}},
            safe_url="https://clinicaltrials.gov/api/v2/studies",
        )

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
    monkeypatch.setattr(v4_adapters, "_clinical_lossless_external_call", clinical_call)

    v4_adapters.build_source_adapters()["clinicaltrials"](
        "피타바스타틴 및 에제티미브 임상현황"
    )

    assert len(captured) == 1
    assert captured[0].ingredients == ("Pitavastatin", "Ezetimibe")
    assert captured[0].match == "both"


def _clinical_source_result(
    query: str,
    records: list[dict[str, Any]],
    *,
    total: int,
) -> SourceResult:
    return SourceResult(
        source="clinicaltrials",
        query=query,
        status="ok",
        payload={
            "calls": [
                {
                    "tool": "clinicaltrials_v2_lossless_search",
                    "source": "clinicaltrials_api_v2",
                    "status": "live",
                    "safe_url": "https://clinicaltrials.gov/api/v2/studies",
                    "render_data": {
                        "payload": {"studies": records, "totalCount": total},
                        "query_manifest": {
                            "query_id": query,
                            "compiled_expression": query,
                            "source_queries": [query],
                            "total_reported": total,
                            "records_received": len(records),
                            "records_unique": len(records),
                            "page_count": 1,
                            "pagination_complete": True,
                            "partial_reason": None,
                        },
                        "coverage": {
                            "total_reported": total,
                            "records_received": len(records),
                            "records_unique": len(records),
                            "page_count": 1,
                            "pagination_complete": True,
                            "partial_reason": None,
                        },
                    },
                }
            ]
        },
    )


def _runtime_answer(
    monkeypatch,
    *,
    plan: PlannerOutput,
    results: tuple[SourceResult, ...],
    commentary: str,
    synthesis_trace: dict[str, Any] | None = None,
    lossless_mode: str = "inject",
    request_satisfaction_mode: str = "shadow",
):
    monkeypatch.setenv("CHAT_V4_LOSSLESS_SPINE_MODE", lossless_mode)
    monkeypatch.setenv(
        "CHAT_V4_REQUEST_SATISFACTION_MODE",
        request_satisfaction_mode,
    )

    class Planner:
        def plan(self, _question, _turns, *, budget_s):
            return plan

        def link(self, *_args, **_kwargs):
            return None

    class Executor:
        def execute_with_trace(self, _plan, **_kwargs):
            return SimpleNamespace(
                results=results,
                trace={"elapsed_ms": 1.0, "tools": []},
            )

    class Synthesizer:
        def synthesize_with_trace(
            self,
            _plan,
            _results,
            _turns,
            *,
            budget_s,
            deterministic_facts,
        ):
            return SynthesisOutcome(
                text=commentary,
                trace={
                    "elapsed_ms": 1.0,
                    "usage": {},
                    **(synthesis_trace or {}),
                },
            )

    return V4Runtime(
        planner=Planner(),
        executor=Executor(),
        synthesizer=Synthesizer(),
    ).answer(plan.resolved_question, conversation_id="lossless-runtime", turns=())


def test_lossless_clinical_spine_unions_queries_and_renders_every_record() -> None:
    first = normalize_clinical_study(_study("NCT00000001"), matched_queries=("q1",))
    overlap = normalize_clinical_study(_study("NCT00000002"), matched_queries=("q1",))
    overlap_2 = normalize_clinical_study(_study("NCT00000002"), matched_queries=("q2",))
    third = normalize_clinical_study(_study("NCT00000003"), matched_queries=("q2",))
    plan = _plan("q1", "q2").model_copy(
        update={"resolved_question": "리바로젯 제네릭 임상현황"}
    )

    evidence_sets = build_evidence_sets(
        plan,
        (
            _clinical_source_result("q1", [first, overlap], total=2),
            _clinical_source_result("q2", [overlap_2, third], total=2),
        ),
        observed_on=date(2026, 8, 12),
    )
    rendered = render_deterministic_facts(
        plan,
        evidence_sets,
        observed_on=date(2026, 8, 12),
    )

    clinical = next(item for item in evidence_sets if item.source == "clinicaltrials")
    assert clinical.coverage.total_reported == 4
    assert clinical.coverage.records_received == 4
    assert clinical.coverage.records_unique == 3
    assert len(clinical.query_manifest) == 2
    assert clinical.records[1].payload["matched_query"] == ["q1", "q2"]
    assert rendered.profile == "clinical_portfolio"
    assert rendered.coverage.records_rendered == 3
    assert "원천 검색 4건 · 수신 4건 · 중복 제거 후 3건 · 상세 표시 3건" in rendered.text
    assert rendered.text.count("NCT00000001") >= 1
    assert rendered.text.count("NCT00000002") >= 1
    assert rendered.text.count("NCT00000003") >= 1
    assert rendered.required_field_surface_rate == 1.0


def test_clinical_timeout_is_partial_unknown_not_complete_zero() -> None:
    plan = _plan("pitavastatin")
    timed_out = SourceResult(
        source="clinicaltrials",
        query="pitavastatin",
        status="timeout",
        notice="응답 지연으로 미포함",
    )

    clinical = build_evidence_sets(
        plan,
        (timed_out,),
        observed_on=date(2026, 8, 12),
    )[0]

    assert clinical.coverage.total_reported is None
    assert clinical.coverage.pagination_complete is False
    assert clinical.coverage.partial_reasons
    assert clinical.item_failures[0]["status"] == "timeout"


def test_clinical_call_timeout_is_preserved_as_item_failure() -> None:
    plan = _plan("pitavastatin")
    result = SourceResult(
        source="clinicaltrials",
        query="pitavastatin",
        status="ok",
        payload={
            "calls": [
                {
                    "tool": "clinicaltrials_v2_lossless_search",
                    "status": "timeout",
                    "summary_text": "upstream timeout",
                    "render_data": {
                        "payload": {"studies": []},
                        "coverage": {
                            "total_reported": None,
                            "records_received": 0,
                            "pagination_complete": False,
                            "partial_reason": "upstream timeout",
                        },
                    },
                }
            ]
        },
    )

    clinical = build_evidence_sets(
        plan,
        (result,),
        observed_on=date(2026, 8, 12),
    )[0]

    assert clinical.coverage.total_reported is None
    assert clinical.coverage.pagination_complete is False
    assert clinical.item_failures == (
        {
            "tool": "clinicaltrials_v2_lossless_search",
            "status": "timeout",
            "summary": "upstream timeout",
        },
    )


def test_clinical_portfolio_keeps_full_table_and_major_cards_above_twelve() -> None:
    records = [
        normalize_clinical_study(_study(f"NCT{index:08d}"), matched_queries=("q",))
        for index in range(1, 14)
    ]
    plan = _plan("q")

    rendered = render_deterministic_facts(
        plan,
        build_evidence_sets(
            plan,
            (_clinical_source_result("q", records, total=13),),
            observed_on=date(2026, 8, 12),
        ),
        observed_on=date(2026, 8, 12),
    )

    assert rendered.coverage.records_rendered == 13
    assert all(f"NCT{index:08d}" in rendered.text for index in range(1, 14))
    assert "## 주요 임상시험 건별 상세 (12건)" in rendered.text
    assert rendered.text.count("### NCT") == 12


def test_lossless_timeout_composition_keeps_full_clinical_facts() -> None:
    records = [
        normalize_clinical_study(_study(f"NCT{index:08d}"), matched_queries=("q",))
        for index in range(1, 4)
    ]
    plan = _plan("q")
    rendered = render_deterministic_facts(
        plan,
        build_evidence_sets(
            plan,
            (_clinical_source_result("q", records, total=3),),
            observed_on=date(2026, 8, 12),
        ),
        observed_on=date(2026, 8, 12),
    )

    composed = compose_lossless_answer(
        rendered,
        "이번 조회에서 확인된 근거가 없어 구체적인 답을 구성하지 못했습니다.",
        synthesis_trace={"status": "fallback", "fallback_reason": "empty_or_transport_error"},
        mode="inject",
    )

    assert "자동 해설 생성이 완료되지 않았습니다" in composed.text
    assert "구체적인 답을 구성하지 못했습니다" not in composed.text
    assert all(f"NCT{index:08d}" in composed.text for index in range(1, 4))
    assert composed.fallback_detail_retention_rate == 1.0


def test_request_satisfaction_notice_has_a_separate_inject_only_flag() -> None:
    records = [normalize_clinical_study(_study("NCT00000001"), matched_queries=("q",))]
    plan = _attach_lossless_contracts(
        "국내에서 진행 중인 리바로 임상시험",
        _plan("q").model_copy(
            update={"resolved_question": "국내에서 진행 중인 리바로 임상시험"}
        ),
    )
    rendered = render_deterministic_facts(
        plan,
        build_evidence_sets(
            plan,
            (_clinical_source_result("q", records, total=1),),
            observed_on=date(2026, 8, 12),
        ),
        observed_on=date(2026, 8, 12),
    )
    notice = "요청하신 국내 진행 중 임상시험은 현재 연결된 원천에서 확인되지 않았습니다."
    assert rendered.request_notice and notice in rendered.request_notice

    shadow_notice = compose_lossless_answer(
        rendered,
        "확인된 임상 해설입니다.",
        synthesis_trace={"status": "synthesized"},
        mode="inject",
        request_satisfaction_mode="shadow",
    )
    inject_notice = compose_lossless_answer(
        rendered,
        "확인된 임상 해설입니다.",
        synthesis_trace={"status": "synthesized"},
        mode="inject",
        request_satisfaction_mode="inject",
    )

    assert notice not in shadow_notice.text
    assert notice in inject_notice.text
    assert shadow_notice.trace["request_satisfaction_mode"] == "shadow"
    assert inject_notice.trace["request_satisfaction_mode"] == "inject"
    assert shadow_notice.trace["request_notice_observed"] is True


def test_request_satisfaction_injects_for_market_profile_independently() -> None:
    plan = _attach_lossless_contracts(
        "리바로 원료의약품 API 단가",
        _plan("리바로").model_copy(
            update={
                "resolved_question": "리바로 원료의약품 API 단가",
                "answer_sources": ("mart",),
            }
        ),
    )
    rendered = render_deterministic_facts(
        plan,
        (),
        observed_on=date(2026, 8, 12),
    )
    commentary = "리바로의 관련 브랜드 매출은 확인됩니다."

    composed = compose_lossless_answer(
        rendered,
        commentary,
        synthesis_trace={"status": "synthesized"},
        mode="shadow",
        request_satisfaction_mode="inject",
    )

    assert rendered.profile == "market_analysis"
    assert composed.text.startswith(
        "요청하신 API 단가는 현재 연결된 원천에서 확인되지 않았습니다."
    )
    assert composed.text.endswith(commentary)
    assert composed.trace["request_notice_injected"] is True


def test_empty_api_unit_price_is_not_treated_as_request_satisfaction() -> None:
    plan = _attach_lossless_contracts(
        "리바로 원료의약품 API 단가",
        _plan("리바로").model_copy(
            update={
                "resolved_question": "리바로 원료의약품 API 단가",
                "answer_sources": ("web",),
            }
        ),
    )
    result = SourceResult(
        source="web",
        query="리바로 원료의약품 API 단가",
        status="ok",
        payload={"api_unit_price": None, "related_market_sales": 123},
    )

    rendered = render_deterministic_facts(
        plan,
        build_evidence_sets(plan, (result,), observed_on=date(2026, 8, 12)),
        observed_on=date(2026, 8, 12),
    )

    assert rendered.request_notice is not None
    assert "API 단가" in rendered.request_notice


def test_policy_coverage_distinguishes_received_from_unique_records() -> None:
    duplicate_call = {
        "tool": "hira_reimbursement_detail",
        "status": "ok",
        "safe_url": "https://www.hira.or.kr/criterion/101",
        "render_data": {
            "notice_number": "고시 제2026-101호",
            "title": "급여기준",
            "raw_text": "투여대상 환자",
            "source_url": "https://www.hira.or.kr/criterion/101",
        },
    }
    plan = _plan("hira").model_copy(
        update={"resolved_question": "아일리아 급여기준", "answer_sources": ("hira",)}
    )
    result = SourceResult(
        source="hira",
        query="아일리아 급여기준",
        status="ok",
        payload={"calls": [duplicate_call, duplicate_call]},
    )

    policy = build_evidence_sets(
        plan,
        (result,),
        observed_on=date(2026, 8, 12),
    )[0]

    assert policy.coverage.records_received == 2
    assert policy.coverage.records_unique == 1
    assert len(policy.records) == 1


def test_ten_year_notice_names_available_range_without_substitution() -> None:
    plan = _attach_lossless_contracts(
        "리바로 최근 10년 연도별 추이",
        _plan("리바로").model_copy(
            update={
                "resolved_question": "리바로 최근 10년 연도별 추이",
                "answer_sources": ("mart",),
            }
        ),
    )
    result = SourceResult(
        source="web",
        query="리바로 최근 10년 연도별 추이",
        status="ok",
        payload={
            "calls": [
                {
                    "series": [
                        {"period": "2024-12", "sales": 10},
                        {"period": "2025-12", "sales": 11},
                        {"period": "2026-06", "sales": 12},
                    ]
                }
            ]
        },
    )

    rendered = render_deterministic_facts(
        plan,
        build_evidence_sets(plan, (result,), observed_on=date(2026, 8, 12)),
        observed_on=date(2026, 8, 12),
    )
    notice = rendered.request_notice

    assert notice is not None
    assert "2024~2026년" in notice
    assert "대체값이 아닙니다" in notice


def test_active_korean_trial_does_not_emit_missing_request_notice() -> None:
    active = normalize_clinical_study(_study("NCT00000001"), matched_queries=("q",))
    active.update(
        {
            "overall_status": "RECRUITING",
            "countries": ["Korea, Republic of"],
        }
    )
    plan = _attach_lossless_contracts(
        "국내에서 진행 중인 리바로 임상시험",
        _plan("q").model_copy(
            update={"resolved_question": "국내에서 진행 중인 리바로 임상시험"}
        ),
    )

    rendered = render_deterministic_facts(
        plan,
        build_evidence_sets(
            plan,
            (_clinical_source_result("q", [active], total=1),),
            observed_on=date(2026, 8, 12),
        ),
        observed_on=date(2026, 8, 12),
    )

    assert rendered.request_notice is None


def test_lossless_patent_renderer_keeps_three_lanes_and_bounded_wording() -> None:
    result = SourceResult(
        source="patent",
        query="리바로젯 특허현황",
        status="ok",
        payload={
            "patent_lanes": build_patent_lane_payload(
                kr_calls=(
                    {
                        "tool": "mfds_patent",
                        "source": "식품의약품안전처",
                        "safe_url": "https://nedrug.mfds.go.kr/pbp/CCBBF01",
                        "render_data": {
                            "items": [
                                {
                                    "ITEM_NAME": "리바로젯정",
                                    "DOMESTIC_PATENT_NO": "10-0777553",
                                    "INVENTION_TITLE": "지질 저하 복합제",
                                    "DOMESTIC_PATENT_STATUS": "말소",
                                    "DOMESTIC_END_DATE": "2030-11-12",
                                }
                            ]
                        },
                    },
                ),
                us_calls=(
                    {
                        "tool": "mfds_fda_orangebook",
                        "source": "FDA Orange Book",
                        "safe_url": "https://www.accessdata.fda.gov/scripts/cder/ob/",
                        "render_data": {
                            "items": [
                                {
                                    "PRT_NAME": "LIVALO",
                                    "KOR_PAT_NO": "8557993",
                                    "KOR_STATUS": "ACTIVE",
                                    "KOR_EXP_DATE": "2028-02-02",
                                }
                            ]
                        },
                    },
                ),
                news_calls=(
                    {
                        "tool": "web_search",
                        "source": "Tavily",
                        "render_data": {
                            "items": [
                                {
                                    "title": "특허 분쟁 보도",
                                    "url": "https://news.example.test/patent",
                                    "event_date": "2026-07-31",
                                    "published_at": "2026-08-01",
                                }
                            ]
                        },
                    },
                ),
            )
        },
    )
    plan = _plan("q").model_copy(
        update={
            "resolved_question": "리바로젯 특허현황",
            "answer_sources": ("patent",),
        }
    )

    evidence_sets = build_evidence_sets(
        plan,
        (result,),
        observed_on=date(2026, 8, 12),
    )
    rendered = render_deterministic_facts(
        plan,
        evidence_sets,
        observed_on=date(2026, 8, 12),
    )

    structured_records = [
        record
        for record in evidence_sets[0].records
        if record.result_kind == "structured_patent_record"
    ]
    assert structured_records[0].payload["listed_status"] == "말소"
    assert structured_records[0].payload["jurisdiction"] == "KR"
    assert structured_records[0].payload["as_of_date"] == "2026-08-12"
    assert structured_records[1].payload["listed_status"] == "ACTIVE"
    assert structured_records[1].payload["jurisdiction"] == "US"
    assert structured_records[1].payload["as_of_date"] == "2026-08-12"

    assert rendered.profile == "patent_portfolio"
    assert "2026-08-12 조회 기준 NeDrug 특허목록상 상태 '말소'" in rendered.text
    assert "목록상 존속기간만료일 2030-11-12" in rendered.text
    assert "## 미국 Orange Book 보조표" in rendered.text
    assert "## 뉴스 맥락" in rendered.text
    assert "2026-07-31" in rendered.text and "2026-08-01" in rendered.text
    assert "제네릭 진입 가능" not in rendered.text
    assert "특허가 끝났다" not in rendered.text
    assert "만료일에 말소" not in rendered.text

    composed = compose_lossless_answer(
        rendered,
        "이번 조회에서 확인된 근거가 없어 구체적인 답을 구성하지 못했습니다.",
        synthesis_trace={"status": "fallback", "fallback_reason": "timeout"},
        mode="inject",
        request_satisfaction_mode="shadow",
    )
    assert "10-0777553" in composed.text
    assert "8557993" in composed.text
    assert "특허 분쟁 보도" in composed.text
    assert "구체적인 답을 구성하지 못했습니다" not in composed.text
    assert composed.fallback_detail_retention_rate == 1.0


def test_lossless_patent_renderer_surfaces_as_of_date_for_us_only_records() -> None:
    result = SourceResult(
        source="patent",
        query="리바로 미국 특허현황",
        status="ok",
        payload={
            "patent_lanes": build_patent_lane_payload(
                kr_calls=(),
                us_calls=(
                    {
                        "tool": "mfds_fda_orangebook",
                        "source": "FDA Orange Book",
                        "safe_url": "https://www.accessdata.fda.gov/scripts/cder/ob/",
                        "render_data": {
                            "items": [
                                {
                                    "PRT_NAME": "LIVALO",
                                    "KOR_PAT_NO": "8557993",
                                    "KOR_STATUS": "ACTIVE",
                                    "KOR_EXP_DATE": "2028-02-02",
                                }
                            ]
                        },
                    },
                ),
                news_calls=(),
            )
        },
    )
    plan = _plan("q").model_copy(
        update={
            "resolved_question": "리바로 미국 특허현황",
            "answer_sources": ("patent",),
        }
    )

    rendered = render_deterministic_facts(
        plan,
        build_evidence_sets(
            plan,
            (result,),
            observed_on=date(2026, 8, 12),
        ),
        observed_on=date(2026, 8, 12),
    )

    assert "2026-08-12 조회 기준" in rendered.text
    assert rendered.required_field_surface_rate == 1.0


def test_lossless_policy_renderer_retains_sections_and_full_raw_text() -> None:
    raw_text = """고시 제2026-101호
시행일 2026-08-01
1) 투여대상
황반변성 환자 중 기준을 충족한 경우
2) 제외기준
치료 효과가 없는 경우
3) 투여방법 및 횟수
4주 간격으로 투여하며 초기 3회
■ 고시 개정 사유
대상 환자 기준을 명확히 함"""
    result = SourceResult(
        source="hira",
        query="아일리아 급여기준",
        status="ok",
        payload={
            "calls": [
                {
                    "tool": "hira_reimbursement_detail",
                    "source": "hira_reimbursement",
                    "status": "ok",
                    "safe_url": "https://www.hira.or.kr/criterion/101",
                    "render_data": {
                        "brand_name": "아일리아",
                        "title": "아일리아 급여기준",
                        "raw_text": raw_text,
                        "source_date": "2026-08-01",
                        "notice_number": "고시 제2026-101호",
                        "source_url": "https://www.hira.or.kr/criterion/101",
                    },
                }
            ]
        },
    )
    plan = _plan("q").model_copy(
        update={"resolved_question": "아일리아 급여기준", "answer_sources": ("hira",)}
    )

    rendered = render_deterministic_facts(
        plan,
        build_evidence_sets(plan, (result,), observed_on=date(2026, 8, 12)),
        observed_on=date(2026, 8, 12),
    )

    assert rendered.profile == "policy_document"
    assert all(
        heading in rendered.text
        for heading in ("## 투여대상", "## 제외기준", "## 투여 방법 및 횟수", "## 개정 사유")
    )
    assert raw_text in rendered.text
    assert rendered.required_field_surface_rate == 1.0

    composed = compose_lossless_answer(
        rendered,
        "이번 조회에서 확인된 근거가 없어 구체적인 답을 구성하지 못했습니다.",
        synthesis_trace={"status": "fallback", "fallback_reason": "timeout"},
        mode="inject",
        request_satisfaction_mode="shadow",
    )
    assert all(
        value in composed.text
        for value in (
            "고시 제2026-101호",
            "2026-08-01",
            "황반변성 환자 중 기준을 충족한 경우",
            "치료 효과가 없는 경우",
            "4주 간격으로 투여하며 초기 3회",
            raw_text,
        )
    )
    assert "구체적인 답을 구성하지 못했습니다" not in composed.text
    assert composed.fallback_detail_retention_rate == 1.0


def test_lossless_shadow_and_market_profile_never_mutate_existing_answer() -> None:
    plan = _plan("q").model_copy(
        update={"resolved_question": "리바로 매출 알려줘", "answer_sources": ("mart",)}
    )
    rendered = render_deterministic_facts(
        plan,
        build_evidence_sets(plan, (), observed_on=date(2026, 8, 12)),
        observed_on=date(2026, 8, 12),
    )
    original = "## 핵심 답\n기존 시장 답변"

    assert rendered.profile == "market_analysis"
    assert compose_lossless_answer(
        rendered, original, synthesis_trace={"status": "synthesized"}, mode="inject"
    ).text == original

    external_render = rendered.model_copy(
        update={"profile": "clinical_portfolio", "text": "## 임상시험 전건\nNCT00000001"}
    )
    assert compose_lossless_answer(
        external_render,
        original,
        synthesis_trace={"status": "synthesized"},
        mode="shadow",
    ).text == original


def test_market_primary_source_is_not_replaced_by_incidental_clinical_records() -> None:
    plan = _plan("pitavastatin").model_copy(
        update={
            "resolved_question": (
                "리바로의 최근 매출 현황, 시장 점유율, 임상 시험 결과, "
                "안전성 정보 및 관련 뉴스는 어떠한가?"
            ),
            "answer_sources": ("mart",),
        }
    )
    clinical = normalize_clinical_study(
        _study("NCT00000001"),
        matched_queries=("pitavastatin",),
    )
    rendered = render_deterministic_facts(
        plan,
        build_evidence_sets(
            plan,
            (_clinical_source_result("pitavastatin", [clinical], total=1),),
            observed_on=date(2026, 8, 12),
        ),
        observed_on=date(2026, 8, 12),
    )
    original = "## 핵심 답\n기존 5단 시장 답변"

    assert rendered.profile == "market_analysis"
    assert compose_lossless_answer(
        rendered,
        original,
        synthesis_trace={"status": "synthesized"},
        mode="inject",
    ).text == original


def test_source_gate_returns_more_than_five_public_references() -> None:
    now = datetime.now(UTC)
    result = SourceResult(
        source="clinicaltrials",
        query="임상",
        status="ok",
        payload={
            "records": [
                {"url": f"https://clinicaltrials.gov/study/NCT{index:08d}"}
                for index in range(1, 8)
            ]
        },
        citations=tuple(
            Citation(
                source="ClinicalTrials.gov",
                query="임상",
                url=f"https://clinicaltrials.gov/study/NCT{index:08d}",
                retrieved_at=now,
            )
            for index in range(1, 8)
        ),
    )

    answer = apply_v4_gates("임상 현황", "확인된 임상입니다.", (result,)).text

    assert sum(f"NCT{index:08d}" in answer for index in range(1, 8)) == 7


def test_normal_lossless_composition_keeps_every_source_reference() -> None:
    rendered = render_deterministic_facts(
        _plan("q"),
        build_evidence_sets(
            _plan("q"),
            (_clinical_source_result("q", [normalize_clinical_study(_study("NCT00000001"))], total=1),),
            observed_on=date(2026, 8, 12),
        ),
        observed_on=date(2026, 8, 12),
    ).model_copy(
        update={
            "source_refs": tuple(
                SourceReference(url=f"https://clinicaltrials.gov/study/NCT{index:08d}")
                for index in range(1, 8)
            )
        }
    )

    composed = compose_lossless_answer(
        rendered,
        "임상시험 해설입니다.",
        synthesis_trace={"status": "synthesized"},
        mode="inject",
        request_satisfaction_mode="shadow",
    )

    assert sum(f"NCT{index:08d}" in composed.text for index in range(1, 8)) == 7


def test_runtime_inject_mode_composes_deterministic_facts_before_commentary(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CHAT_V4_LOSSLESS_SPINE_MODE", "inject")
    plan = _plan("pitavastatin").model_copy(
        update={"resolved_question": "리바로젯 임상현황"}
    )
    result = _clinical_source_result(
        "pitavastatin",
        [
            normalize_clinical_study(
                _study("NCT00000001"),
                matched_queries=("pitavastatin",),
            )
        ],
        total=1,
    )

    class Planner:
        def plan(self, _question, _turns, *, budget_s):
            return plan

        def link(self, *_args, **_kwargs):
            return None

    class Executor:
        def execute_with_trace(self, _plan, **_kwargs):
            return SimpleNamespace(
                results=(result,),
                trace={"elapsed_ms": 1.0, "tools": []},
            )

    class Synthesizer:
        def synthesize_with_trace(
            self,
            _plan,
            _results,
            _turns,
            *,
            budget_s,
            deterministic_facts,
        ):
            assert "NCT00000001" in deterministic_facts
            return SynthesisOutcome(
                text="임상 포트폴리오 해설입니다.",
                trace={"elapsed_ms": 1.0, "usage": {}},
            )

    answer = V4Runtime(
        planner=Planner(),
        executor=Executor(),
        synthesizer=Synthesizer(),
    ).answer("리바로젯 임상현황", conversation_id="lossless", turns=())

    assert answer.text.startswith("## 조사 범위와 완전성")
    assert "NCT00000001" in answer.text
    assert "## 자동 해설\n임상 포트폴리오 해설입니다." in answer.text
    assert answer.trace["lossless_spine"]["answer_mutation"] is True
    assert answer.trace["lossless_spine"]["records_rendered"] == 1


def test_runtime_market_profile_is_byte_invariant_between_shadow_and_inject(
    monkeypatch,
) -> None:
    plan = _plan("리바로").model_copy(
        update={
            "resolved_question": "리바로 매출 알려줘",
            "answer_sources": ("mart",),
        }
    )
    result = SourceResult(
        source="mart",
        query="리바로 매출 알려줘",
        status="ok",
        payload={"calls": [{"metric": "sales", "value": 100}]},
    )
    commentary = "## 핵심 답\n기존 시장 답변\n\n## 출처\n- UBIST"

    shadow = _runtime_answer(
        monkeypatch,
        plan=plan,
        results=(result,),
        commentary=commentary,
        lossless_mode="shadow",
    )
    inject = _runtime_answer(
        monkeypatch,
        plan=plan,
        results=(result,),
        commentary=commentary,
        lossless_mode="inject",
    )

    assert inject.text == shadow.text
    assert inject.trace["lossless_spine"]["profile"] == "market_analysis"
    assert inject.trace["lossless_spine"]["answer_mutation"] is False


def test_runtime_request_satisfaction_flag_only_adds_a_notice_in_inject_mode(
    monkeypatch,
) -> None:
    plan = _attach_lossless_contracts(
        "리바로 원료의약품 API 단가",
        _plan("리바로").model_copy(
            update={
                "resolved_question": "리바로 원료의약품 API 단가",
                "answer_sources": ("web",),
            }
        ),
    )
    result = SourceResult(
        source="web",
        query=plan.resolved_question,
        status="ok",
        payload={"related_market_sales": 123},
    )
    commentary = "리바로의 관련 브랜드 매출은 확인됩니다."

    shadow = _runtime_answer(
        monkeypatch,
        plan=plan,
        results=(result,),
        commentary=commentary,
        lossless_mode="shadow",
        request_satisfaction_mode="shadow",
    )
    inject = _runtime_answer(
        monkeypatch,
        plan=plan,
        results=(result,),
        commentary=commentary,
        lossless_mode="shadow",
        request_satisfaction_mode="inject",
    )

    assert shadow.text.startswith(commentary)
    assert "요청하신 API 단가" not in shadow.text
    assert inject.text.startswith(
        "요청하신 API 단가는 현재 연결된 원천에서 확인되지 않았습니다."
    )
    assert inject.text.endswith(shadow.text)
    assert inject.trace["lossless_spine"]["request_notice_injected"] is True


def test_runtime_fallback_retains_full_clinical_facts(monkeypatch) -> None:
    record = normalize_clinical_study(
        _study("NCT00000001"),
        matched_queries=("pitavastatin",),
    )
    plan = _plan("pitavastatin").model_copy(
        update={"resolved_question": "리바로젯 임상현황"}
    )

    answer = _runtime_answer(
        monkeypatch,
        plan=plan,
        results=(_clinical_source_result("pitavastatin", [record], total=1),),
        commentary="이번 조회에서 확인된 근거가 없어 구체적인 답을 구성하지 못했습니다.",
        synthesis_trace={"status": "fallback", "fallback_reason": "timeout"},
    )

    assert "NCT00000001" in answer.text
    assert "Pitavastatin" in answer.text
    assert "자동 해설 생성이 완료되지 않았습니다" in answer.text
    assert "구체적인 답을 구성하지 못했습니다" not in answer.text
    assert answer.trace["lossless_spine"]["fallback_detail_retention_rate"] == 1.0


def test_runtime_fallback_retains_full_patent_facts(monkeypatch) -> None:
    result = SourceResult(
        source="patent",
        query="리바로젯 특허현황",
        status="ok",
        payload={
            "patent_lanes": build_patent_lane_payload(
                kr_calls=(
                    {
                        "tool": "mfds_patent",
                        "source": "식품의약품안전처",
                        "render_data": {
                            "items": [
                                {
                                    "DOMESTIC_PATENT_NO": "10-0777553",
                                    "INVENTION_TITLE": "지질 저하 복합제",
                                    "DOMESTIC_PATENT_STATUS": "말소",
                                    "DOMESTIC_END_DATE": "2030-11-12",
                                }
                            ]
                        },
                    },
                ),
                us_calls=(),
                news_calls=(),
            )
        },
    )
    plan = _plan("patent").model_copy(
        update={
            "resolved_question": "리바로젯 특허현황",
            "answer_sources": ("patent",),
        }
    )

    answer = _runtime_answer(
        monkeypatch,
        plan=plan,
        results=(result,),
        commentary="이번 조회에서 확인된 근거가 없어 구체적인 답을 구성하지 못했습니다.",
        synthesis_trace={"status": "fallback", "fallback_reason": "timeout"},
    )

    assert "10-0777553" in answer.text
    assert "지질 저하 복합제" in answer.text
    assert "2030-11-12" in answer.text
    assert "구체적인 답을 구성하지 못했습니다" not in answer.text
    assert answer.trace["lossless_spine"]["fallback_detail_retention_rate"] == 1.0


def test_runtime_fallback_retains_full_hira_policy(monkeypatch) -> None:
    raw_text = """고시 제2026-101호
시행일 2026-08-01
투여대상 황반변성 환자
제외기준 치료 효과가 없는 경우
투여방법 및 횟수 4주 간격으로 초기 3회"""
    result = SourceResult(
        source="hira",
        query="아일리아 급여기준",
        status="ok",
        payload={
            "calls": [
                {
                    "tool": "hira_reimbursement_detail",
                    "source": "hira_reimbursement",
                    "status": "ok",
                    "render_data": {
                        "notice_number": "고시 제2026-101호",
                        "source_date": "2026-08-01",
                        "raw_text": raw_text,
                    },
                }
            ]
        },
    )
    plan = _plan("hira").model_copy(
        update={
            "resolved_question": "아일리아 급여기준",
            "answer_sources": ("hira",),
        }
    )

    answer = _runtime_answer(
        monkeypatch,
        plan=plan,
        results=(result,),
        commentary="이번 조회에서 확인된 근거가 없어 구체적인 답을 구성하지 못했습니다.",
        synthesis_trace={"status": "fallback", "fallback_reason": "timeout"},
    )

    assert "고시 제2026-101호" in answer.text
    assert "황반변성 환자" in answer.text
    assert "치료 효과가 없는 경우" in answer.text
    assert "4주 간격으로 초기 3회" in answer.text
    assert raw_text in answer.text
    assert "구체적인 답을 구성하지 못했습니다" not in answer.text
    assert answer.trace["lossless_spine"]["fallback_detail_retention_rate"] == 1.0


def test_runtime_trace_and_answer_keep_all_external_source_references(
    monkeypatch,
) -> None:
    expected_urls = tuple(
        f"https://clinicaltrials.gov/study/NCT{index:08d}"
        for index in range(1, 8)
    )
    now = datetime.now(UTC)
    result = _clinical_source_result(
        "pitavastatin",
        [normalize_clinical_study(_study("NCT00000001"))],
        total=1,
    ).model_copy(
        update={
            "citations": tuple(
                Citation(
                    source="ClinicalTrials.gov",
                    query="pitavastatin",
                    url=url,
                    retrieved_at=now,
                )
                for url in expected_urls
            )
        }
    )
    plan = _plan("pitavastatin").model_copy(
        update={"resolved_question": "리바로젯 임상현황"}
    )

    answer = _runtime_answer(
        monkeypatch,
        plan=plan,
        results=(result,),
        commentary="임상 포트폴리오 해설입니다.",
    )
    trace_urls = {
        ref["url"]
        for evidence_set in answer.trace["lossless_spine"]["evidence_sets"]
        for ref in evidence_set["source_refs"]
    }

    assert set(expected_urls) <= trace_urls
    assert all(url in answer.text for url in expected_urls)


def test_as_of_date_is_dynamic_in_planner_synthesizer_and_mart_periods(
    monkeypatch,
) -> None:
    observed_on = date(2026, 8, 12)
    plan = _plan("리바로 최근 3년 매출").model_copy(
        update={
            "resolved_question": "리바로 2021년~2023년 매출",
            "expanded_intents": ("리바로 2021년~2023년 매출 추이",),
            "tool_queries": ToolQueries(
                mart=("리바로 2021년~2023년 매출",),
                nedrug=("리바로 2021년~2023년 매출",),
                hira=("리바로 2021년~2023년 매출",),
                openfda=("리바로 2021년~2023년 매출",),
                clinicaltrials=("리바로 2021년~2023년 매출",),
                web=("리바로 2021년~2023년 매출",),
                patent=("리바로 2021년~2023년 매출",),
            ),
            "linking_plan": "리바로 2021년~2023년 조회",
        }
    )

    class Layer:
        def __init__(self) -> None:
            self.metric_calls: list[tuple[str, int]] = []

        def market_scope(self, _brand: str) -> dict[str, Any]:
            return {
                "source": "UBIST",
                "render_data": {"market_id": "ml_livalo"},
            }

        def brand_metric(
            self,
            _brand: str,
            metric: str,
            period: str,
            *,
            market: str | None,
            history_points: int,
        ) -> dict[str, Any]:
            assert market == "ml_livalo"
            self.metric_calls.append((period, history_points))
            return {
                "source": "UBIST",
                "metric": metric,
                "render_data": {
                    "series": [
                        {"period": f"{year}-12", "value": year}
                        for year in range(2021, 2027)
                    ]
                },
            }

        def top_brands(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            raise LookupError

        def cause_card_data(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {}

    anchored = _anchor_relative_years("리바로 최근 3년 매출", plan, observed_on)
    planner_messages = _planner_messages(
        "리바로 최근 3년 매출",
        (),
        observed_on=observed_on,
    )
    synth_messages = _synthesis_messages(
        anchored,
        (),
        (),
        observed_on=observed_on,
    )
    monkeypatch.setattr(v4_adapters, "current_kst_date", lambda: observed_on)
    layer = Layer()
    mart_calls = v4_adapters._strategic_mart_calls(
        layer,
        "리바로",
        anchored.tool_queries.mart[0],
    )
    sales_call = next(call for call in mart_calls if call.get("metric") == "sales")

    assert "2023년~2026년" in anchored.resolved_question
    assert all(
        "2023년~2026년" in query
        for queries in anchored.tool_queries.model_dump().values()
        for query in queries
    )
    assert "오늘은 2026-08-12이다" in planner_messages[-1]["content"]
    assert "오늘은 2026-08-12이다" in synth_messages[-1]["content"]
    assert "오늘은 2026-08-12이다" not in planner_messages[0]["content"]
    assert "오늘은 2026-08-12이다" not in synth_messages[0]["content"]
    assert "최근 3년" in anchored.tool_queries.mart[0]
    assert layer.metric_calls == [("latest", 37)] * 4
    assert [row["period"] for row in sales_call["render_data"]["series"]] == [
        "2023-12",
        "2024-12",
        "2025-12",
        "2026-12",
    ]


def test_exact_nct_detail_builds_one_evidence_record_and_single_record_render() -> None:
    plan = _plan("NCT05151731").model_copy(
        update={"resolved_question": "NCT05151731 시험 디자인"}
    )
    result = SourceResult(
        source="clinicaltrials",
        query="NCT05151731",
        status="ok",
        payload={
            "calls": [
                {
                    "tool": "clinicaltrials_study_details",
                    "status": "live",
                    "safe_url": "https://clinicaltrials.gov/study/NCT05151731",
                    "render_data": {
                        "detail": {
                            "nct_id": "NCT05151731",
                            "title": "A randomized pitavastatin trial",
                            "official_title": "Official randomized pitavastatin trial",
                            "status": "COMPLETED",
                            "phase": "PHASE3",
                            "enrollment": 120,
                            "interventions": ["Pitavastatin"],
                            "start_date": "2022-01-01",
                            "primary_completion_date": "2024-01-01",
                            "url": "https://clinicaltrials.gov/study/NCT05151731",
                        }
                    },
                }
            ]
        },
    )

    evidence_set = build_evidence_sets(
        plan,
        (result,),
        observed_on=date(2026, 8, 12),
    )[0]
    rendered = render_deterministic_facts(
        plan,
        (evidence_set,),
        observed_on=date(2026, 8, 12),
    )

    assert evidence_set.coverage.records_received == 1
    assert evidence_set.coverage.records_unique == 1
    assert evidence_set.records[0].payload["nct_id"] == "NCT05151731"
    assert rendered.profile == "single_record_detail"
    assert "NCT05151731" in rendered.text
    assert "PHASE3" in rendered.text
    assert "Pitavastatin" in rendered.text


def test_runtime_does_not_swallow_lossless_structural_invariant(monkeypatch) -> None:
    invariant_type = getattr(lossless_contracts, "LosslessInvariantError", ValueError)

    def fail_render(*_args: Any, **_kwargs: Any):
        raise invariant_type("records_rendered cannot exceed records_received")

    monkeypatch.setattr(v4_runtime, "build_lossless_render", fail_render)

    with pytest.raises(invariant_type):
        _runtime_answer(
            monkeypatch,
            plan=_plan("q"),
            results=(),
            commentary="legacy commentary",
        )


def test_requested_fields_has_independent_shadow_and_inject_contract(monkeypatch) -> None:
    configured = getattr(lossless_spine, "configured_requested_fields_mode", None)
    assert callable(configured)
    monkeypatch.setenv("CHAT_V4_REQUESTED_FIELDS_MODE", "inject")
    assert configured() == "inject"

    rendered = lossless_contracts.DeterministicRender(
        profile="clinical_portfolio",
        text="## 사실\nNCT05151731\n\n## 요청 필드 보강\n- countries: 원천 미제공",
        nodes=(
            lossless_contracts.RenderNode(
                block_id="clinical:records",
                record_ids=("ct:NCT05151731",),
                surface_fields=("nct_id",),
                text="## 사실\nNCT05151731",
            ),
            lossless_contracts.RenderNode(
                block_id="requested-fields:absence",
                record_ids=("ct:NCT05151731",),
                surface_fields=("countries",),
                text="## 요청 필드 보강\n- countries: 원천 미제공",
            ),
        ),
        coverage=lossless_contracts.CoverageLedger(
            records_received=1,
            records_unique=1,
            records_rendered=1,
        ),
    )

    shadow = compose_lossless_answer(
        rendered,
        "해설",
        synthesis_trace={"status": "synthesized"},
        mode="inject",
        requested_fields_mode="shadow",
    )
    inject = compose_lossless_answer(
        rendered,
        "해설",
        synthesis_trace={"status": "synthesized"},
        mode="inject",
        requested_fields_mode="inject",
    )

    assert "NCT05151731" in shadow.text
    assert "countries: 원천 미제공" not in shadow.text
    assert "countries: 원천 미제공" in inject.text
    assert shadow.trace["requested_fields_mode"] == "shadow"
    assert inject.trace["requested_fields_mode"] == "inject"


def test_clinical_static_query_boundary_rejects_more_than_thirty_two() -> None:
    queries = tuple(f"query-{index}" for index in range(33))

    with pytest.raises(ValidationError):
        _plan(*queries)
