from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
from types import SimpleNamespace
from typing import Any

from jw_chat_agent_poc.service.v4.clinical import compile_clinical_query
from jw_chat_agent_poc.service.v4.clinical_query_policy import (
    DEFAULT_ACTIVE_CLINICAL_STATUSES,
    prepare_resolved_clinical_requests,
)
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    CoverageLedger,
    EvidenceRecord,
    EvidenceSet,
)
from jw_chat_agent_poc.service.v4.patent import build_patent_lane_payload
from jw_chat_agent_poc.service.v4.reason_code_enforcement import enforce_reason_codes
from jw_chat_agent_poc.service.v4.render_clinical import render_clinical
from jw_chat_agent_poc.service.v4.render_patent import render_patent
from jw_chat_agent_poc.service.v4.contracts import SourceResult
from jw_chat_agent_poc.tools.external.client import _mcp_render_data, _mcp_tool_spec
from jw_chat_agent_poc.tools.external.clinicaltrials_v2 import ClinicalTrialsV2Client


def _resolution(
    brand: str = "리바로젯",
    molecules: tuple[str, ...] = ("ezetimibe", "pitavastatin"),
) -> SimpleNamespace:
    return SimpleNamespace(canonical_brand=brand, molecule_en=molecules)


def _prepared_parameters(question: str, resolution: SimpleNamespace) -> dict[str, Any]:
    prepared = prepare_resolved_clinical_requests(
        ((resolution.canonical_brand, resolution),),
        (),
        scope_query=question,
    )
    assert len(prepared) == 1
    return compile_clinical_query(prepared[0][1]).parameters


def test_a_combo_uses_and_without_an_implicit_status_filter_deterministically() -> None:
    question = "리바로젯 제네릭 임상현황"

    first = _prepared_parameters(question, _resolution())
    second = _prepared_parameters(question, _resolution())

    assert first == second
    assert first["query.intr"] == "ezetimibe AND pitavastatin"
    assert "filter.overallStatus" not in first


def test_a_single_ingredient_keeps_a_single_intervention_term_without_status_filter() -> None:
    parameters = _prepared_parameters(
        "리바로 임상현황",
        _resolution("리바로", ("pitavastatin",)),
    )

    assert parameters["query.intr"] == "pitavastatin"
    assert "filter.overallStatus" not in parameters


def test_a_explicit_historical_scope_replaces_default_active_statuses() -> None:
    parameters = _prepared_parameters("리바로젯 과거 임상현황", _resolution())

    assert parameters["filter.overallStatus"] == "COMPLETED|TERMINATED|WITHDRAWN"


@dataclass
class _Response:
    payload: dict[str, Any]

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


def _study(
    nct_id: str,
    *,
    interventions: tuple[str, ...],
    title: str,
    status: str = "RECRUITING",
    last_update: str = "2026-08-13",
) -> dict[str, Any]:
    return {
        "protocolSection": {
            "identificationModule": {
                "nctId": nct_id,
                "briefTitle": title,
                "officialTitle": f"Official {title}",
            },
            "statusModule": {
                "overallStatus": status,
                "lastUpdatePostDateStruct": {"date": last_update},
            },
            "designModule": {"phases": ["PHASE3"]},
            "conditionsModule": {"conditions": ["Dyslipidemia"]},
            "armsInterventionsModule": {
                "interventions": [
                    {"name": intervention} for intervention in interventions
                ]
            },
            "sponsorCollaboratorsModule": {
                "leadSponsor": {"name": "Sponsor"}
            },
        }
    }


def test_b_client_audits_status_filter_and_records_relevance_exclusions() -> None:
    seen: list[dict[str, Any]] = []
    responses = iter(
        (
            {
                "totalCount": 3,
                "studies": [
                    _study(
                        "NCT00000001",
                        interventions=("Pitavastatin", "Ezetimibe"),
                        title="Pitavastatin Ezetimibe Bioequivalence",
                    ),
                    _study(
                        "NCT06686615",
                        interventions=("Ezetimibe",),
                        title="Ezetimibe Study",
                    ),
                    _study(
                        "NCT07036991",
                        interventions=("PCSK9 inhibitor", "statin"),
                        title="Carotid Cohort",
                    ),
                ],
            },
        )
    )

    def get(_url: str, *, params: dict[str, Any], timeout: float) -> _Response:
        seen.append({"params": dict(params), "timeout": timeout})
        return _Response(next(responses))

    concept = prepare_resolved_clinical_requests(
        (("리바로젯", _resolution()),),
        (),
        scope_query="리바로젯 제네릭 임상현황",
    )[0][1]
    result = ClinicalTrialsV2Client(get=get, timeout_s=5).search(
        compile_clinical_query(concept)
    )

    assert "filter.overallStatus" not in seen[0]["params"]
    assert seen[0]["params"]["pageSize"] == 100
    assert result.total_unfiltered == 3
    assert result.total_reported == 3
    assert result.records_received == 3
    assert result.records_unique == 3
    assert result.records_relevant == 1
    assert [record["nct_id"] for record in result.records] == ["NCT00000001"]
    assert result.relevance_exclusions == (
        {
            "nct_id": "NCT06686615",
            "reason_code": "missing_required_ingredient_token",
        },
        {
            "nct_id": "NCT07036991",
            "reason_code": "missing_required_ingredient_token",
        },
    )
    assert "title" not in str(result.relevance_exclusions).casefold()
    assert "url" not in str(result.relevance_exclusions).casefold()


def test_e_patent_adapter_uses_one_exact_kr_brand_query_for_combo_product(
    monkeypatch,
) -> None:
    from jw_chat_agent_poc.agent_loop import factory
    from jw_chat_agent_poc.service import general_view_routing
    from jw_chat_agent_poc.service.v4 import adapters as v4_adapters
    from jw_chat_agent_poc.tools.external.client import ExternalCall

    kr_calls: list[tuple[str, str | None]] = []

    def call(tool: str, source: str) -> ExternalCall:
        return ExternalCall(
            tool=tool,
            source=source,
            status="live",
            summary_text=f"{tool} result",
            render_data={"items": []},
        )

    class Resolver:
        def resolve(self, _query: str, *, allow_default: bool) -> SimpleNamespace:
            assert allow_default is False
            return _resolution()

    class External:
        timeout_s = 12

        def mfds_patent(
            self,
            ingredient: str,
            *,
            item_name: str | None = None,
        ) -> ExternalCall:
            kr_calls.append((ingredient, item_name))
            return call("mfds_patent", "식품의약품안전처")

        def mfds_fda_orangebook(self, _ingredient: str) -> ExternalCall:
            return call("mfds_fda_orangebook", "FDA Orange Book")

        def web_search(self, _query: str, *, topic: str = "general") -> ExternalCall:
            assert topic == "news"
            return call("web_search", "Tavily")

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

    v4_adapters.build_source_adapters()["patent"]("리바로젯 특허현황")

    assert kr_calls == [("ezetimibe", "리바로젯")]


def _record(
    index: int,
    *,
    title: str,
    status: str = "RECRUITING",
    last_update: str = "2026-08-13",
    sponsor: str = "Sponsor",
) -> EvidenceRecord:
    nct_id = f"NCT{index:08d}"
    return EvidenceRecord(
        evidence_id=f"ct:{nct_id}",
        source="clinicaltrials",
        result_kind="structured_clinical_record",
        payload={
            "nct_id": nct_id,
            "brief_title": title,
            "overall_status": status,
            "phases": ["PHASE3"],
            "sponsor": sponsor,
            "last_update_date": last_update,
            "interventions": ["Pitavastatin", "Ezetimibe"],
            "url": f"https://clinicaltrials.gov/study/{nct_id}",
        },
    )


def test_c_portfolio_renders_one_deterministic_table_capped_at_ten() -> None:
    records = tuple(
        _record(
            index,
            title=(
                "Bioequivalence of generic Pitavastatin Ezetimibe"
                if index == 11
                else f"Pitavastatin Ezetimibe Study {index}"
            ),
            status="ACTIVE_NOT_RECRUITING" if index == 11 else "RECRUITING",
            last_update=f"2026-08-{index:02d}",
        )
        for index in range(1, 12)
    )
    evidence = EvidenceSet(
        source="clinicaltrials",
        query_spec=("리바로젯 제네릭 임상현황",),
        retrieved_at="2026-08-13T00:00:00Z",
        coverage=CoverageLedger(
            total_reported=583,
            records_after_status_filter=11,
            records_received=11,
            records_unique=11,
            records_relevant=11,
            records_excluded_by_status=572,
            records_excluded_by_relevance=0,
        ),
        records=records,
    )

    nodes, _required = render_clinical(evidence, single=False)
    record_node = next(node for node in nodes if node.block_id == "clinical:records")

    assert [node.block_id for node in nodes] == [
        "clinical:coverage",
        "clinical:records",
        "clinical:record-details",
    ]
    assert len(record_node.record_ids) == 11
    assert record_node.record_ids[0] == "ct:NCT00000011"
    assert "NCT00000011" in record_node.text
    assert "외 1건" not in record_node.text
    assert "제네릭·생동성 관련 시험 우선" not in record_node.text
    assert "원천 검색 583건" in nodes[0].text
    assert "활성 상태 기준 11건" in nodes[0].text
    assert "상세 표시 11건" in nodes[0].text
    assert "### NCT" not in record_node.text
    assert record_node.text.count("\n|") == 13


def test_c_single_record_detail_is_not_truncated() -> None:
    record = _record(5151731, title="NCT05151731 Trial Design")
    evidence = EvidenceSet(
        source="clinicaltrials",
        query_spec=("NCT05151731 시험 디자인",),
        retrieved_at="2026-08-13T00:00:00Z",
        coverage=CoverageLedger(
            total_reported=1,
            records_after_status_filter=1,
            records_received=1,
            records_unique=1,
            records_relevant=1,
        ),
        records=(record,),
    )

    nodes, _required = render_clinical(evidence, single=True)
    record_node = next(node for node in nodes if node.block_id == "clinical:records")

    assert record_node.record_ids == (record.evidence_id,)
    assert "단일 임상시험 상세" in record_node.text
    assert "외 " not in record_node.text
    assert "NCT05151731" in record_node.text


def test_c_all_missing_columns_are_omitted() -> None:
    record = _record(1, title="Pitavastatin Trial", sponsor="")
    payload = {**record.payload, "phases": []}
    evidence = EvidenceSet(
        source="clinicaltrials",
        query_spec=("pitavastatin",),
        retrieved_at="2026-08-13T00:00:00Z",
        coverage=CoverageLedger(
            total_reported=1,
            records_after_status_filter=1,
            records_received=1,
            records_unique=1,
            records_relevant=1,
        ),
        records=(record.model_copy(update={"payload": payload}),),
    )

    nodes, _required = render_clinical(evidence, single=False)
    record_text = next(node.text for node in nodes if node.block_id == "clinical:records")

    assert "단계" not in record_text
    assert "스폰서" not in record_text


def _patent_item(
    patent_no: str | None,
    *,
    status: str,
    expiration: str | None,
    product: str = "리바로젯정2/10밀리그램",
    patent_type: str = "용도",
    page_group: str = "제품특허",
) -> dict[str, Any]:
    return {
        "PAGE_GB_NM": page_group,
        "ITEM_NAME": product,
        "INGR_ENG_NAME": "Pitavastatin Calcium Hydrate/Ezetimibe",
        "DOMESTIC_PATENT_NO": patent_no,
        "DOMESTIC_INVN_NM": f"발명 {patent_no or '번호없음'}",
        "PATENT_GB_CODE": patent_type,
        "DOMESTIC_PATENT_STATUS": status,
        "DOMESTIC_END_DATE": expiration,
        "PATENTEE": "권리자",
    }


def _patent_call(items: list[dict[str, Any]], *, limit: int = 500) -> dict[str, Any]:
    return {
        "source": "nedrug_mcp",
        "tool": "mfds_patent",
        "safe_url": "http://code-serving-250:8080/json",
        "render_data": {
            "request": {"item_name": "리바로젯", "limit": str(limit)},
            "items": items,
        },
    }


def test_e_patent_limit_is_env_driven_and_mcp_payload_is_not_resliced(
    monkeypatch: Any,
) -> None:
    monkeypatch.delenv("MFDS_PATENT_MAX_RESULTS", raising=False)
    default_spec = _mcp_tool_spec(
        "mfds_patent",
        {"item_name": "리바로젯", "limit": "500"},
    )
    assert default_spec["arguments"]["limit"] == 500

    monkeypatch.setenv("MFDS_PATENT_MAX_RESULTS", "123")
    env_spec = _mcp_tool_spec("mfds_patent", {"item_name": "리바로젯"})
    assert env_spec["arguments"]["limit"] == 123

    payload = [_patent_item(f"10-{index:07d}", status="등록", expiration="2030-01-01") for index in range(500)]
    render_data = _mcp_render_data(
        payload,
        {"item_name": "리바로젯", "limit": "500"},
        "search_korea_drug_patent",
        "raw",
    )
    assert len(render_data["items"]) == 500
    assert render_data["request_limit"] == 500
    assert render_data["source_limit_reached"] is True
    assert "content_text" not in render_data["mcp"]
    assert render_data["mcp"]["content_length"] == 3
    assert render_data["mcp"]["content_sha256"] == hashlib.sha256(b"raw").hexdigest()


def test_e_patent_lane_deduplicates_by_patent_number_and_records_cap() -> None:
    items = [
        _patent_item("10-0000001", status="소멸(존속기간만료)", expiration="2024-01-01"),
        _patent_item(
            "10-0000001",
            status="등록",
            expiration="2035-01-01",
            product="리바로젯정4/10밀리그램",
        ),
        _patent_item("10-0000002", status="등록", expiration="2032-01-01"),
        _patent_item(None, status="심사중", expiration=None),
        _patent_item(
            "10-9999999",
            status="출원",
            expiration=None,
            page_group="기타특허",
        ),
    ]
    lanes = build_patent_lane_payload(
        kr_calls=(_patent_call(items, limit=5),),
        us_calls=(),
        news_calls=(),
    )
    kr = lanes["kr_primary"]

    assert kr["records_received"] == 5
    assert kr["product_patent_rows"] == 4
    assert kr["non_product_exclusions"] == 1
    assert kr["records_unique"] == 2
    assert [record["patent_no"] for record in kr["records"]] == [
        "10-0000001",
        "10-0000002",
    ]
    assert kr["records"][0]["status"] == "등록"
    assert kr["records"][0]["patent_type"] == "용도"
    assert kr["source_limit"] == 5
    assert kr["source_limit_reached"] is True
    assert kr["identifier_exclusions"] == 1


def test_e_product_patent_filter_keeps_exact_four_canonical_patents() -> None:
    canonical = (
        ("10-0186853", "물질물질(염)용도기타", "소멸(존속기간만료)"),
        ("10-0596257", "조성용도", "소멸(존속기간만료)"),
        ("10-1244508", "용도", "소멸(무효)"),
        ("10-1198822", "제법", "소멸(등록료불납)"),
    )
    rows: list[dict[str, Any]] = []
    for patent_no, patent_type, status in canonical:
        rows.extend(
            (
                _patent_item(
                    patent_no,
                    status=status,
                    expiration="2021-05-06",
                    patent_type=patent_type,
                ),
                _patent_item(
                    patent_no,
                    status=("소멸" if patent_no == "10-1244508" else status),
                    expiration="2021-05-06",
                    product="리바로젯정4/10밀리그램",
                    patent_type=patent_type,
                ),
            )
        )
    rows.append(
        _patent_item(
            "10-0101149",
            status="출원",
            expiration=None,
            page_group="기타특허",
        )
    )

    kr = build_patent_lane_payload(
        kr_calls=(_patent_call(rows, limit=500),),
        us_calls=(),
        news_calls=(),
    )["kr_primary"]

    assert kr["records_received"] == 9
    assert kr["product_patent_rows"] == 8
    assert kr["non_product_exclusions"] == 1
    assert [record["patent_no"] for record in kr["records"]] == [
        "10-0186853",
        "10-0596257",
        "10-1244508",
        "10-1198822",
    ]
    assert all(record["page_group"] == "제품특허" for record in kr["records"])
    invalidated = next(
        record for record in kr["records"] if record["patent_no"] == "10-1244508"
    )
    assert invalidated["status"] == "소멸(무효)"
    assert invalidated["status_variants"] == ["소멸(무효)", "소멸"]


def test_e_limit_expansion_and_product_filter_are_both_required() -> None:
    first_page = [
        _patent_item(
            "10-0101149",
            status="소멸(존속기간만료)",
            expiration="2013-02-01",
            page_group="기타특허",
        ),
        _patent_item(
            "10-0186853",
            status="소멸(존속기간만료)",
            expiration="2016-04-29",
        ),
        _patent_item(
            "10-0348842",
            status="소멸(존속기간만료)",
            expiration="2018-01-01",
            page_group="기타특허",
        ),
        _patent_item(
            "10-0830018",
            status="소멸(존속기간만료)",
            expiration="2019-01-01",
            page_group="기타특허",
        ),
        _patent_item(
            "10-0777553",
            status="소멸(존속기간만료)",
            expiration="2020-01-01",
            page_group="기타특허",
        ),
    ]
    remaining_product_patents = [
        _patent_item(
            "10-0596257",
            status="소멸(존속기간만료)",
            expiration="2022-01-25",
            patent_type="조성용도",
        ),
        _patent_item(
            "10-1244508",
            status="소멸(무효)",
            expiration="2021-05-06",
            patent_type="용도",
        ),
        _patent_item(
            "10-1198822",
            status="소멸(등록료불납)",
            expiration="2023-11-01",
            patent_type="제법",
        ),
    ]

    truncated = build_patent_lane_payload(
        kr_calls=(_patent_call(first_page, limit=5),),
        us_calls=(),
        news_calls=(),
    )["kr_primary"]
    expanded = build_patent_lane_payload(
        kr_calls=(_patent_call([*first_page, *remaining_product_patents], limit=500),),
        us_calls=(),
        news_calls=(),
    )["kr_primary"]

    assert truncated["records_received"] == 5
    assert truncated["product_patent_rows"] == 1
    assert [record["patent_no"] for record in truncated["records"]] == [
        "10-0186853"
    ]
    assert expanded["records_received"] == 8
    assert expanded["product_patent_rows"] == 4
    assert expanded["non_product_exclusions"] == 4
    assert [record["patent_no"] for record in expanded["records"]] == [
        "10-0186853",
        "10-0596257",
        "10-1244508",
        "10-1198822",
    ]


def test_e_patent_render_prioritizes_registered_and_caps_domestic_table() -> None:
    records = tuple(
        EvidenceRecord(
            evidence_id=f"patent:kr:{index}",
            source="patent",
            result_kind="structured_patent_record",
            payload={
                "lane": "kr_primary",
                "product": "리바로젯",
                "ingredient": "Pitavastatin/Ezetimibe",
                "patent_no": f"10-{index:07d}",
                "invention_title": f"발명 {index}",
                "patent_type": "용도" if index != 12 else "",
                "status": "등록" if index in {2, 12} else "소멸(존속기간만료)",
                "listed_status": "등록" if index in {2, 12} else "소멸(존속기간만료)",
                "expiration_date": f"20{20 + index:02d}-01-01",
                "owner": "권리자",
                "jurisdiction": "KR",
                "as_of_date": "2026-08-13",
            },
        )
        for index in range(1, 13)
    )
    evidence = EvidenceSet(
        source="patent",
        query_spec=("리바로젯 특허현황",),
        query_manifest=(
            {
                "lane": "kr_primary",
                "records_received": 274,
                "product_patent_rows": 12,
                "records_unique": 12,
                "non_product_exclusions": 262,
                "source_limit": 500,
                "source_limit_reached": False,
                "identifier_exclusions": 0,
            },
        ),
        retrieved_at="2026-08-13T00:00:00Z",
        coverage=CoverageLedger(
            total_reported=274,
            records_received=274,
            records_unique=12,
        ),
        records=records,
    )

    nodes, _required = render_patent(evidence, date(2026, 8, 13))
    coverage = next(node.text for node in nodes if node.block_id == "patent:coverage")
    kr_node = next(node for node in nodes if node.block_id == "patent:kr-primary")

    assert len(kr_node.record_ids) == 12
    assert kr_node.record_ids[:2] == ("patent:kr:12", "patent:kr:2")
    assert "특허구분" in kr_node.text
    assert "등록 상태 2건" in kr_node.text
    assert "외 2건" not in kr_node.text
    assert "등록 우선" in kr_node.text
    assert (
        "국내 정본: 원천 수신 274건 → 제품특허 12건 → "
        "고유 특허번호 12건 → 상세 표시 12건"
    ) in coverage
    assert "기타특허 262건은 등재특허가 아니어서 정본 표에서 제외" in coverage
    assert "모두 소멸" not in "\n".join(node.text for node in nodes)
    assert "등재목록상 소멸일" in kr_node.text
    assert "목록상 존속기간만료일" not in kr_node.text


def test_e_patent_cap_reached_is_disclosed_as_partial() -> None:
    evidence = EvidenceSet(
        source="patent",
        query_spec=("리바로 특허현황",),
        query_manifest=(
            {
                "lane": "kr_primary",
                "records_received": 500,
                "product_patent_rows": 1,
                "records_unique": 1,
                "non_product_exclusions": 499,
                "source_limit": 500,
                "source_limit_reached": True,
                "identifier_exclusions": 0,
            },
        ),
        retrieved_at="2026-08-13T00:00:00Z",
        coverage=CoverageLedger(
            total_reported=500,
            records_received=500,
            records_unique=1,
            pagination_complete=False,
            partial_reasons=("국내 특허 조회가 상류 호출 상한 500건에 도달",),
        ),
        records=(
            EvidenceRecord(
                evidence_id="patent:kr:1",
                source="patent",
                result_kind="structured_patent_record",
                payload={
                    "lane": "kr_primary",
                    "patent_no": "10-0000001",
                    "status": "등록",
                    "listed_status": "등록",
                    "expiration_date": "2030-01-01",
                    "patent_type": "용도",
                    "jurisdiction": "KR",
                    "as_of_date": "2026-08-13",
                },
            ),
        ),
    )

    nodes, _required = render_patent(evidence, date(2026, 8, 13))
    surface = "\n".join(node.text for node in nodes)

    assert "상류 호출 상한 500건에 도달" in surface
    assert "전체 현황으로 단정할 수 없습니다" in surface
    assert "무효로 소멸한 특허의 등재목록상 소멸일은 원 존속기간과 다를 수 있습니다" in surface


def test_e_expiry_forecast_question_gets_scope_limit_not_domestic_expiry_claim() -> None:
    evidence = EvidenceSet(
        source="patent",
        query_spec=("리바로젯 특허 만료 예정일",),
        retrieved_at="2026-08-13T00:00:00Z",
        coverage=CoverageLedger(records_received=0, records_unique=0),
        records=(),
    )

    nodes, _required = render_patent(evidence, date(2026, 8, 13))
    surface = "\n".join(node.text for node in nodes)

    assert "식약처 등재목록 API만으로 특허 만료 예정일을 확인할 수 없습니다" in surface


def test_e_global_patent_expiry_claim_is_scoped_and_registered_status_wins() -> None:
    result = SourceResult(
        source="patent",
        query="리바로젯 특허현황",
        status="ok",
        payload={
            "patent_lanes": {
                "kr_primary": {
                    "records_received": 2,
                    "records_unique": 2,
                    "records": [
                        {"patent_no": "10-1", "status": "소멸(존속기간만료)"},
                        {"patent_no": "10-2", "status": "등록"},
                    ],
                }
            }
        },
    )

    repaired, trace = enforce_reason_codes(
        "공식 특허목록상 리바로젯에 등재된 특허들은 이미 소멸 상태로 확인됩니다.",
        (result,),
    )

    assert "모두 소멸" not in repaired
    assert "등록 상태 등재특허 1건" in repaired
    assert trace["PATENT_STATUS_OVERCLAIM"] == 1


def test_e_patent_status_overclaim_is_removed_even_with_another_repair_candidate() -> None:
    result = SourceResult(
        source="patent",
        query="리바로젯 특허현황",
        status="ok",
        payload={
            "patent_lanes": {
                "kr_primary": {
                    "records": [
                        {
                            "patent_no": "10-2",
                            "status": "등록",
                            "patent_expiry": "2024-06-30",
                        }
                    ]
                }
            }
        },
    )

    repaired, trace = enforce_reason_codes(
        "등재 특허들은 모두 소멸 상태입니다. 해당 특허는 2024년 만료를 앞두고 있습니다.",
        (result,),
    )

    assert trace["review_only"] is True
    assert trace["PATENT_STATUS_OVERCLAIM"] == 1
    assert trace["AS_OF_DATE"] == 1
    assert "모두 소멸" not in repaired
    assert "등록 상태 등재특허 1건" in repaired
