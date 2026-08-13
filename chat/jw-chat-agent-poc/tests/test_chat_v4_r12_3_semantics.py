from __future__ import annotations

from datetime import date
from hashlib import sha256

from jw_chat_agent_poc.service.v4 import semantic_realization
from jw_chat_agent_poc.service.v4.claim_ir import classify_answer_claims
from jw_chat_agent_poc.service.v4.contracts import (
    PlannerOutput,
    RequestedAnswerShape,
    SourceResult,
    ToolQueries,
)
from jw_chat_agent_poc.service.v4.deterministic_render import render_deterministic_facts
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    CoverageLedger,
    EvidenceRecord,
    EvidenceSet,
)
from jw_chat_agent_poc.service.v4.retrieval_events import (
    public_retrieval_notice,
    retrieval_event_from_result,
)
from jw_chat_agent_poc.service.v4.semantic_realization import downgrade_predicate
from jw_chat_agent_poc.service.v4.semantic_realization import SemanticEvidenceContext


def _evidence() -> EvidenceSet:
    return EvidenceSet(
        source="clinicaltrials",
        retrieved_at="2026-08-13T00:00:00Z",
        coverage=CoverageLedger(records_received=1, records_unique=1),
        records=(
            EvidenceRecord(
                evidence_id="ct:NCT00000001",
                source="clinicaltrials",
                result_kind="structured_clinical_record",
                payload={
                    "nct_id": "NCT00000001",
                    "overall_status": "COMPLETED",
                    "sponsor": "Alpha Pharma",
                    "start_date": "2023-01-01",
                    "enrollment": 100,
                },
            ),
        ),
    )


def test_c_semantic_downgrade_changes_predicate_and_never_hedges() -> None:
    temporal = downgrade_predicate(
        "CAUSES",
        SemanticEvidenceContext(
            has_temporal_support=True,
            supported_text="매출 증가 처방 증가",
            temporal_support_texts=(
                "2025-01-01 매출 증가",
                "2026-01-01 처방 증가",
            ),
            observed_count=2,
            requested_count=2,
        ),
    )
    unsupported = downgrade_predicate(
        "TREND_PREDICTION",
        SemanticEvidenceContext(
            has_temporal_support=False,
            supported_text="",
            observed_count=2,
            requested_count=2,
        ),
    )

    assert temporal.action == "retain"
    assert temporal.predicate_id == "TEMPORALLY_ASSOCIATED"
    assert temporal.predicate_id != temporal.original_predicate_id
    assert temporal.causal_level == "ASSOCIATION"
    assert unsupported.action == "delete"
    assert unsupported.predicate_id is None


def test_c_posthoc_claim_classifier_never_emits_causal_level() -> None:
    classified = classify_answer_claims(
        "NCT00000001이 시장 성장을 일으켰습니다.", (_evidence(),)
    )
    assert classified.claim_ir[0].causal_level != "CAUSAL"


def test_c_surface_realization_transforms_predicate_instead_of_hedging() -> None:
    answer = (
        "매출 증가가 처방 증가를 일으켰습니다.\n"
        "모든 브랜드를 완전하게 비교했습니다.\n"
        "내년에도 점유율이 증가할 것으로 전망됩니다."
    )

    realized = semantic_realization.realize_semantic_surface(
        answer,
        SemanticEvidenceContext(
            has_temporal_support=True,
            supported_text="매출 증가 처방 증가",
            temporal_support_texts=(
                "2025-01-01 매출 증가",
                "2026-01-01 처방 증가",
            ),
            observed_count=2,
            requested_count=3,
        ),
    )

    assert "일으켰" not in realized.text
    assert "보입니다" not in realized.text
    assert "[관찰적 연결]" in realized.text
    assert "시간상 함께 관찰되었습니다" in realized.text
    assert "확인된 일부 브랜드" in realized.text
    assert "전망" not in realized.text
    assert realized.downgrade_count == 2
    assert realized.deletion_count == 1


def test_c_surface_realization_prunes_heading_emptied_by_semantic_deletion() -> None:
    answer = (
        "## 핵심 답\n확인된 사실입니다.\n\n"
        "## 종합 인사이트\n내년에도 점유율이 증가할 것으로 전망됩니다.\n\n"
        "## 미확인 요소\n- 조회 범위 밖 정보는 확인하지 못했습니다."
    )

    realized = semantic_realization.realize_semantic_surface(
        answer,
        SemanticEvidenceContext(
            has_temporal_support=False,
            supported_text="확인된 사실",
            observed_count=1,
            requested_count=1,
        ),
    )

    assert "## 종합 인사이트" not in realized.text
    assert "## 미확인 요소" in realized.text
    assert "조회 범위 밖 정보" in realized.text
    assert realized.deletion_count == 1


def test_c_unbound_causal_terms_are_deleted_even_with_temporal_records() -> None:
    realized = semantic_realization.realize_semantic_surface(
        "알파 물질이 베타 처방을 일으켰습니다.",
        SemanticEvidenceContext(
            has_temporal_support=True,
            supported_text="감마 물질 델타 처방",
            observed_count=2,
            requested_count=2,
        ),
    )

    assert realized.text == ""
    assert realized.downgrade_count == 0
    assert realized.deletion_count == 1


def test_c_grounded_field_restatement_is_not_deleted_for_causal_field_text() -> None:
    grounded = (
        "- ClinicalTrials.gov의 NCT06722521은(는) 2차 평가변수 all-cause "
        "mortality로 확인됩니다. [출처: ClinicalTrials.gov]"
    )
    unsupported = "매출 증가가 처방 확대를 일으켰습니다."

    realized = semantic_realization.realize_semantic_surface(
        f"{grounded}\n{unsupported}",
        SemanticEvidenceContext(
            has_temporal_support=False,
            supported_text=grounded,
            observed_count=1,
            requested_count=1,
            protected_line_sha256=(
                sha256(grounded.encode("utf-8")).hexdigest(),
            ),
        ),
    )

    assert grounded in realized.text
    assert unsupported not in realized.text
    assert realized.deletion_count == 1


def test_c_alternate_causal_forms_cannot_bypass_semantic_deletion() -> None:
    context = SemanticEvidenceContext(
        has_temporal_support=True,
        supported_text="감마 물질 델타 처방",
        temporal_support_texts=(
            "2025-01-01 감마 물질",
            "2026-01-01 델타 처방",
        ),
        observed_count=2,
        requested_count=2,
    )

    for sentence in (
        "시장 감소는 경쟁 심화 때문입니다.",
        "뉴스가 매출에 영향을 줬습니다.",
        "매출 감소의 원인은 경쟁 심화입니다.",
        "환자수 감소가 원인입니다.",
        "경쟁 심화가 원인으로 확인되었습니다.",
        "경쟁 심화가 원인일 가능성이 있습니다.",
        "경쟁 심화는 원인 중 하나입니다.",
        "매출 감소는 경쟁 심화에 기인한 것으로 관측됩니다.",
        "경쟁 심화는 원인 중 하나일 가능성이 있습니다.",
        "경쟁 심화는 원인 중 하나일 수 있습니다.",
        "매출 감소는 경쟁 심화에 기인할 가능성이 있습니다.",
        "매출 감소는 경쟁 심화에 기인할 수 있습니다.",
        "A causation signal was observed.",
        "A causality signal was observed.",
    ):
        realized = semantic_realization.realize_semantic_surface(sentence, context)
        assert realized.text == ""
        assert realized.deletion_count == 1


def test_c_causal_gate_covers_headings_and_preserves_bound_table_rows() -> None:
    answer = (
        "## 매출 감소는 경쟁 심화 때문입니다.\n"
        "| 주장 | 근거 |\n"
        "| --- | --- |\n"
        "| 매출 감소는 경쟁 심화 때문입니다. | record-1 |"
    )

    realized = semantic_realization.realize_semantic_surface(
        answer,
        SemanticEvidenceContext(
            has_temporal_support=False,
            supported_text="record-1",
            observed_count=1,
            requested_count=1,
        ),
    )

    assert "경쟁 심화 때문" not in realized.text
    assert "record-1" in realized.text
    assert realized.text.count("|") == answer.count("|")
    assert "[확인 한계] 인과 관계는 이 조회로 확정하지 않습니다." in realized.text
    assert realized.deletion_count == 2


def test_c_temporal_downgrade_requires_clause_bound_temporal_records() -> None:
    answer = "매출 증가가 처방 증가를 일으켰습니다."
    realized = semantic_realization.realize_semantic_surface(
        answer,
        SemanticEvidenceContext(
            has_temporal_support=True,
            supported_text="매출 증가 처방 증가 2025-01-01 2026-01-01",
            temporal_support_texts=(
                "2025-01-01 unrelated alpha",
                "2026-01-01 unrelated beta",
            ),
            observed_count=2,
            requested_count=2,
        ),
    )

    assert realized.text == ""
    assert realized.downgrade_count == 0
    assert realized.deletion_count == 1


def test_c_observed_expected_enrollment_is_not_misread_as_trend_prediction() -> None:
    answer = "예상 등록 인원은 100명입니다."
    realized = semantic_realization.realize_semantic_surface(
        answer,
        SemanticEvidenceContext(
            has_temporal_support=False,
            supported_text=answer,
            observed_count=1,
            requested_count=1,
        ),
    )

    assert realized.text == answer
    assert realized.deletion_count == 0


def test_d_scope_limit_is_distinct_from_absence_and_bound_to_f_scope() -> None:
    result = SourceResult(
        source="nedrug",
        query="pitavastatin 품목",
        status="scope_limit",
        notice="성분명으로는 품목 검색이 지원되지 않아 이 항목은 확인하지 못했습니다",
    )

    event = retrieval_event_from_result(result)
    notice = public_retrieval_notice(event)

    assert event.status == "scope_limit"
    assert event.exposure_layer == "F-scope"
    assert "성분명으로는 품목 검색이 지원되지 않아" in notice
    assert "0건" not in notice


def test_e_scope_limit_is_rendered_in_confirmation_limits_for_market_profile() -> None:
    evidence = EvidenceSet(
        source="nedrug",
        retrieved_at="2026-08-13T00:00:00Z",
        coverage=CoverageLedger(),
        item_failures=(
            {
                "source": "nedrug",
                "query": "pitavastatin 성분 품목",
                "status": "scope_limit",
                "notice": (
                    "성분명으로는 품목 검색이 지원되지 않아 "
                    "이 항목은 확인하지 못했습니다"
                ),
            },
        ),
    )
    plan = PlannerOutput(
        resolved_question="pitavastatin 성분 품목",
        expanded_intents=("pitavastatin 성분 품목",),
        answer_sources=("nedrug",),
        tool_queries=ToolQueries(
            mart=("pitavastatin 성분 품목",),
            nedrug=("pitavastatin 성분 품목",),
            hira=("pitavastatin 성분 품목",),
            openfda=("pitavastatin 성분 품목",),
            clinicaltrials=("pitavastatin 성분 품목",),
            web=("pitavastatin 성분 품목",),
            patent=("pitavastatin 성분 품목",),
        ),
        linking_plan="first hop is sufficient",
        requested_answer_shape=RequestedAnswerShape(),
    )

    rendered = render_deterministic_facts(
        plan, (evidence,), observed_on=date(2026, 8, 13)
    )

    assert rendered.source_notices
    assert rendered.source_notice_bindings[0]["exposure_layer"] == "F-scope"
    assert "성분명으로는 품목 검색이 지원되지 않아" in rendered.source_notices[0]
