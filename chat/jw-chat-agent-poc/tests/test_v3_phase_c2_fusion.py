from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from jw_chat_agent_poc.tool_use.v3_execution_contracts import (
    ClinicalTrialFact,
    MarketMetricFact,
    RegulatoryRuleFact,
    ToolFailureRecord,
    V3EvidenceBundle,
    V3EvidenceFact,
)
from jw_chat_agent_poc.tool_use.v3_fusion import (
    FusionOutputTruncatedError,
    V3FusionEngine,
    build_fusion_messages,
    validate_fusion_answer,
)
from jw_chat_agent_poc.tool_use.v3_fusion_contracts import (
    GeneratedFusionAnswer,
    GeneratedFusionClaim,
)
from jw_chat_agent_poc.tool_use.v3_fusion_provider import (
    FusionProviderResult,
    GenosV3FusionProvider,
)


def _fact(
    evidence_id: str,
    *,
    value: object,
    metric: str = "share",
    unit: str = "%",
) -> MarketMetricFact:
    return MarketMetricFact(
        evidence_id=evidence_id,
        tool_name="market.get_brand_metric",
        arguments={"brand": "아일리아", "metric": metric},
        raw_result={
            "render_data": {
                "brand": "아일리아",
                "metric": metric,
                "value": value,
                "unit_label": unit,
                "period": "2026-Q1",
                "view_type": "general_view",
                "market_id": "S01P0",
            }
        },
        missing_required_fields=(),
        entity="아일리아",
        metric=metric,
        period="2026-Q1",
        unit=unit,
        view="general_view",
        market="S01P0",
    )


def _bundle(
    *facts: V3EvidenceFact,
    failures: tuple[ToolFailureRecord, ...] = (),
) -> V3EvidenceBundle:
    return V3EvidenceBundle(
        status="partial" if facts and failures else "complete" if facts else "failed",
        facts=facts,
        failures=failures,
        deferred=(),
        executions=(),
        original_call_count=len(facts) + len(failures),
        executed_call_count=len(facts) + len(failures),
        deduplicated_call_count=0,
    )


def _clinical_list_fact() -> ClinicalTrialFact:
    return ClinicalTrialFact(
        evidence_id="v3-shadow:clinicaltrials_v2_search:203e3d6e29478d89",
        tool_name="clinicaltrials_v2_search",
        arguments={"query.condition": "cerebral infarction"},
        raw_result={
            "ok": True,
            "evidence": [
                {
                    "fact_id": "clinicaltrials_v2_search:1",
                    "metric": "글로벌 임상시험",
                    "period": None,
                    "raw_ref": "clinicaltrials_v2_search:1",
                    "source_locator": "NCT06715007 · 첫 번째 시험",
                    "source_name": "ClinicalTrials.gov 임상시험 정보",
                    "subject": "cerebral infarction",
                    "unit": None,
                    "value": None,
                },
                {
                    "fact_id": "clinicaltrials_v2_search:원천_제공_총_건수",
                    "metric": "원천 제공 총 건수",
                    "period": None,
                    "raw_ref": "clinicaltrials_v2_search:원천_제공_총_건수",
                    "source_locator": "upstream totalCount",
                    "source_name": "ClinicalTrials.gov 임상시험 정보",
                    "subject": "cerebral infarction",
                    "unit": "건",
                    "value": "1048",
                },
            ],
            "raw": {
                "render_data": {
                    "payload": {"totalCount": 1048},
                }
            },
        },
        missing_required_fields=(),
        status="RECRUITING",
    )


def _population_fact(evidence_id: str = "fact-population") -> MarketMetricFact:
    return MarketMetricFact(
        evidence_id=evidence_id,
        tool_name="market.get_market_members",
        arguments={"brand": "아일리아"},
        raw_result={
            "render_data": {
                "metric": "market_members",
                "period": "2026-Q1",
                "view_type": "general_view",
                "market_id": "S01P0",
                "member_population": tuple(f"전체{i}" for i in range(1, 11)),
                "member_population_count": 10,
                "active_members": tuple(f"활성{i}" for i in range(1, 10)),
                "active_member_count": 9,
                "active_members_period": "2026-Q1",
                "display_members": tuple(f"표시{i}" for i in range(1, 6)),
                "display_member_count": 5,
            }
        },
        missing_required_fields=(),
        entity="아일리아",
        metric="market_members",
        period="2026-Q1",
        unit="brand",
        view="general_view",
        market="S01P0",
    )


def _period_fact(
    evidence_id: str,
    *,
    periods: tuple[str, ...],
    value: object = 51.38,
    metric: str = "share",
) -> MarketMetricFact:
    return MarketMetricFact(
        evidence_id=evidence_id,
        tool_name="market.get_timeseries",
        arguments={"brand": "아일리아", "metric": metric},
        raw_result={
            "series": tuple(
                {"period": period, "value": value} for period in periods
            )
        },
        missing_required_fields=(),
        entity="아일리아",
        metric=metric,
        period=periods[-1],
        unit="%",
        view="general_view",
        market="S01P0",
    )


def _regulatory_date_fact(effective_date: str = "2019-06-07") -> RegulatoryRuleFact:
    return RegulatoryRuleFact(
        evidence_id="fact-regulatory-date",
        tool_name="hira_reimbursement_criteria",
        arguments={"query": "보험 인정기준"},
        raw_result={"effective_date": effective_date, "product_count": 5},
        missing_required_fields=(),
        effective_date=effective_date,
        last_checked="2026-08-05",
    )


def test_claim_requires_existing_evidence_and_exact_numeric_literal() -> None:
    fact = _fact("fact-share", value=51.38)
    answer = GeneratedFusionAnswer(
        claims=(
            GeneratedFusionClaim(
                text="아일리아 점유율은 51.38%입니다.",
                evidence_ids=("fact-share",),
            ),
        ),
    )

    result = validate_fusion_answer(answer, _bundle(fact))

    assert [claim.text for claim in result.answer.claims] == [
        "아일리아 점유율은 51.38%입니다."
    ]
    assert result.audit.rejected_claims == ()
    assert result.audit.ungrounded_numeric_literals == ()


def test_ungrounded_numeric_claim_is_removed_without_discarding_grounded_claim() -> None:
    fact = _fact("fact-share", value=51.38)
    answer = GeneratedFusionAnswer(
        claims=(
            GeneratedFusionClaim(
                text="아일리아 점유율은 51.38%입니다.",
                evidence_ids=("fact-share",),
            ),
            GeneratedFusionClaim(
                text="아일리아 점유율은 52%입니다.",
                evidence_ids=("fact-share",),
            ),
        ),
    )

    result = validate_fusion_answer(answer, _bundle(fact))

    assert [claim.text for claim in result.answer.claims] == [
        "아일리아 점유율은 51.38%입니다."
    ]
    assert result.audit.ungrounded_numeric_literals == ("52",)
    assert result.audit.rejected_claims[0].reason == "ungrounded_numeric_literal"
    assert result.answer.limitations


def test_missing_evidence_reference_is_rejected_at_the_claim_boundary() -> None:
    fact = _fact("fact-share", value=51.38)
    answer = GeneratedFusionAnswer(
        claims=(
            GeneratedFusionClaim(
                text="근거가 없는 문장입니다.",
                evidence_ids=(),
            ),
            GeneratedFusionClaim(
                text="존재하지 않는 근거입니다.",
                evidence_ids=("missing-fact",),
            ),
        ),
    )

    result = validate_fusion_answer(answer, _bundle(fact))

    assert result.answer.claims == ()
    assert {item.reason for item in result.audit.rejected_claims} == {
        "missing_evidence_reference",
        "unknown_evidence_reference",
    }


def test_typed_failure_is_added_to_limitations_when_model_omits_it() -> None:
    failure = ToolFailureRecord(
        tool_name="market.get_brand_metric",
        arguments={"brand": "리바로", "source": "IQVIA"},
        stage="execution",
        error_type="UnsupportedSourceError",
        message="unsupported_source: iqvia",
    )

    result = validate_fusion_answer(GeneratedFusionAnswer(), _bundle(failures=(failure,)))

    assert result.answer.claims == ()
    assert result.answer.limitations == ("그 소스에는 해당 지표가 없습니다.",)
    assert result.audit.injected_limitation_reason_codes == ("unsupported_source",)


def test_partial_success_keeps_claim_and_records_failed_facet() -> None:
    fact = _fact("fact-share", value=51.38)
    failure = ToolFailureRecord(
        tool_name="hira_reimbursement_criteria",
        arguments={"query": "아일리아"},
        stage="execution",
        error_type="UPSTREAM_ERROR",
        message="upstream unavailable",
    )
    answer = GeneratedFusionAnswer(
        claims=(
            GeneratedFusionClaim(
                text="아일리아 점유율은 51.38%입니다.",
                evidence_ids=("fact-share",),
            ),
        )
    )

    result = validate_fusion_answer(answer, _bundle(fact, failures=(failure,)))

    assert len(result.answer.claims) == 1
    assert len(result.answer.limitations) == 1


def test_general_composite_remains_fail_closed_in_fusion() -> None:
    failure = ToolFailureRecord(
        tool_name="market.get_market_size",
        arguments={"scope": {"kind": "general_composite"}},
        stage="execution",
        error_type="GeneralCompositeUnavailableError",
        message="formula parity details must not be exposed",
    )
    attempted = GeneratedFusionAnswer(
        claims=(
            GeneratedFusionClaim(
                text="복합 시장 결과가 확인됐습니다.",
                evidence_ids=("fabricated",),
            ),
        )
    )

    result = validate_fusion_answer(attempted, _bundle(failures=(failure,)))

    assert result.answer.claims == ()
    assert result.answer.limitations == ("현재 지원하지 않는 시장 조합입니다.",)
    assert "formula" not in result.answer.limitations[0]


def test_hhi_keeps_direct_mart_precision_and_rejects_truncation() -> None:
    fact = _fact("fact-hhi", value=3188.0404, metric="hhi", unit="index")
    answer = GeneratedFusionAnswer(
        claims=(
            GeneratedFusionClaim(
                text="2026-Q1 HHI는 3,188.0404입니다.",
                evidence_ids=("fact-hhi",),
            ),
            GeneratedFusionClaim(
                text="2026-Q1 HHI는 3,188.0403입니다.",
                evidence_ids=("fact-hhi",),
            ),
        )
    )

    result = validate_fusion_answer(answer, _bundle(fact))

    assert [claim.text for claim in result.answer.claims] == [
        "2026-Q1 HHI는 3,188.0404입니다."
    ]
    assert result.audit.ungrounded_numeric_literals == ("3,188.0403",)


def test_hhi_claim_without_its_evidence_period_is_rejected() -> None:
    fact = _fact("fact-hhi", value=3015.4125, metric="hhi", unit="index")
    answer = GeneratedFusionAnswer(
        claims=(
            GeneratedFusionClaim(
                text="HHI는 3,015.4125입니다.",
                evidence_ids=("fact-hhi",),
            ),
        )
    )

    result = validate_fusion_answer(answer, _bundle(fact))

    assert result.answer.claims == ()
    assert result.audit.rejected_claims[0].reason == "hhi_period_missing"


def test_hhi_claim_accepts_equivalent_korean_quarter_period() -> None:
    fact = _fact("fact-hhi", value=3188.0404, metric="hhi", unit="index")
    answer = GeneratedFusionAnswer(
        claims=(
            GeneratedFusionClaim(
                text="2026년 1분기 기준 HHI는 3,188.0404입니다.",
                evidence_ids=("fact-hhi",),
            ),
        )
    )

    result = validate_fusion_answer(answer, _bundle(fact))

    assert [claim.text for claim in result.answer.claims] == [
        "2026년 1분기 기준 HHI는 3,188.0404입니다."
    ]
    assert result.audit.rejected_claims == ()


@pytest.mark.parametrize("month_text", ("2026년 5월", "2026년 05월"))
def test_hhi_claim_accepts_equivalent_korean_month_period(month_text: str) -> None:
    fact = replace(
        _fact("fact-hhi", value=253.6207, metric="hhi", unit="index"),
        period="2026-05",
    )
    answer = GeneratedFusionAnswer(
        claims=(
            GeneratedFusionClaim(
                text=f"{month_text} 기준 HHI는 253.6207입니다.",
                evidence_ids=(fact.evidence_id,),
            ),
        )
    )

    result = validate_fusion_answer(answer, _bundle(fact))

    assert [claim.text for claim in result.answer.claims] == [
        f"{month_text} 기준 HHI는 253.6207입니다."
    ]
    assert result.audit.rejected_claims == ()


def test_period_month_digits_cannot_supply_rank_evidence() -> None:
    fact = replace(_fact("fact-share", value=51.38), period="2026-05")
    answer = GeneratedFusionAnswer(
        claims=(
            GeneratedFusionClaim(
                text="2026년 05월 기준 아일리아는 6위이고 점유율은 51.38%입니다.",
                evidence_ids=(fact.evidence_id,),
            ),
        )
    )

    result = validate_fusion_answer(answer, _bundle(fact))

    assert result.answer.claims == ()
    assert result.audit.rejected_claims[0].reason == "ungrounded_numeric_literal"
    assert result.audit.ungrounded_numeric_literals == ("6",)


@pytest.mark.parametrize(
    ("metric", "observed", "injected", "unit"),
    (
        ("market_size", 2139.25, "2140.00", "억원"),
        ("hhi", 253.6207, "253.6208", "index"),
        ("market_members", 555, "556", "brand"),
    ),
)
def test_unobserved_numeric_injections_are_rejected(
    metric: str,
    observed: float,
    injected: str,
    unit: str,
) -> None:
    fact = replace(_fact(f"fact-{metric}", value=observed, metric=metric, unit=unit), period="2026-05")
    answer = GeneratedFusionAnswer(
        claims=(
            GeneratedFusionClaim(
                text=f"2026년 05월 {metric} 값은 {injected}입니다.",
                evidence_ids=(fact.evidence_id,),
            ),
        )
    )

    result = validate_fusion_answer(answer, _bundle(fact))

    assert result.answer.claims == ()
    assert result.audit.rejected_claims[0].reason == "ungrounded_numeric_literal"
    assert injected in result.audit.ungrounded_numeric_literals


def test_allowed_numeric_value_in_wrong_population_layer_is_rejected() -> None:
    fact = _population_fact()
    answer = GeneratedFusionAnswer(
        claims=(
            GeneratedFusionClaim(
                text="이 시장의 전체 mart 관측 브랜드는 9개입니다.",
                evidence_ids=(fact.evidence_id,),
            ),
        )
    )

    result = validate_fusion_answer(answer, _bundle(fact))

    assert result.answer.claims == ()
    assert result.audit.rejected_claims[0].reason == "population_layer_mismatch"


def test_allowed_value_relabelled_to_another_korean_month_is_rejected() -> None:
    fact = replace(_fact("fact-share", value=51.38), period="2026-05")
    answer = GeneratedFusionAnswer(
        claims=(
            GeneratedFusionClaim(
                text="2026년 04월 아일리아 점유율은 51.38%입니다.",
                evidence_ids=(fact.evidence_id,),
            ),
        )
    )

    result = validate_fusion_answer(answer, _bundle(fact))

    assert result.answer.claims == ()
    assert result.audit.rejected_claims[0].reason == "ungrounded_numeric_literal"
    assert result.audit.ungrounded_numeric_literals == ("04",)


def test_market_size_and_hhi_with_different_korean_months_remain_rejected() -> None:
    market_size = replace(
        _fact("fact-size", value=2139.25, metric="market_size", unit="억원"),
        period="2026-05",
    )
    hhi = replace(
        _fact("fact-hhi", value=253.6207, metric="hhi", unit="index"),
        period="2026-04",
    )
    answer = GeneratedFusionAnswer(
        claims=(
            GeneratedFusionClaim(
                text="2026년 05월 시장 규모는 2139.25억원이고 2026년 04월 HHI는 253.6207입니다.",
                evidence_ids=(market_size.evidence_id, hhi.evidence_id),
            ),
        )
    )

    result = validate_fusion_answer(answer, _bundle(market_size, hhi))

    assert result.answer.claims == ()
    assert result.audit.rejected_claims[0].reason == "market_hhi_period_mismatch"


def test_period_quarter_digit_cannot_supply_rank_evidence() -> None:
    fact = _fact("fact-share", value=51.38)
    answer = GeneratedFusionAnswer(
        claims=(
            GeneratedFusionClaim(
                text="2026-Q1 기준 아일리아는 1위이고 점유율은 51.38%입니다.",
                evidence_ids=(fact.evidence_id,),
            ),
        )
    )

    result = validate_fusion_answer(answer, _bundle(fact))

    assert result.answer.claims == ()
    assert result.audit.rejected_claims[0].reason == "ungrounded_numeric_literal"
    assert result.audit.ungrounded_numeric_literals == ("1",)


@pytest.mark.parametrize(
    ("periods", "text"),
    (
        (("2026-05",), "2026년 기준 점유율은 51.38%입니다."),
        (("2026-Q1",), "2026년 기준 점유율은 51.38%입니다."),
    ),
)
def test_evidence_period_derives_korean_year_axis(
    periods: tuple[str, ...],
    text: str,
) -> None:
    fact = _period_fact("fact-derived-year", periods=periods)

    result = validate_fusion_answer(
        GeneratedFusionAnswer(
            claims=(
                GeneratedFusionClaim(text=text, evidence_ids=(fact.evidence_id,)),
            )
        ),
        _bundle(fact),
    )

    assert [claim.text for claim in result.answer.claims] == [text]


@pytest.mark.parametrize(
    "date_text",
    ("2019년 6월 7일", "2019년 6월", "2019년"),
)
def test_effective_date_derives_korean_date_month_and_year(date_text: str) -> None:
    fact = _regulatory_date_fact()
    text = f"{date_text} 기준 적용 대상은 5개 품목입니다."

    result = validate_fusion_answer(
        GeneratedFusionAnswer(
            claims=(
                GeneratedFusionClaim(text=text, evidence_ids=(fact.evidence_id,)),
            )
        ),
        _bundle(fact),
    )

    assert [claim.text for claim in result.answer.claims] == [text]


def test_unique_bare_month_is_derived_from_evidence_periods() -> None:
    fact = _period_fact("fact-unique-month", periods=("2023-07",))
    text = "07월 점유율은 51.38%입니다."

    result = validate_fusion_answer(
        GeneratedFusionAnswer(
            claims=(
                GeneratedFusionClaim(text=text, evidence_ids=(fact.evidence_id,)),
            )
        ),
        _bundle(fact),
    )

    assert [claim.text for claim in result.answer.claims] == [text]


def test_ambiguous_bare_month_is_rejected_with_candidate_count() -> None:
    fact = _period_fact(
        "fact-ambiguous-month",
        periods=("2023-07", "2024-07", "2025-07"),
    )

    result = validate_fusion_answer(
        GeneratedFusionAnswer(
            claims=(
                GeneratedFusionClaim(
                    text="07월 점유율은 51.38%입니다.",
                    evidence_ids=(fact.evidence_id,),
                ),
            )
        ),
        _bundle(fact),
    )

    assert result.answer.claims == ()
    assert result.audit.rejected_claims[0].reason == (
        "ambiguous_period_month_candidates_3"
    )


def test_multiple_ambiguous_bare_months_preserve_all_candidate_counts() -> None:
    fact = _period_fact(
        "fact-multiple-ambiguous-months",
        periods=("2023-07", "2024-07", "2025-07", "2023-08", "2024-08"),
    )

    result = validate_fusion_answer(
        GeneratedFusionAnswer(
            claims=(
                GeneratedFusionClaim(
                    text="07월과 08월 점유율은 51.38%입니다.",
                    evidence_ids=(fact.evidence_id,),
                ),
            )
        ),
        _bundle(fact),
    )

    assert result.answer.claims == ()
    assert result.audit.rejected_claims[0].reason == (
        "ambiguous_period_month_candidates_2_3"
    )


@pytest.mark.parametrize(
    "text",
    (
        "2019년 점유율은 51.38%입니다.",
        "2019년 6월 7일 점유율은 51.38%입니다.",
    ),
)
def test_period_not_derived_from_evidence_remains_rejected(text: str) -> None:
    fact = _period_fact("fact-other-period", periods=("2023-07", "2026-05"))

    result = validate_fusion_answer(
        GeneratedFusionAnswer(
            claims=(
                GeneratedFusionClaim(text=text, evidence_ids=(fact.evidence_id,)),
            )
        ),
        _bundle(fact),
    )

    assert result.answer.claims == ()
    assert result.audit.rejected_claims[0].reason == "ungrounded_numeric_literal"


def test_derived_year_cannot_supply_rank_evidence() -> None:
    fact = _period_fact("fact-year-rank", periods=("2023-07",))

    result = validate_fusion_answer(
        GeneratedFusionAnswer(
            claims=(
                GeneratedFusionClaim(
                    text="2023년: 아일리아 매출은 3위이고 점유율은 51.38%입니다.",
                    evidence_ids=(fact.evidence_id,),
                ),
            )
        ),
        _bundle(fact),
    )

    assert result.answer.claims == ()
    assert result.audit.ungrounded_numeric_literals == ("3",)


def test_derived_full_date_cannot_supply_item_count_evidence() -> None:
    fact = _regulatory_date_fact()

    result = validate_fusion_answer(
        GeneratedFusionAnswer(
            claims=(
                GeneratedFusionClaim(
                    text="2019년 6월 7일 기준 적용 대상은 7개 품목입니다.",
                    evidence_ids=(fact.evidence_id,),
                ),
            )
        ),
        _bundle(fact),
    )

    assert result.answer.claims == ()
    assert result.audit.ungrounded_numeric_literals == ("7",)


def test_hhi_annual_series_accepts_evidence_derived_year_labels() -> None:
    fact = _period_fact(
        "fact-hhi-series",
        periods=("2023", "2024", "2025"),
        value=262.4174,
        metric="hhi",
    )
    fact = replace(
        fact,
        raw_result={
            "render_data": {
                "hhi_recent": 262.4174,
                "hhi_period": "2025",
                "hhi_series_5y": (
                    {"period": "2023", "hhi": 281.4508},
                    {"period": "2024", "hhi": 271.1722},
                    {"period": "2025", "hhi": 262.4174},
                ),
            }
        },
    )
    text = (
        "연도별 HHI는 2023년 281.4508, 2024년 271.1722, "
        "2025년 262.4174입니다."
    )

    result = validate_fusion_answer(
        GeneratedFusionAnswer(
            claims=(
                GeneratedFusionClaim(text=text, evidence_ids=(fact.evidence_id,)),
            )
        ),
        _bundle(fact),
    )

    assert [claim.text for claim in result.answer.claims] == [text]


def test_hhi_annual_series_rejects_value_without_its_period_label() -> None:
    fact = replace(
        _period_fact(
            "fact-hhi-series-missing-period",
            periods=("2023", "2024"),
            value=271.1722,
            metric="hhi",
        ),
        raw_result={
            "render_data": {
                "hhi_recent": 271.1722,
                "hhi_period": "2024",
                "hhi_series_5y": (
                    {"period": "2023", "hhi": 281.4508},
                    {"period": "2024", "hhi": 271.1722},
                ),
            }
        },
    )

    result = validate_fusion_answer(
        GeneratedFusionAnswer(
            claims=(
                GeneratedFusionClaim(
                    text="HHI는 2023년 281.4508, 이어서 271.1722입니다.",
                    evidence_ids=(fact.evidence_id,),
                ),
            )
        ),
        _bundle(fact),
    )

    assert result.answer.claims == ()
    assert result.audit.rejected_claims[0].reason == "hhi_period_missing"


def test_hhi_annual_series_rejects_values_swapped_between_period_labels() -> None:
    fact = replace(
        _period_fact(
            "fact-hhi-series-swapped",
            periods=("2023", "2024"),
            value=271.1722,
            metric="hhi",
        ),
        raw_result={
            "render_data": {
                "hhi_recent": 271.1722,
                "hhi_period": "2024",
                "hhi_series_5y": (
                    {"period": "2023", "hhi": 281.4508},
                    {"period": "2024", "hhi": 271.1722},
                ),
            }
        },
    )

    result = validate_fusion_answer(
        GeneratedFusionAnswer(
            claims=(
                GeneratedFusionClaim(
                    text="HHI는 2023년 271.1722, 2024년 281.4508입니다.",
                    evidence_ids=(fact.evidence_id,),
                ),
            )
        ),
        _bundle(fact),
    )

    assert result.answer.claims == ()
    assert result.audit.rejected_claims[0].reason == "hhi_period_missing"


def test_hhi_annual_series_accepts_duplicate_value_bound_to_explicit_period() -> None:
    fact = replace(
        _period_fact(
            "fact-hhi-series-duplicate-value",
            periods=("2023", "2024"),
            value=271.1722,
            metric="hhi",
        ),
        raw_result={
            "render_data": {
                "hhi_recent": 271.1722,
                "hhi_period": "2024",
                "hhi_series_5y": (
                    {"period": "2023", "hhi": 271.1722},
                    {"period": "2024", "hhi": 271.1722},
                ),
            }
        },
    )
    text = "2024년 HHI는 271.1722입니다."

    result = validate_fusion_answer(
        GeneratedFusionAnswer(
            claims=(
                GeneratedFusionClaim(text=text, evidence_ids=(fact.evidence_id,)),
            )
        ),
        _bundle(fact),
    )

    assert [claim.text for claim in result.answer.claims] == [text]


def test_hhi_annual_series_rejects_one_value_claimed_for_mismatched_periods() -> None:
    fact = replace(
        _period_fact(
            "fact-hhi-series-grouped-wrong",
            periods=("2023", "2024"),
            value=271.1722,
            metric="hhi",
        ),
        raw_result={
            "render_data": {
                "hhi_recent": 271.1722,
                "hhi_period": "2024",
                "hhi_series_5y": (
                    {"period": "2023", "hhi": 281.4508},
                    {"period": "2024", "hhi": 271.1722},
                ),
            }
        },
    )

    result = validate_fusion_answer(
        GeneratedFusionAnswer(
            claims=(
                GeneratedFusionClaim(
                    text="2023년과 2024년 HHI는 271.1722입니다.",
                    evidence_ids=(fact.evidence_id,),
                ),
            )
        ),
        _bundle(fact),
    )

    assert result.answer.claims == ()
    assert result.audit.rejected_claims[0].reason == "hhi_period_missing"


def test_hhi_annual_series_accepts_respectively_ordered_period_value_lists() -> None:
    fact = replace(
        _period_fact(
            "fact-hhi-series-respectively",
            periods=("2023", "2024"),
            value=271.1722,
            metric="hhi",
        ),
        raw_result={
            "render_data": {
                "hhi_recent": 271.1722,
                "hhi_period": "2024",
                "hhi_series_5y": (
                    {"period": "2023", "hhi": 281.4508},
                    {"period": "2024", "hhi": 271.1722},
                ),
            }
        },
    )
    text = "2023년과 2024년 HHI는 각각 281.4508과 271.1722입니다."

    result = validate_fusion_answer(
        GeneratedFusionAnswer(
            claims=(
                GeneratedFusionClaim(text=text, evidence_ids=(fact.evidence_id,)),
            )
        ),
        _bundle(fact),
    )

    assert [claim.text for claim in result.answer.claims] == [text]


def test_hhi_monthly_series_prefers_specific_month_spans_over_shared_year() -> None:
    fact = replace(
        _period_fact(
            "fact-hhi-monthly-respectively",
            periods=("2026-04", "2026-05"),
            value=263.6207,
            metric="hhi",
        ),
        raw_result={
            "render_data": {
                "hhi_recent": 263.6207,
                "hhi_period": "2026-05",
                "hhi_series_5y": (
                    {"period": "2026-04", "hhi": 253.6207},
                    {"period": "2026-05", "hhi": 263.6207},
                ),
            }
        },
    )
    text = "2026년 04월과 2026년 05월 HHI는 각각 253.6207과 263.6207입니다."

    result = validate_fusion_answer(
        GeneratedFusionAnswer(
            claims=(
                GeneratedFusionClaim(text=text, evidence_ids=(fact.evidence_id,)),
            )
        ),
        _bundle(fact),
    )

    assert [claim.text for claim in result.answer.claims] == [text]


def test_hhi_monthly_series_accepts_uniquely_resolved_bare_month() -> None:
    fact = replace(
        _period_fact(
            "fact-hhi-monthly-bare-month",
            periods=("2026-04", "2026-05"),
            value=263.6207,
            metric="hhi",
        ),
        raw_result={
            "render_data": {
                "hhi_recent": 263.6207,
                "hhi_period": "2026-05",
                "hhi_series_5y": (
                    {"period": "2026-04", "hhi": 253.6207},
                    {"period": "2026-05", "hhi": 263.6207},
                ),
            }
        },
    )
    text = "2026년 04월과 05월 HHI는 각각 253.6207과 263.6207입니다."

    result = validate_fusion_answer(
        GeneratedFusionAnswer(
            claims=(
                GeneratedFusionClaim(text=text, evidence_ids=(fact.evidence_id,)),
            )
        ),
        _bundle(fact),
    )

    assert [claim.text for claim in result.answer.claims] == [text]


def test_active_member_count_cannot_be_labeled_as_full_population() -> None:
    fact = _population_fact()
    answer = GeneratedFusionAnswer(
        claims=(
            GeneratedFusionClaim(
                text="이 시장의 전체 브랜드는 9개입니다.",
                evidence_ids=(fact.evidence_id,),
            ),
        )
    )

    result = validate_fusion_answer(answer, _bundle(fact))

    assert result.answer.claims == ()
    assert result.audit.rejected_claims[0].reason == "population_layer_mismatch"


@pytest.mark.parametrize(
    "text",
    (
        "이 시장의 전체 mart 관측 브랜드는 10개입니다.",
        "2026-Q1 양수 실적 활성 브랜드는 9개입니다.",
        "화면 표시 브랜드는 상위 5개입니다.",
    ),
)
def test_population_claim_is_accepted_when_count_and_layer_label_match(text: str) -> None:
    fact = _population_fact()
    answer = GeneratedFusionAnswer(
        claims=(GeneratedFusionClaim(text=text, evidence_ids=(fact.evidence_id,)),)
    )

    result = validate_fusion_answer(answer, _bundle(fact))

    assert [claim.text for claim in result.answer.claims] == [text]


def test_market_size_and_hhi_with_different_periods_cannot_share_a_claim() -> None:
    market_size = _fact("fact-size", value=42559564361.0, metric="market_size", unit="KRW")
    hhi = _fact("fact-hhi", value=3188.0404, metric="hhi", unit="index")
    hhi = replace(hhi, period="2025")
    answer = GeneratedFusionAnswer(
        claims=(
            GeneratedFusionClaim(
                text="2026-Q1 시장 규모는 42559564361.0원이고 2025 HHI는 3188.0404입니다.",
                evidence_ids=(market_size.evidence_id, hhi.evidence_id),
            ),
        )
    )

    result = validate_fusion_answer(answer, _bundle(market_size, hhi))

    assert result.answer.claims == ()
    assert result.audit.rejected_claims[0].reason == "market_hhi_period_mismatch"


def test_prompt_contains_fact_values_and_safe_failure_language_only() -> None:
    fact = _fact("fact-hhi", value=3188.0404, metric="hhi", unit="index")
    failure = ToolFailureRecord(
        tool_name="market.get_market_size",
        arguments={"scope": {"kind": "general_composite"}},
        stage="execution",
        error_type="GeneralCompositeUnavailableError",
        message="formula parity internal detail",
    )

    messages = build_fusion_messages("시장 집중도를 알려줘", _bundle(fact, failures=(failure,)))
    user_payload = json.loads(messages[1]["content"])

    assert user_payload["evidence"][0]["evidence_id"] == "fact-hhi"
    assert "3188.0404" in user_payload["evidence"][0]["allowed_numeric_literals"]
    assert user_payload["failures"] == [
        {
            "reason_code": "general_composite_unavailable",
            "limitation": "현재 지원하지 않는 시장 조합입니다.",
        }
    ]
    assert "formula parity" not in messages[1]["content"]


def test_external_list_items_receive_deterministic_v3_shadow_evidence_ids() -> None:
    fact = _clinical_list_fact()

    first_messages = build_fusion_messages("뇌경색 임상시험", _bundle(fact))
    second_messages = build_fusion_messages("뇌경색 임상시험", _bundle(fact))
    first_evidence = json.loads(first_messages[1]["content"])["evidence"]
    second_evidence = json.loads(second_messages[1]["content"])["evidence"]

    assert first_evidence == second_evidence
    assert len(first_evidence) == 3
    parent, trial_item, count_item = first_evidence
    assert parent["evidence_id"] == fact.evidence_id
    assert "evidence" not in parent["raw_result"]
    assert trial_item["evidence_id"].startswith(
        "v3-shadow:clinicaltrials_v2_search:"
    )
    assert count_item["evidence_id"].startswith(
        "v3-shadow:clinicaltrials_v2_search:"
    )
    assert len(trial_item["evidence_id"].rsplit(":", 1)[1]) == 16
    assert len(count_item["evidence_id"].rsplit(":", 1)[1]) == 16
    assert trial_item["evidence_id"] != count_item["evidence_id"]
    assert "1048" not in trial_item["allowed_numeric_literals"]
    assert "1048" in count_item["allowed_numeric_literals"]
    assert "fact_id" not in trial_item["raw_result"]
    assert "raw_ref" not in trial_item["raw_result"]


def test_external_item_id_is_strictly_validated_without_accepting_legacy_id() -> None:
    fact = _clinical_list_fact()
    payload = json.loads(
        build_fusion_messages("뇌경색 임상시험", _bundle(fact))[1]["content"]
    )
    count_id = payload["evidence"][2]["evidence_id"]
    generated = GeneratedFusionAnswer(
        claims=(
            GeneratedFusionClaim(
                text="원천 제공 총 건수는 1048건입니다.",
                evidence_ids=(count_id,),
            ),
            GeneratedFusionClaim(
                text="원천 제공 총 건수는 1048건입니다.",
                evidence_ids=("clinicaltrials_v2_search:원천_제공_총_건수",),
            ),
        )
    )

    result = validate_fusion_answer(generated, _bundle(fact))

    assert [claim.evidence_ids for claim in result.answer.claims] == [(count_id,)]
    assert result.audit.rejected_claims[0].reason == "unknown_evidence_reference"


def test_prompt_requires_exact_supplied_ids_and_forbids_constructed_ids() -> None:
    system_prompt = build_fusion_messages(
        "뇌경색 임상시험", _bundle(_clinical_list_fact())
    )[0]["content"]

    assert "그대로 복사" in system_prompt
    assert "새로 만들지" in system_prompt
    assert "한글 필드명" in system_prompt
    assert "순번" in system_prompt
    assert "NCT" in system_prompt
    assert "limitations" in system_prompt


def test_engine_preserves_raw_provider_response_and_validates_before_return() -> None:
    fact = _fact("fact-share", value=51.38)

    class FakeProvider:
        def generate(self, *, messages: list[dict[str, str]]) -> FusionProviderResult:
            assert len(messages) == 2
            return FusionProviderResult(
                content=json.dumps(
                    {
                        "claims": [
                            {
                                "text": "아일리아 점유율은 51.38%입니다.",
                                "evidence_ids": ["fact-share"],
                            }
                        ],
                        "limitations": [],
                    },
                    ensure_ascii=False,
                ),
                raw_text='{"model":"fixture","usage":{"total_tokens":10}}',
                raw_bytes_sha256="b" * 64,
                raw_response={"model": "fixture", "usage": {"total_tokens": 10}},
                usage={"total_tokens": 10},
                model="fixture",
                latency_ms=1.0,
                completed_at_utc="2026-08-05T00:00:00Z",
                request_body_sha256="a" * 64,
                finish_reason="stop",
            )

    result = V3FusionEngine(FakeProvider()).generate("점유율 알려줘", _bundle(fact))

    assert result.validated.answer.claims[0].evidence_ids == ("fact-share",)
    assert result.provider.raw_response["model"] == "fixture"
    assert result.generated.claims[0].text == "아일리아 점유율은 51.38%입니다."


def test_genos_provider_records_usage_without_exposing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        encoding = "utf-8"

        @property
        def text(self) -> str:
            return json.dumps(self.json(), ensure_ascii=False, separators=(",", ":"))

        @property
        def content(self) -> bytes:
            return self.text.encode("utf-8")

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "model": "genos/514/fixture",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"claims":[],"limitations":["확인할 근거가 없습니다."]}'
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                },
            }

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr("jw_chat_agent_poc.tool_use.v3_fusion_provider.requests.post", fake_post)
    provider = GenosV3FusionProvider(
        base_url="https://example.invalid/api/gateway/rep/serving/514",
        token="fixture-secret",
        model="fixture-model",
    )

    result = provider.generate(messages=[{"role": "user", "content": "fixture"}])

    assert result.model == "genos/514/fixture"
    assert result.usage == {
        "model": "genos/514/fixture",
        "serving_id": "514",
        "stream": False,
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "raw_usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
        },
    }
    assert captured["headers"] == {"Authorization": "Bearer fixture-secret"}
    assert captured["json"]["max_tokens"] == 8192
    assert result.finish_reason == "stop"
    assert "fixture-secret" not in json.dumps(result.raw_response)
    assert result.raw_text == FakeResponse().text
    assert result.raw_bytes_sha256 == hashlib.sha256(
        result.raw_text.encode("utf-8")
    ).hexdigest()


def test_engine_reports_length_finish_as_typed_failure_without_partial_recovery() -> None:
    fact = _fact("fact-share", value=51.38)

    class TruncatedProvider:
        def generate(self, *, messages: list[dict[str, str]]) -> FusionProviderResult:
            assert len(messages) == 2
            return FusionProviderResult(
                content='{"claims":[{"text":"아일리아 점유율은 51.38%',
                raw_text='{"choices":[{"finish_reason":"length"}]}',
                raw_bytes_sha256="b" * 64,
                raw_response={"choices": [{"finish_reason": "length"}]},
                usage={"output_tokens": 4092},
                model="fixture",
                latency_ms=1.0,
                completed_at_utc="2026-08-05T00:00:00Z",
                request_body_sha256="a" * 64,
                finish_reason="length",
            )

    with pytest.raises(FusionOutputTruncatedError) as caught:
        V3FusionEngine(TruncatedProvider()).generate("점유율 알려줘", _bundle(fact))

    assert caught.value.reason_code == "fusion_output_truncated"
    assert caught.value.limitations == (
        "응답이 출력 상한에서 잘려 일부를 확인하지 못했습니다.",
    )
    assert caught.value.provider.finish_reason == "length"
