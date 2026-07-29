from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.tool_use.registry import ExternalToolRegistry
from jw_chat_agent_poc.tool_use.reimbursement_evidence import reimbursement_envelope
from jw_chat_agent_poc.tools.external import ExternalApiClient, ExternalCall
from jw_chat_agent_poc.tools.external.client import (
    _clinicaltrials_detail_payload,
    _clinicaltrials_mcp_payload,
)
from jw_chat_agent_poc.tools.external.hira_reimbursement import (
    CacheLookupStatus,
    CacheStatus,
    ReimbursementCacheResult,
    ReimbursementCriterion,
    ReimbursementLookupService,
)


class _IndexMissStore:
    def get_reimbursement_criteria(self, _brand_name: str) -> ReimbursementCacheResult:
        return ReimbursementCacheResult(
            CacheStatus.NOT_FOUND,
            None,
            None,
            lookup_status=CacheLookupStatus.BRAND_UNMATCHED,
            schema_name="reimbursement_stage",
        )

    def put_reimbursement_criteria(self, _criterion: ReimbursementCriterion) -> bool:
        return False


class _Realtime:
    def __init__(self, result: ReimbursementCriterion | None) -> None:
        self.result = result
        self.calls: list[str] = []

    def fetch(self, brand_name: str) -> ReimbursementCriterion | None:
        self.calls.append(brand_name)
        return self.result


class _ClinicalDetailClient(ExternalApiClient):
    def __init__(self, detail: dict[str, Any]) -> None:
        super().__init__(mode="fixture")
        self._detail = detail

    def clinicaltrials_study_details(self, nct_id: str) -> ExternalCall:
        return ExternalCall(
            tool="clinicaltrials_study_details",
            source="clinicaltrials_mcp",
            status="live",
            summary_text=f"ClinicalTrials.gov에서 {nct_id} 상세 원문을 확인했습니다.",
            render_data={"detail": self._detail},
            safe_url=f"https://clinicaltrials.gov/study/{nct_id}",
        )


def _criterion() -> ReimbursementCriterion:
    return ReimbursementCriterion(
        brand_name="아일리아",
        title="아일리아 급여기준",
        raw_text="아일리아의 급여기준 원문",
        source_date="2026-07-30",
        collected_at=datetime(2026, 7, 30, tzinfo=UTC),
        notice_number="notice-1",
        source_url="https://www.hira.or.kr/rc/example.do",
    )


def _clinical_envelope(question: str, detail: dict[str, Any]):
    registry = ExternalToolRegistry(
        resolver=BrandResolver(),
        external=_ClinicalDetailClient(detail),
    )
    spec = next(
        item
        for item in registry.list_for_query(question)
        if item.name == "clinicaltrials_study_details"
    )
    return spec.execute(spec.input_model.model_validate({"nct_id": "NCT05151731"}))


def test_fb03_brand_index_miss_attempts_bounded_realtime_lookup() -> None:
    realtime = _Realtime(_criterion())

    result = ReimbursementLookupService(
        store=_IndexMissStore(),
        realtime=realtime,
    ).lookup("아일리아")

    assert result.ok is True
    assert result.retrieval == "realtime"
    assert result.cache_lookup_status is CacheLookupStatus.BRAND_UNMATCHED
    assert realtime.calls == ["아일리아"]


def test_fb03_policy_can_skip_realtime_after_known_upstream_failure() -> None:
    realtime = _Realtime(_criterion())

    result = ReimbursementLookupService(
        store=_IndexMissStore(),
        realtime=realtime,
        realtime_allowed=lambda: False,
    ).lookup("아일리아")

    assert result.ok is False
    assert result.error_code == "INDEX_MISS"
    assert realtime.calls == []


def test_fb03_realtime_miss_reports_index_and_official_lookup_separately() -> None:
    realtime = _Realtime(None)

    result = ReimbursementLookupService(
        store=_IndexMissStore(),
        realtime=realtime,
    ).lookup("아일리아")
    envelope = reimbursement_envelope(result, subject="아일리아")

    assert result.error_code == "REALTIME_NO_EVIDENCE"
    assert realtime.calls == ["아일리아"]
    assert "내부 급여기준 색인" in envelope.preview
    assert "실시간 공식 조회" in envelope.preview


def test_fb03_policy_skip_does_not_claim_official_source_absence() -> None:
    result = ReimbursementLookupService(
        store=_IndexMissStore(),
        realtime=_Realtime(_criterion()),
        realtime_allowed=lambda: False,
    ).lookup("아일리아")
    envelope = reimbursement_envelope(result, subject="아일리아")

    assert result.error_code == "INDEX_MISS"
    assert "내부 급여기준 색인" in envelope.preview
    assert "공식 원천에 없다는 뜻은 아닙니다" in envelope.preview


def test_fb09_raw_detail_preserves_design_fields() -> None:
    detail = _clinicaltrials_detail_payload(
        "\n".join(
            (
                "nctId: NCT05151731",
                "officialTitle: Randomized, Double Masked, Active Comparator-Controlled",
                "allocation: RANDOMIZED",
                "masking: DOUBLE",
                "interventionModel: PARALLEL",
            )
        )
    )

    assert detail["title"] == "Randomized, Double Masked, Active Comparator-Controlled"
    assert detail["allocation"] == "RANDOMIZED"
    assert detail["masking"] == "DOUBLE"
    assert detail["intervention_model"] == "PARALLEL"


def test_fb09_bounded_search_projection_preserves_design_module() -> None:
    payload = _clinicaltrials_mcp_payload(
        "\n".join(
            (
                "- nctId: NCT05151731",
                "officialTitle: Randomized, Double Masked, Active Comparator-Controlled",
                "allocation: RANDOMIZED",
                "masking: DOUBLE",
                "interventionModel: PARALLEL",
            )
        )
    )

    study = payload["studies"][0]
    identification = study["protocolSection"]["identificationModule"]
    design = study["protocolSection"]["designModule"]
    assert identification["officialTitle"] == (
        "Randomized, Double Masked, Active Comparator-Controlled"
    )
    assert design == {
        "phases": [],
        "studyType": None,
        "allocation": "RANDOMIZED",
        "masking": "DOUBLE",
        "interventionModel": "PARALLEL",
    }


def test_fb09_design_fields_are_public_only_for_design_question() -> None:
    detail = {
        "nct_id": "NCT05151731",
        "title": "Randomized, Double Masked, Active Comparator-Controlled",
        "status": "COMPLETED",
        "allocation": "RANDOMIZED",
        "masking": "DOUBLE",
        "intervention_model": "PARALLEL",
    }

    design = _clinical_envelope("NCT05151731 시험 디자인 알려줘", detail)
    generic = _clinical_envelope("NCT05151731 상태 알려줘", detail)

    design_metrics = {fact.metric for fact in design.evidence}
    generic_metrics = {fact.metric for fact in generic.evidence}
    assert {"배정 방식", "눈가림", "중재 모형"} <= design_metrics
    assert {"배정 방식", "눈가림", "중재 모형"}.isdisjoint(generic_metrics)
