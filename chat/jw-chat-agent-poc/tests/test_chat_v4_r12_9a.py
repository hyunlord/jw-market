from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

from jw_chat_agent_poc.service.v4 import adapters as v4_adapters
from jw_chat_agent_poc.service.v4 import synthesizer as v4_synthesizer
from jw_chat_agent_poc.service.v4.clinical import compile_clinical_query
from jw_chat_agent_poc.service.v4.clinical_query_policy import (
    prepare_resolved_clinical_requests,
)
from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, SourceResult, ToolQueries
from jw_chat_agent_poc.service.v4.deterministic_render import render_deterministic_facts
from jw_chat_agent_poc.service.v4 import evidence_sets as v4_evidence_sets
from jw_chat_agent_poc.service.v4.inspection import build_inspection_detail
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    CoverageLedger,
    EvidenceRecord,
    EvidenceSet,
)
from jw_chat_agent_poc.service.v4.narrative_values import narrative_field_value
from jw_chat_agent_poc.service.v4.render_clinical import _direct_relevance_counts
from jw_chat_agent_poc.tools.external.clinicaltrials_v2 import ClinicalTrialsV2Client


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


def _resolution() -> SimpleNamespace:
    return SimpleNamespace(
        canonical_brand="리바로젯",
        molecule_en=("ezetimibe", "pitavastatin"),
    )


def _study(
    nct_id: str,
    *,
    title: str,
    interventions: tuple[str, ...],
    sponsor: str,
) -> dict[str, Any]:
    return {
        "protocolSection": {
            "identificationModule": {
                "nctId": nct_id,
                "briefTitle": title,
                "officialTitle": title,
            },
            "statusModule": {"overallStatus": "RECRUITING"},
            "designModule": {
                "studyType": "INTERVENTIONAL",
                "phases": ["PHASE3"],
            },
            "armsInterventionsModule": {
                "interventions": [
                    {"type": "DRUG", "name": intervention, "otherNames": []}
                    for intervention in interventions
                ]
            },
            "sponsorCollaboratorsModule": {
                "leadSponsor": {"name": sponsor}
            },
        }
    }


def _plan() -> PlannerOutput:
    question = "리바로젯 제네릭 임상현황"
    return PlannerOutput(
        resolved_question=question,
        expanded_intents=(question,),
        tool_queries=ToolQueries(
            mart=(question,),
            nedrug=(question,),
            hira=(question,),
            openfda=(question,),
            clinicaltrials=("ezetimibe AND pitavastatin",),
            web=(question,),
            patent=(question,),
        ),
        linking_plan="deterministic",
    )


def test_r129a_field_restatement_is_not_user_visible_but_record_stays_inspectable() -> None:
    record = EvidenceRecord(
        evidence_id="ct:NCT05705804",
        source="clinicaltrials",
        result_kind="structured_clinical_record",
        payload={
            "nct_id": "NCT05705804",
            "brief_title": "Pitavastatin and Ezetimibe Trial",
            "official_title": "Pitavastatin and Ezetimibe Trial",
            "study_type": "INTERVENTIONAL",
            "overall_status": "RECRUITING",
            "interventions": [
                {"name": "Pitavastatin", "type": "DRUG", "other_names": []}
            ],
            "primary_outcomes": [{"measure": "Change from baseline"}],
            "sponsor": "Sponsor A",
        },
    )
    evidence = EvidenceSet(
        source="clinicaltrials",
        retrieved_at="2026-08-15T00:00:00Z",
        coverage=CoverageLedger(
            total_reported=1,
            records_received=1,
            records_unique=1,
            records_relevant=1,
        ),
        records=(record,),
    )
    rendered = render_deterministic_facts(
        _plan(),
        (evidence,),
        observed_on=date(2026, 8, 15),
    )
    result = SourceResult(
        source="clinicaltrials",
        query="ezetimibe AND pitavastatin",
        status="ok",
        payload={"studies": [record.payload]},
    )
    detail = build_inspection_detail(
        _plan(),
        (result,),
        (evidence,),
        rendered,
        answer_text=rendered.text,
    )

    assert "narrative:field-restatement" not in {
        node.block_id for node in rendered.nodes
    }
    for forbidden in (
        "공식 시험명",
        "평가변수 평가변수",
        "다른 명칭 []",
        "구분 DRUG",
        "INTERVENTIONAL",
    ):
        assert forbidden not in rendered.text
    output_record = detail["calls"][0]["output"]["records"][0]
    assert "NCT05705804" in output_record["identifiers"]
    assert output_record["title"] == "Pitavastatin and Ezetimibe Trial"
    assert output_record["interventions"] == ["Pitavastatin"]
    assert output_record["sponsor"] == "Sponsor A"


def test_r129a_ct_relevance_is_display_status_not_record_deletion() -> None:
    response = _Response(
        {
            "totalCount": 3,
            "studies": [
                _study(
                    "NCT00000001",
                    title="Pitavastatin Ezetimibe Bioequivalence",
                    interventions=("Pitavastatin", "Ezetimibe"),
                    sponsor="Sponsor A",
                ),
                _study(
                    "NCT00000002",
                    title="K-924 Phase III",
                    interventions=("K-924",),
                    sponsor="Sponsor B",
                ),
                _study(
                    "NCT00000003",
                    title="Cardiovascular Study",
                    interventions=("Other drug",),
                    sponsor="Sponsor C",
                ),
            ],
        }
    )
    concept = prepare_resolved_clinical_requests(
        (("리바로젯", _resolution()),),
        (),
        scope_query="리바로젯 제네릭 임상현황",
    )[0][1]
    result = ClinicalTrialsV2Client(
        get=lambda *_args, **_kwargs: response,
        timeout_s=5,
    ).search(compile_clinical_query(concept))

    assert result.records_received == 3
    assert result.records_unique == 3
    assert len(result.records) == 3
    assert result.records_relevant == 3
    assert result.records_direct_relevance_confirmed == 1
    assert result.records_direct_relevance_unconfirmed == 2
    assert result.query_manifest["records_excluded_by_relevance"] == 0
    assert result.query_manifest["records_direct_relevance_unconfirmed"] == 2
    assert [record["relevance_status"] for record in result.records] == [
        "직접 관련 확인",
        "직접 관련 여부 미확인",
        "직접 관련 여부 미확인",
    ]
    assert "missing_required_ingredient_token" not in str(result.records)


def test_r129a_duplicate_ct_query_id_does_not_double_coverage_counts() -> None:
    record = {
        "nct_id": "NCT00000001",
        "brief_title": "Pitavastatin Ezetimibe Bioequivalence",
        "relevance_status": "직접 관련 여부 미확인",
    }
    manifest = {
        "query_id": "ctq:stable",
        "records_received": 23,
        "records_unique": 23,
        "records_relevant": 23,
        "records_direct_relevance_confirmed": 14,
        "records_direct_relevance_unconfirmed": 9,
        "records_excluded_by_relevance": 0,
    }
    call = {
        "tool": "clinicaltrials_v2_lossless_search",
        "status": "live",
        "render_data": {
            "payload": {"studies": [record]},
            "query_manifest": manifest,
            "coverage": {
                "total_reported": 23,
                "records_received": 23,
                "records_unique": 23,
                "records_relevant": 23,
                "records_excluded_by_relevance": 0,
                "pagination_complete": True,
            },
        },
    }
    result = SourceResult(
        source="clinicaltrials",
        query="리바로젯 제네릭 임상현황",
        status="ok",
        payload={"calls": [call]},
    )
    clinical = v4_evidence_sets._clinical_set((result, result), date(2026, 8, 15))

    assert clinical.coverage.total_reported == 23
    assert clinical.coverage.records_received == 23
    assert len(clinical.query_manifest) == 1
    assert clinical.query_manifest[0]["records_direct_relevance_confirmed"] == 14
    assert clinical.query_manifest[0]["records_direct_relevance_unconfirmed"] == 9


def test_r129a_direct_relevance_counts_use_unique_rendered_records() -> None:
    records = (
        EvidenceRecord(
            evidence_id="ct:NCT00000001",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={"relevance_status": "직접 관련 확인"},
        ),
        EvidenceRecord(
            evidence_id="ct:NCT00000002",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={"relevance_status": "직접 관련 여부 미확인"},
        ),
    )
    evidence = EvidenceSet(
        source="clinicaltrials",
        query_spec=("all", "korea subset"),
        retrieved_at="2026-08-15T00:00:00+09:00",
        coverage=CoverageLedger(records_received=3, records_unique=2),
        records=records,
        query_manifest=(
            {
                "records_direct_relevance_confirmed": 1,
                "records_direct_relevance_unconfirmed": 1,
            },
            {
                "records_direct_relevance_confirmed": 1,
                "records_direct_relevance_unconfirmed": 0,
            },
        ),
    )

    assert _direct_relevance_counts(evidence) == (1, 1)


def test_r129a_nested_empty_values_and_known_enums_are_publicly_sanitized() -> None:
    record = EvidenceRecord(
        evidence_id="ct:NCT00000004",
        source="clinicaltrials",
        result_kind="structured_clinical_record",
        payload={
            "interventions": [
                {"name": "Pitavastatin", "type": "DRUG", "other_names": []}
            ],
            "study_type": "INTERVENTIONAL",
        },
    )

    interventions = narrative_field_value(record, "interventions")
    assert interventions is not None
    assert "다른 명칭" not in interventions
    assert "[]" not in interventions
    assert "DRUG" not in interventions
    assert "의약품" in interventions
    assert narrative_field_value(record, "study_type") == "중재 연구"


def test_r129a_mart_period_is_clamped_to_injected_latest_available_period() -> None:
    class QueryLayer:
        def __init__(self) -> None:
            self.metric_periods: list[str] = []

        def market_scope(self, brand: str) -> dict[str, Any]:
            return {
                "source": "UBIST",
                "tool": "market_scope",
                "render_data": {
                    "market_id": "ml_livalo",
                    "anchor_brand": brand,
                    "period": "2026-06",
                },
            }

        def brand_metric(
            self,
            brand: str,
            metric: str,
            period: str,
            *,
            market: str | None = None,
            history_points: int = 10,
        ) -> dict[str, Any]:
            self.metric_periods.append(period)
            if period != "latest":
                return {"source": "UBIST", "tool": metric, "status": "no_data"}
            return {
                "source": "UBIST",
                "tool": metric,
                "render_data": {
                    "brand": brand,
                    "period": "2026-06",
                    "market_id": market,
                    "history_points": history_points,
                    "brand_value_series_10pt": [
                        {"period": "2025-06", "value": 100.0},
                        {"period": "2026-06", "value": 110.0},
                    ],
                },
            }

        def top_brands(
            self,
            brand: str,
            *,
            limit: int,
            metric: str,
            market: str | None = None,
        ) -> dict[str, Any]:
            return {
                "source": "UBIST",
                "tool": "top_brands",
                "render_data": {
                    "level_top5_trend_series": [
                        {"brand": brand, "company": "JW", "rank": 1}
                    ]
                },
            }

        def market_member_metric(
            self,
            anchor_brand: str,
            member_brand: str,
            *,
            market: str | None = None,
            metric: str,
        ) -> dict[str, Any]:
            return {
                "source": "UBIST",
                "tool": "market_member_metric",
                "render_data": {
                    "brand": member_brand,
                    "market_id": market,
                    "metric": metric,
                    "brand_value_series_10pt": [
                        {"period": "2025-06", "value": 100.0},
                        {"period": "2026-06", "value": 110.0},
                    ],
                },
            }

        def cause_card_data(self, brand: str, market: str | None) -> dict[str, Any]:
            return {"brand": brand, "market": market, "ei_ms": {"value": 1.0}}

    layer = QueryLayer()
    calls = v4_adapters._strategic_mart_calls(
        layer,
        "리바로",
        "리바로 매출 알려줘",
        period_from="2021-01-01",
        period_to="2026-08-15",
    )

    assert layer.metric_periods == ["latest"] * 4
    assert len(calls) == 8
    bundle = next(call for call in calls if call.get("tool") == "entity_bundle")
    assert bundle["entity_bundle"]["requested_period"] == "latest"
    assert all(call.get("status") != "no_data" for call in calls)
    assert v4_adapters._mart_period_clamp_notice_for_calls(
        "2026-08-15",
        calls,
    ) == (
        "요청한 종료 기간 2026-08-15은 데이터마트 최신 가용 기간 "
        "2026-06을 넘어, 최신 가용 기간 기준으로 조정했습니다."
    )


def test_r129a_mart_period_clamp_is_coverage_and_surface_notice() -> None:
    notice = (
        "요청한 종료 기간 2026-08-15은 데이터마트 최신 가용 기간 "
        "2026-06을 넘어, 최신 가용 기간 기준으로 조정했습니다."
    )
    result = SourceResult(
        source="mart",
        query="리바로 매출 알려줘",
        status="ok",
        payload={"calls": [{"tool": "market_scope"}]},
        notice=notice,
    )

    assert v4_synthesizer._coverage_notices((result,)) == (notice,)
    answer = v4_synthesizer._finalize_answer("매출 답변입니다.", (result,))
    assert answer.count(notice) == 1
