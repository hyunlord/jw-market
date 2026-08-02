from __future__ import annotations

from decimal import Decimal
import logging

import pytest
from pydantic import ValidationError

from jw_chat_agent_poc.contracts.answer import (
    AnswerModel,
    AnswerSection,
    ChartIntent,
    ContractFacetFailure,
    SectionKind,
    SupportedClaim,
)
from jw_chat_agent_poc.contracts.evidence import (
    EvidenceBundle,
    EvidenceFact,
    EvidenceStatus,
    RationaleFact,
    RationaleKind,
)
from jw_chat_agent_poc.contracts.query import (
    MarketAxisSpec,
    MarketSource,
    MeasureKind,
    MeasureSpec,
    NativeMarketMeasure,
    PeriodSpec,
    PortalMarketView,
    UnitSpec,
)
from jw_chat_agent_poc.contracts.shadow import evidence_bundle_from_legacy_facts
from jw_chat_agent_poc.contracts.validation import (
    RenderAuthorization,
    ValidationDecision,
    ValidationReport,
)
from jw_chat_agent_poc.orchestrator.provenance import EvidenceFact as LegacyEvidenceFact
from jw_chat_agent_poc.service import app as service_app


def _axis() -> MarketAxisSpec:
    return MarketAxisSpec(
        market_id="strategy_006",
        market_definition="고지혈증",
        view=PortalMarketView.MARKET_LANDSCAPE,
        source=MarketSource.UBIST,
        native_measure=NativeMarketMeasure.SALES,
        measure=MeasureSpec(kind=MeasureKind.ABSOLUTE, name="sales"),
        period=PeriodSpec(start="2026-05", end="2026-05", granularity="month"),
        unit=UnitSpec(code="KRW", label="원"),
        catalog_snapshot_id="phase0a-20260802",
        market_definition_version="phase0a-v1",
    )


def _fact(**changes: object) -> EvidenceFact:
    values = {
        "evidence_id": "sales:리바로:2026-05",
        "subject_type": "brand",
        "subject_id": "리바로",
        "subject_name": "리바로",
        "metric": "sales",
        "value": Decimal("8039000000"),
        "unit": "KRW",
        "period_from": "2026-05",
        "period_to": "2026-05",
        "source": "UBIST",
        "view": "market_landscape",
        "market_id": "strategy_006",
        "axis_id": _axis().axis_id,
        "provenance": {"type": "direct", "locator": "fixture"},
        "status": EvidenceStatus.FOUND,
    }
    values.update(changes)
    return EvidenceFact(**values)


def test_rationale_not_found_rejects_claimed_content() -> None:
    with pytest.raises(ValidationError, match="NOT_FOUND"):
        RationaleFact(
            evidence_id="rationale:가드렛:class",
            subject_type="brand",
            subject_id="가드렛",
            subject_name="가드렛",
            metric="market_membership_rationale",
            value="class recode 사유를 찾았습니다",
            source="catalog",
            provenance={"type": "catalog_rationale"},
            status=EvidenceStatus.NOT_FOUND,
            rationale_kind=RationaleKind.CLASS_RECODE,
        )


def test_rationale_not_found_accepts_an_explicit_empty_value() -> None:
    fact = RationaleFact(
        evidence_id="rationale:가드렛:class",
        subject_type="brand",
        subject_id="가드렛",
        subject_name="가드렛",
        metric="market_membership_rationale",
        value=None,
        source="catalog",
        provenance={"type": "catalog_rationale"},
        status=EvidenceStatus.NOT_FOUND,
        rationale_kind=RationaleKind.CLASS_RECODE,
    )

    assert fact.value is None


@pytest.mark.parametrize(
    "provenance",
    [
        {"type": "deterministic_calculation", "input_evidence_ids": ["left", "right"]},
        {"type": "deterministic_calculation", "formula": "left / right"},
    ],
)
def test_calculated_fact_requires_formula_and_inputs(provenance: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="deterministic_calculation"):
        _fact(evidence_id="share:리바로", metric="share", provenance=provenance)


def test_calculated_fact_accepts_complete_lineage() -> None:
    fact = _fact(
        evidence_id="share:리바로",
        metric="share",
        provenance={
            "type": "deterministic_calculation",
            "formula": "sales / market_sales",
            "input_evidence_ids": ["sales:리바로", "sales:market"],
        },
    )

    assert fact.provenance["formula"] == "sales / market_sales"


def test_evidence_fact_uses_market_axis_identity_without_recalculation() -> None:
    axis = _axis()
    fact = _fact(axis_id=axis.axis_id)

    assert fact.axis_id == axis.axis_id
    assert EvidenceBundle(facts=(fact,)).bundle_hash == EvidenceBundle(
        facts=(fact,)
    ).bundle_hash


def test_answer_model_represents_fb02_requested_and_unresolvable_facets() -> None:
    fact = _fact(evidence_id="clinical:stroke:001", metric="clinical_trial")
    claim = SupportedClaim(
        claim_type="bounded_result",
        subject="뇌경색",
        predicate="clinical_trials_found",
        value="1",
        evidence_ids=(fact.evidence_id,),
        qualifiers=("permission facet unresolved",),
    )
    chart = ChartIntent(
        chart_id="clinical-count",
        chart_type="bar",
        evidence_ids=(fact.evidence_id,),
    )
    answer = AnswerModel(
        title="뇌경색 임상시험 및 허가 현황",
        claims=(claim,),
        sections=(
            AnswerSection(
                section_id="clinical",
                kind=SectionKind.CHART,
                evidence_ids=(fact.evidence_id,),
                chart_intent=chart,
            ),
        ),
        notices=("확인된 임상시험만 제공합니다.",),
        limitations=("허가 facet은 현재 해소되지 않았습니다.",),
        requested_facets=("clinical", "permission"),
        unresolvable_facets=(
            ContractFacetFailure(
                facet="permission",
                reason_code="CAPABILITY_NOT_IMPLEMENTED",
                message="허가 조회 capability가 없습니다.",
            ),
        ),
    )

    assert answer.requested_facets == ("clinical", "permission")
    assert answer.unresolvable_facets[0].facet == "permission"
    assert answer.sections[0].chart_intent is not None


def test_render_authorization_references_exact_bundle_hash() -> None:
    bundle = EvidenceBundle(facts=(_fact(),))
    report = ValidationReport(decision=ValidationDecision.ALLOW)
    authorization = RenderAuthorization(
        passed=True,
        authorized_chart_ids=("sales-trend",),
        evidence_bundle_hash=bundle.bundle_hash,
    )

    assert report.violations == ()
    assert authorization.evidence_bundle_hash == bundle.bundle_hash


def test_legacy_projection_is_shadow_only_and_preserves_fact_identity() -> None:
    legacy = LegacyEvidenceFact(
        fact_id="fact:1",
        label="리바로 매출",
        value="80.39억원",
        source="UBIST",
        tool="market_query",
        path="$.rows[0]",
        period="2026-05",
        allowed_numbers=("80.39",),
        entity="리바로",
        metric="sales",
        unit="억원",
        view="market_landscape",
        market_id="strategy_006",
    )

    bundle = evidence_bundle_from_legacy_facts((legacy,))

    assert bundle.facts[0].evidence_id == "fact:1"
    assert bundle.facts[0].axis_id is None
    assert bundle.facts[0].provenance["type"] == "legacy_projection"


def test_evidence_shadow_failure_leaves_answer_bytes_unchanged(monkeypatch, caplog) -> None:
    result = {"tool_calls": [], "markdown_response": {"evidence": []}}
    baseline = service_app._apply_evidence_binding_gate(
        "리바로 매출 알려줘",
        "확인 가능한 매출 근거가 없습니다.",
        result,
    )

    def _fail_shadow(_facts: object) -> EvidenceBundle:
        raise RuntimeError("synthetic phase1b shadow failure")

    monkeypatch.setattr(service_app, "evidence_bundle_shadow_observation", _fail_shadow)
    with caplog.at_level(logging.ERROR, logger="jw_chat_agent_poc.service.app"):
        actual = service_app._apply_evidence_binding_gate(
            "리바로 매출 알려줘",
            "확인 가능한 매출 근거가 없습니다.",
            result,
        )

    assert actual.encode("utf-8") == baseline.encode("utf-8")
    assert any(
        "evidence_bundle_shadow_observation_failed" in item.message
        for item in caplog.records
    )
