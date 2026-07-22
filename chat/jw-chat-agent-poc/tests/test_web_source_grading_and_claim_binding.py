from __future__ import annotations

from dataclasses import asdict

from jw_chat_agent_poc.orchestrator.provenance import EvidenceFact, evidence_from_calls
from jw_chat_agent_poc.orchestrator.markdown_response import MarkdownResponseBuilder
from jw_chat_agent_poc.orchestrator.source_grading import (
    SourceGrade,
    grade_evidence_source,
    grade_web_url,
    requested_authority_source_explicit,
)
from jw_chat_agent_poc.service.app import compute_final_answer
from jw_chat_agent_poc.service.evidence_binding import (
    expected_entities_from_result,
    verify_claim_bindings,
)
from jw_chat_agent_poc.service.genos_client import _without_web_fact_context
from jw_chat_agent_poc.service.runtime_numeric_grounding import ungrounded_numbers
from jw_chat_agent_poc.service.web_mi_summary import (
    web_search_mi_section,
    web_search_mi_section_from_calls,
)
from jw_chat_agent_poc.service.web_presentation_policy import web_presentation_policy
from jw_chat_agent_poc.tool_use.routing_v4_execution import official_web_fallback_policy


def _fact(
    *,
    value: str,
    entity: str,
    metric: str,
    period: str,
    unit: str,
    source_grade: SourceGrade = SourceGrade.AUTHORITATIVE,
    operand_fact_ids: tuple[str, ...] = (),
) -> EvidenceFact:
    return EvidenceFact(
        fact_id=f"fact_{entity}_{metric}_{period}",
        label=metric,
        value=value,
        source="HIRA" if metric == "환자수" else "mart",
        tool="hira_stats" if metric == "환자수" else "get_market_metric",
        path="render_data.items[0]",
        period=period,
        allowed_numbers=(value,),
        entity=entity,
        metric=metric,
        unit=unit,
        source_grade=source_grade.value,
        view="strategic_ml" if metric == "HHI" else "",
        operand_fact_ids=operand_fact_ids,
    )


def test_source_grades_distinguish_authoritative_official_web_and_general_web() -> None:
    assert grade_evidence_source(tool="hira_stats", source="HIRA") is SourceGrade.AUTHORITATIVE
    assert grade_web_url("https://opendata.hira.or.kr/official") is SourceGrade.SUPPLEMENTARY
    assert grade_web_url("https://blog.naver.com/unverified") is SourceGrade.UNVERIFIED


def test_source_explicit_request_never_silently_enables_web_fallback(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_TOOL_ROUTING_OFFICIAL_WEB_FALLBACK_ENABLED", "true")

    decision = official_web_fallback_policy(
        source_domain="hira",
        runtime_reason="UPSTREAM_UNAVAILABLE",
        usable_authoritative_results=0,
        candidate_urls=("https://opendata.hira.or.kr/official",),
        requested_source_explicit=True,
    )

    assert decision.web_call_budget == 0
    assert decision.accepted_urls == ()
    assert decision.reason_code == "EXPLICIT_SOURCE_NO_FALLBACK"


def test_natural_explicit_hira_phrases_all_disable_web_fallback() -> None:
    for question in (
        "HIRA D693 환자수 추이를 알려줘",
        "HIRA에서 D693 환자수 추이를 알려줘",
        "HIRA 공식 통계로 D693 환자수 추이를 알려줘",
        "심평원 공식 자료 기준 D693 환자수 추이를 알려줘",
    ):
        assert requested_authority_source_explicit(question, source_domain="hira"), question


def test_non_runtime_failure_reasons_never_enable_web_fallback(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_TOOL_ROUTING_OFFICIAL_WEB_FALLBACK_ENABLED", "true")

    for reason in (
        "NO_MATCH",
        "CAPABILITY_NOT_IMPLEMENTED",
        "INVALID_INPUT",
        "AMBIGUOUS_INPUT",
    ):
        decision = official_web_fallback_policy(
            source_domain="hira",
            runtime_reason=reason,
            usable_authoritative_results=0,
            candidate_urls=("https://opendata.hira.or.kr/official",),
        )

        assert decision.web_call_budget == 0, reason
        assert decision.accepted_urls == (), reason
        assert decision.reason_code == reason


def test_partial_authoritative_result_is_preserved_without_web_merge(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_TOOL_ROUTING_OFFICIAL_WEB_FALLBACK_ENABLED", "true")

    decision = official_web_fallback_policy(
        source_domain="hira",
        runtime_reason="UPSTREAM_UNAVAILABLE",
        usable_authoritative_results=4,
        candidate_urls=("https://opendata.hira.or.kr/official",),
    )

    assert decision.web_call_budget == 0
    assert decision.accepted_urls == ()
    assert decision.separate_section is False
    assert decision.reason_code == "PARTIAL_RESULT"


def _web_call(*items: dict[str, str]) -> dict[str, object]:
    return {
        "tool": "web_search",
        "source": "web_search",
        "status": "ok",
        "render_data": {"items": list(items)},
    }


def _hira_call(*, status: str, error_code: str = "") -> dict[str, object]:
    render_data: dict[str, object] = {}
    if error_code:
        render_data["error_code"] = error_code
    if status == "ok":
        render_data["items"] = [{"sick_cd": "D693", "year": "2025", "patients": 123}]
    return {
        "tool": "hira_disease_hospitalization_outpatient_stats",
        "source": "HIRA",
        "status": status,
        "render_data": render_data,
    }


def test_public_web_section_suppresses_silent_fallback_for_explicit_hira_request() -> None:
    section = web_search_mi_section_from_calls(
        (
            _hira_call(status="error", error_code="UPSTREAM_UNAVAILABLE"),
            _web_call(
                {
                    "title": "HIRA 통계 안내",
                    "url": "https://opendata.hira.or.kr/guide",
                    "snippet": "공식 사이트의 통계 조회 안내",
                }
            ),
        ),
        question="HIRA: 상병코드 D693의 최근 5개년 환자수 추이를 분석해줘",
    )

    assert section == ""


def test_public_web_section_keeps_only_matching_official_domain_after_upstream_failure() -> None:
    section = web_search_mi_section_from_calls(
        (
            _hira_call(status="error", error_code="UPSTREAM_UNAVAILABLE"),
            _web_call(
                {
                    "title": "HIRA 통계 안내",
                    "url": "https://opendata.hira.or.kr/guide",
                    "snippet": "공식 사이트의 통계 조회 안내",
                },
                {
                    "title": "D693 환자수 블로그",
                    "url": "https://blog.naver.com/unverified-d693",
                    "snippet": "D693 환자수 999명",
                },
            ),
        ),
        question="상병코드 D693의 최근 5개년 환자수 추이를 분석해줘",
    )

    assert "HIRA 공식 통계 조회에 실패했습니다(UPSTREAM_UNAVAILABLE)" in section
    assert "공식 통계가 아닙니다" in section
    assert "https://opendata.hira.or.kr/guide" in section
    assert "blog.naver.com" not in section
    assert "999" not in section


def test_public_web_section_rejects_web_when_no_authoritative_route_was_attempted() -> None:
    section = web_search_mi_section_from_calls(
        (
            _web_call(
                {
                    "title": "D693 환자수 블로그",
                    "url": "https://blog.naver.com/unverified-d693",
                    "snippet": "D693 환자수 999명",
                }
            ),
        ),
        question="상병코드 D693의 최근 5개년 환자수 추이를 분석해줘",
    )

    assert section == ""


def test_public_web_section_allows_explicit_news_request_with_unverified_disclosure() -> None:
    section = web_search_mi_section_from_calls(
        (
            _web_call(
                {
                    "title": "리바로 최근 이슈",
                    "url": "https://news.example.com/livalo",
                    "snippet": "리바로 관련 최근 동향",
                }
            ),
        ),
        question="리바로 관련 최근 이슈를 웹에서 찾아줘",
    )

    assert "리바로 최근 이슈" in section
    assert "UNVERIFIED" in section
    assert "공식 통계가 아닙니다" in section


def test_public_web_section_does_not_mix_web_into_partial_authoritative_result() -> None:
    section = web_search_mi_section_from_calls(
        (
            _hira_call(status="ok"),
            _web_call(
                {
                    "title": "D693 웹 보강",
                    "url": "https://opendata.hira.or.kr/guide",
                    "snippet": "웹 보강 자료",
                }
            ),
        ),
        question="상병코드 D693의 환자수 추이를 알려줘",
    )

    assert section == ""


def test_final_generation_context_keeps_web_results_out_of_authoritative_facts() -> None:
    response = MarkdownResponseBuilder().build(
        brand="D693",
        calls=[
            _hira_call(status="ok"),
            _web_call(
                {
                    "title": "D693 환자수 블로그",
                    "url": "https://blog.naver.com/unverified-d693",
                    "snippet": "D693 환자수 999명",
                }
            ),
        ],
        sources=["HIRA", "web_search"],
    )
    sanitized = _without_web_fact_context(
        response.to_dict(),
        calls=[
            _hira_call(status="ok"),
            _web_call(
                {
                    "title": "D693 환자수 블로그",
                    "url": "https://blog.naver.com/unverified-d693",
                    "snippet": "D693 환자수 999명",
                }
            ),
        ],
        brand="D693",
        sources=["HIRA", "web_search"],
    )

    assert "D693 환자수 블로그" in response.data_md
    assert "D693 환자수 블로그" not in sanitized["data_md"]
    assert "blog.naver.com" not in sanitized["fact_md"]
    assert "web_search" not in sanitized["summary_md"]


def test_error_only_web_call_does_not_discard_existing_authoritative_fact_context() -> None:
    response = {
        "fact_md": "글로벌 임상시험 = NCT00257686\n국내 임상시험 = HL040XC정",
        "data_md": "",
        "allowed_numbers": ("NCT00257686",),
    }

    sanitized = _without_web_fact_context(
        response,
        calls=[
            {"tool": "clinicaltrials_v2_search", "source": "clinicaltrials_mcp"},
            {"tool": "mfds_clinical_trial_kr", "source": "nedrug_mcp"},
            {
                "tool": "web_search",
                "source": "web_search",
                "status": "error",
                "render_data": {"error_code": "UPSTREAM_UNAVAILABLE"},
            },
        ],
        brand="고지혈증",
        sources=["clinicaltrials_mcp", "nedrug_mcp", "web_search"],
    )

    assert sanitized == response


def test_upstream_failure_accepts_only_matching_official_domain(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_TOOL_ROUTING_OFFICIAL_WEB_FALLBACK_ENABLED", "true")

    decision = official_web_fallback_policy(
        source_domain="hira",
        runtime_reason="UPSTREAM_UNAVAILABLE",
        usable_authoritative_results=0,
        candidate_urls=(
            "https://opendata.hira.or.kr/official",
            "https://www.mfds.go.kr/unrelated",
            "https://blog.naver.com/unverified",
        ),
    )

    assert decision.web_call_budget == 1
    assert decision.accepted_urls == ("https://opendata.hira.or.kr/official",)
    assert decision.separate_section is True
    assert "공식 통계가 아닙니다" in decision.disclosure


def test_web_summary_discloses_grade_and_not_official_statistics() -> None:
    section = web_search_mi_section(
        (
            {
                "title": "HIRA 안내",
                "url": "https://www.hira.or.kr/guide",
                "snippet": "공식 시스템 이용 안내입니다.",
            },
            {
                "title": "블로그 해설",
                "url": "https://blog.naver.com/unverified",
                "snippet": "개인 해설입니다.",
            },
        )
    )

    assert "공식 통계가 아닙니다" in section
    assert "SUPPLEMENTARY" in section
    assert "UNVERIFIED" in section


def test_web_summary_never_dedupes_across_source_grades() -> None:
    section = web_search_mi_section(
        (
            {
                "title": "리바로젯 이상지질혈증 복합제 매출 1위",
                "url": "https://www.hira.or.kr/official-story",
                "snippet": "2026-06-17 리바로젯 이상지질혈증 복합제 매출 1위 안내",
            },
            {
                "title": "리바로젯 이상지질혈증 복합제 매출 1위",
                "url": "https://blog.example.test/unverified-story",
                "snippet": "2026-06-17 리바로젯 이상지질혈증 복합제 매출 1위 해설",
            },
        )
    )

    assert "https://www.hira.or.kr/official-story" in section
    assert "https://blog.example.test/unverified-story" in section
    assert "매체 병합: 2건" not in section


def test_web_numbers_cannot_ground_a_numeric_claim() -> None:
    tool_calls = (
        {
            "tool": "web_search",
            "status": "ok",
            "render_data": {
                "items": (
                    {
                        "title": "일반 웹",
                        "url": "https://example.test/stat",
                        "snippet": "환자수는 12345명입니다.",
                    },
                )
            },
        },
    )

    assert ungrounded_numbers(
        "환자수는 12345명입니다.",
        {"allowed_numbers": (), "fact_md": "", "data_md": ""},
        tool_calls,
    ) == ("12345명",)


def test_hira_patient_fact_is_bound_to_exact_code_metric_period_and_unit() -> None:
    calls = [
        {
            "tool": "hira_disease_hospitalization_outpatient_stats",
            "source": "HIRA",
            "status": "ok",
            "render_data": {
                "calls": [
                    {
                        "render_data": {
                            "items": [
                                {
                                    "sickCd": "H36.0",
                                    "sickNm": "당뇨병성 망막병증",
                                    "year": "2024",
                                    "ptntCnt": 12345,
                                }
                            ]
                        }
                    }
                ]
            },
        }
    ]

    patient_fact = next(fact for fact in evidence_from_calls(calls, "") if fact.metric == "환자수")

    assert patient_fact.entity == "H36.0"
    assert patient_fact.period == "2024"
    assert patient_fact.unit == "명"
    assert patient_fact.source_grade == SourceGrade.AUTHORITATIVE.value


def test_web_shaped_patient_payload_is_not_promoted_to_authoritative() -> None:
    calls = [
        {
            "tool": "web_search",
            "source": "web_search",
            "status": "ok",
            "render_data": {
                "items": [
                    {
                        "sickCd": "H36.0",
                        "year": "2024",
                        "ptntCnt": 12345,
                    }
                ]
            },
        }
    ]

    patient_fact = next(fact for fact in evidence_from_calls(calls, "") if fact.metric == "환자수")

    assert patient_fact.source_grade == SourceGrade.UNVERIFIED.value


def test_share_delta_fact_is_bound_as_percentage_points() -> None:
    facts = evidence_from_calls(
        [
            {
                "tool": "agent_calculation",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "metric": "market_share_delta",
                    "period": "2026-03→2026-04",
                    "ms_delta_pct": -0.13,
                },
            }
        ],
        "",
    )

    delta = next(fact for fact in facts if fact.label == "점유율 변화")

    assert delta.unit == "%p"


def test_exact_disease_code_mismatch_is_blocked() -> None:
    result = verify_claim_bindings(
        question="H36.0 환자수는?",
        answer="2024년 H36 환자수는 12345명입니다.",
        facts=(_fact(value="12345명", entity="H36", metric="환자수", period="2024", unit="명"),),
        expected_entities=("H36.0",),
    )

    assert result.status == "fail"
    assert result.disposition == "unavailable"
    assert "12345" not in result.answer
    assert "ENTITY_MISMATCH" in result.blocked_reasons


def test_expected_entity_uses_v4_normalized_disease_code() -> None:
    result = {
        "router_diagnostics": {
            "routing_v4": {
                "proposed_routing_signature": {
                    "proposed_calls": [
                        {
                            "normalized_args": {
                                "sick_cd": "H36.0",
                            }
                        }
                    ]
                }
            }
        }
    }

    assert expected_entities_from_result("당뇨병성 망막병증 환자수", result) == ("H36.0",)


def test_expected_entity_uses_only_high_confidence_legacy_disease_alias() -> None:
    assert expected_entities_from_result("고지혈증 환자수", {}) == ("E78",)
    assert expected_entities_from_result("당뇨병성 망막병증 환자수", {}) == ()


def test_parent_diabetes_code_cannot_support_retinopathy_claim() -> None:
    result = verify_claim_bindings(
        question="당뇨병성 망막병증 환자수는?",
        answer="2024년 환자수는 67890명입니다.",
        facts=(_fact(value="67890명", entity="E11", metric="환자수", period="2024", unit="명"),),
        expected_entities=("H36.0",),
    )

    assert result.status == "fail"
    assert "67890" not in result.answer


def test_patient_statistic_without_expected_entity_binding_fails_closed() -> None:
    result = verify_claim_bindings(
        question="당뇨병성 망막병증의 2024년 환자수는?",
        answer="2024년 환자수는 67890명입니다.",
        facts=(_fact(value="67890명", entity="E11", metric="환자수", period="2024", unit="명"),),
    )

    assert result.status == "fail"
    assert "MISSING_EXPECTED_ENTITY_BINDING" in result.blocked_reasons
    assert "67890" not in result.answer


def test_exact_bound_fact_passes() -> None:
    result = verify_claim_bindings(
        question="H36.0의 2024년 환자수는?",
        answer="H36.0의 2024년 환자수는 12345명입니다.",
        facts=(_fact(value="12345명", entity="H36.0", metric="환자수", period="2024", unit="명"),),
        expected_entities=("H36.0",),
    )

    assert result.status == "pass"
    assert result.answer.endswith("12345명입니다.")


def test_bare_year_in_table_uses_the_bound_fact_period() -> None:
    result = verify_claim_bindings(
        question="고지혈증 환자수",
        answer="| 질병코드 | 연도 | 환자수(명) |\n| E78 | 2024 | 1,305,727 |",
        facts=(
            _fact(
                value="1305727",
                entity="E78",
                metric="환자수",
                period="2024",
                unit="명",
            ),
        ),
        expected_entities=("E78",),
    )

    assert result.status == "pass"


def test_numeric_claim_without_any_bound_fact_is_blocked() -> None:
    result = verify_claim_bindings(
        question="H36.0의 2024년 환자수는?",
        answer="H36.0 환자수는 99999명입니다.",
        facts=(_fact(value="12345명", entity="H36.0", metric="환자수", period="2024", unit="명"),),
        expected_entities=("H36.0",),
    )

    assert result.status == "fail"
    assert "MISSING_EVIDENCE_BINDING" in result.blocked_reasons
    assert "99999" not in result.answer


def test_series_insight_share_values_use_canonical_metric_binding() -> None:
    facts = evidence_from_calls(
        [
            {
                "tool": "get_brand_series",
                "source": "mart",
                "render_data": {
                    "brand": "리바로",
                    "period": "2026-01~2026-05",
                    "series_insight": {
                        "share_start_pct": 3.55,
                        "share_end_pct": 3.81,
                    },
                },
            }
        ],
        "",
    )
    share_metrics = {fact.label: fact.metric for fact in facts if "점유율" in fact.label}

    assert share_metrics == {
        "점유율 시작": "시장점유율",
        "점유율 종료": "시장점유율",
    }
    result = verify_claim_bindings(
        question="리바로 시장점유율 추이는?",
        answer="리바로 시장점유율은 3.55%에서 3.81%로 변했습니다.",
        facts=facts,
        expected_entities=("리바로",),
    )
    assert result.status == "pass"


def test_competitor_series_fact_is_bound_to_competitor_not_anchor_brand() -> None:
    facts = evidence_from_calls(
        [
            {
                "tool": "get_brand_series",
                "source": "mart",
                "render_data": {
                    "brand": "리바로",
                    "period": "2026-01~2026-05",
                    "series_insight": {
                        "competitors": [
                            {
                                "brand": "로수젯",
                                "share_end_pct": 8.0,
                            }
                        ]
                    },
                },
            }
        ],
        "",
    )
    competitor = next(fact for fact in facts if fact.label == "경쟁 브랜드 종료 점유율")

    assert competitor.entity == "로수젯"
    result = verify_claim_bindings(
        question="리바로 시장점유율은?",
        answer="리바로 시장점유율은 8.00%입니다.",
        facts=facts,
        expected_entities=("리바로",),
    )
    assert result.status == "fail"
    assert "ENTITY_MISMATCH" in result.blocked_reasons


def test_ordered_list_marker_is_not_treated_as_a_numeric_claim() -> None:
    result = verify_claim_bindings(
        question="H36.0의 2024년 환자수는?",
        answer="1. H36.0의 2024년 환자수는 12345명입니다.",
        facts=(_fact(value="12345명", entity="H36.0", metric="환자수", period="2024", unit="명"),),
        expected_entities=("H36.0",),
    )

    assert result.status == "pass"


def test_series_growth_and_excess_growth_use_canonical_units_and_metrics() -> None:
    facts = evidence_from_calls(
        [
            {
                "tool": "get_brand_series",
                "source": "mart",
                "render_data": {
                    "brand": "리바로",
                    "period": "2026-04~2026-05",
                    "series_insight": {
                        "brand_growth_pct": 5.0,
                        "market_growth_pct": -2.8,
                        "excess_growth_pctp": 7.8,
                    },
                },
            }
        ],
        "",
    )
    by_label = {fact.label: fact for fact in facts}

    assert by_label["브랜드 성장률"].unit == "%"
    assert by_label["시장 성장률"].unit == "%"
    assert by_label["초과성장"].unit == "%p"
    growth = verify_claim_bindings(
        question="리바로 브랜드 성장률은?",
        answer="리바로 브랜드 성장률은 5.00%입니다.",
        facts=facts,
        expected_entities=("리바로",),
    )
    excess = verify_claim_bindings(
        question="리바로 초과성장은?",
        answer="리바로 초과성장은 7.80%p입니다.",
        facts=facts,
        expected_entities=("리바로",),
    )

    assert growth.status == "pass"
    assert excess.status == "pass"


def test_unverified_web_fact_cannot_support_authoritative_patient_statistic() -> None:
    result = verify_claim_bindings(
        question="H36.0의 2024년 환자수는?",
        answer="H36.0의 2024년 환자수는 12345명입니다.",
        facts=(
            _fact(
                value="12345명",
                entity="H36.0",
                metric="환자수",
                period="2024",
                unit="명",
                source_grade=SourceGrade.UNVERIFIED,
            ),
        ),
        expected_entities=("H36.0",),
    )

    assert result.status == "fail"
    assert "SOURCE_GRADE_MISMATCH" in result.blocked_reasons


def test_authoritative_fact_wins_when_same_number_also_appears_on_unverified_web() -> None:
    result = verify_claim_bindings(
        question="H36.0의 2024년 환자수는?",
        answer="H36.0의 2024년 환자수는 12345명입니다.",
        facts=(
            _fact(value="12345명", entity="H36.0", metric="환자수", period="2024", unit="명"),
            _fact(
                value="12345명",
                entity="H36.0",
                metric="환자수",
                period="2024",
                unit="명",
                source_grade=SourceGrade.UNVERIFIED,
            ),
        ),
        expected_entities=("H36.0",),
    )

    assert result.status == "pass"


def test_supplementary_fact_is_partial_even_when_same_number_is_on_unverified_web() -> None:
    result = verify_claim_bindings(
        question="H36.0의 2024년 환자수는?",
        answer="H36.0의 2024년 환자수는 12345명입니다.",
        facts=(
            _fact(
                value="12345명",
                entity="H36.0",
                metric="환자수",
                period="2024",
                unit="명",
                source_grade=SourceGrade.SUPPLEMENTARY,
            ),
            _fact(
                value="12345명",
                entity="H36.0",
                metric="환자수",
                period="2024",
                unit="명",
                source_grade=SourceGrade.UNVERIFIED,
            ),
        ),
        expected_entities=("H36.0",),
    )

    assert result.status == "partial"
    assert "공식 통계" in result.answer


def test_missing_period_binding_is_partial_instead_of_silently_answered_or_blocked() -> None:
    result = verify_claim_bindings(
        question="H36.0 환자수는?",
        answer="H36.0 환자수는 12345명입니다.",
        facts=(_fact(value="12345명", entity="H36.0", metric="환자수", period="", unit="명"),),
        expected_entities=("H36.0",),
    )

    assert result.status == "partial"
    assert result.disposition == "partial"
    assert "기간" in result.answer


def test_explicit_period_mismatch_is_blocked() -> None:
    result = verify_claim_bindings(
        question="H36.0의 2024년 환자수는?",
        answer="H36.0의 2024년 환자수는 12345명입니다.",
        facts=(_fact(value="12345명", entity="H36.0", metric="환자수", period="2023", unit="명"),),
        expected_entities=("H36.0",),
    )

    assert result.status == "fail"
    assert "PERIOD_MISMATCH" in result.blocked_reasons


def test_annual_period_cannot_be_supported_by_a_single_month_fact() -> None:
    result = verify_claim_bindings(
        question="H36.0의 2024년 환자수는?",
        answer="H36.0의 2024년 환자수는 12345명입니다.",
        facts=(_fact(value="12345명", entity="H36.0", metric="환자수", period="2024-05", unit="명"),),
        expected_entities=("H36.0",),
    )

    assert result.status == "fail"
    assert "PERIOD_MISMATCH" in result.blocked_reasons


def test_bound_value_with_wrong_unit_is_blocked() -> None:
    result = verify_claim_bindings(
        question="H36.0의 2024년 환자수는?",
        answer="H36.0의 2024년 환자수는 12345명입니다.",
        facts=(_fact(value="12345명", entity="H36.0", metric="환자수", period="2024", unit="억원"),),
        expected_entities=("H36.0",),
    )

    assert result.status == "fail"
    assert "UNIT_MISMATCH" in result.blocked_reasons


def test_normal_authoritative_hhi_facts_remain_answerable() -> None:
    result = verify_claim_bindings(
        question="두 시장의 HHI를 비교해줘",
        answer="두 시장의 HHI는 각각 262.4174와 253.62입니다.",
        facts=(
            _fact(value="262.4174", entity="ml_001", metric="HHI", period="2026-05", unit="index"),
            _fact(value="253.62", entity="ml_002", metric="HHI", period="2026-05", unit="index"),
        ),
    )

    assert result.status == "pass"
    assert result.blocked_claim_count == 0


def test_hhi_fact_from_wrong_market_is_blocked_even_when_value_matches() -> None:
    result = verify_claim_bindings(
        question="ml_006의 2026-05 HHI는?",
        answer="ml_006의 2026-05 HHI는 253.62입니다.",
        facts=(
            _fact(value="253.62", entity="ml_002", metric="HHI", period="2026-05", unit="index"),
        ),
        expected_entities=("ml_006",),
    )

    assert result.status == "fail"
    assert "ENTITY_MISMATCH" in result.blocked_reasons


def test_hhi_fact_from_wrong_period_is_blocked_even_when_value_matches() -> None:
    result = verify_claim_bindings(
        question="ml_006의 2026-05 HHI는?",
        answer="ml_006의 2026-05 HHI는 253.62입니다.",
        facts=(
            _fact(value="253.62", entity="ml_006", metric="HHI", period="2026-04", unit="index"),
        ),
        expected_entities=("ml_006",),
    )

    assert result.status == "fail"
    assert "PERIOD_MISMATCH" in result.blocked_reasons


def test_concentration_question_accepts_bound_hhi_and_cr5_together() -> None:
    result = verify_claim_bindings(
        question="이 시장 집중도는?",
        answer="시장 집중도는 HHI 253.62, CR5 29.515799%입니다.",
        facts=(
            _fact(value="253.62", entity="ml_006", metric="HHI", period="2026-05", unit="index"),
            _fact(value="29.515799%", entity="ml_006", metric="CR5", period="2026-05", unit="%"),
        ),
    )

    assert result.status == "pass"


def test_derived_share_delta_fact_declares_both_operand_facts() -> None:
    facts = evidence_from_calls(
        [
            {
                "tool": "agent_calculation",
                "source": "mart",
                "render_data": {
                    "brand": "리바로",
                    "metric": "market_share_delta",
                    "period": "2026-04~2026-05",
                    "from_ms_pct": 3.71,
                    "to_ms_pct": 3.76,
                    "ms_delta_pct": 0.05,
                },
            }
        ],
        "",
    )
    by_label = {fact.label: fact for fact in facts}

    assert set(by_label["점유율 변화"].operand_fact_ids) == {
        by_label["기준 점유율"].fact_id,
        by_label["비교 점유율"].fact_id,
    }


def test_derived_fact_requires_all_declared_operands() -> None:
    left = _fact(value="10억원", entity="리바로", metric="매출", period="2026-04", unit="억원")
    right = _fact(value="12억원", entity="리바로", metric="매출", period="2026-05", unit="억원")
    derived = EvidenceFact(
        fact_id="fact_growth",
        label="매출 변화율",
        value="20%",
        source="mart",
        tool="get_brand_metric",
        path="derived",
        period="2026-04~2026-05",
        allowed_numbers=("20%",),
        entity="리바로",
        metric="매출 변화율",
        unit="%",
        source_grade=SourceGrade.AUTHORITATIVE.value,
        operand_fact_ids=(left.fact_id, right.fact_id),
    )

    missing = verify_claim_bindings(
        question="리바로 매출 변화율은?",
        answer="리바로 매출 변화율은 20%입니다.",
        facts=(left, derived),
    )
    complete = verify_claim_bindings(
        question="리바로 매출 변화율은?",
        answer="리바로 매출 변화율은 20%입니다.",
        facts=(left, right, derived),
    )

    assert missing.status == "fail"
    assert "MISSING_OPERAND" in missing.blocked_reasons
    assert complete.status == "pass"


def test_derived_fact_rejects_operand_from_another_entity() -> None:
    left = _fact(value="10억원", entity="리바로", metric="매출", period="2026-04", unit="억원")
    right = _fact(value="12억원", entity="로수젯", metric="매출", period="2026-05", unit="억원")
    derived = EvidenceFact(
        fact_id="fact_growth_wrong_entity",
        label="매출 변화율",
        value="20%",
        source="mart",
        tool="get_brand_metric",
        path="derived",
        period="2026-04~2026-05",
        allowed_numbers=("20%",),
        entity="리바로",
        metric="매출 변화율",
        unit="%",
        source_grade=SourceGrade.AUTHORITATIVE.value,
        operand_fact_ids=(left.fact_id, right.fact_id),
    )

    result = verify_claim_bindings(
        question="리바로 매출 변화율은?",
        answer="리바로 매출 변화율은 20%입니다.",
        facts=(left, right, derived),
        expected_entities=("리바로",),
    )

    assert result.status == "fail"
    assert "OPERAND_ENTITY_MISMATCH" in result.blocked_reasons


def test_derived_fact_rejects_operand_outside_derived_period() -> None:
    left = _fact(value="10억원", entity="리바로", metric="매출", period="2025-04", unit="억원")
    right = _fact(value="12억원", entity="리바로", metric="매출", period="2026-05", unit="억원")
    derived = EvidenceFact(
        fact_id="fact_growth_wrong_period",
        label="매출 변화율",
        value="20%",
        source="mart",
        tool="get_brand_metric",
        path="derived",
        period="2026-04~2026-05",
        allowed_numbers=("20%",),
        entity="리바로",
        metric="매출 변화율",
        unit="%",
        source_grade=SourceGrade.AUTHORITATIVE.value,
        operand_fact_ids=(left.fact_id, right.fact_id),
    )

    result = verify_claim_bindings(
        question="리바로 매출 변화율은?",
        answer="리바로 매출 변화율은 20%입니다.",
        facts=(left, right, derived),
        expected_entities=("리바로",),
    )

    assert result.status == "fail"
    assert "OPERAND_PERIOD_MISMATCH" in result.blocked_reasons


def test_direct_authoritative_fact_prevents_invalid_duplicate_derived_fact_from_overblocking() -> None:
    direct = _fact(value="20%", entity="리바로", metric="매출 변화율", period="2026-04~2026-05", unit="%")
    derived = EvidenceFact(
        fact_id="fact_growth_invalid_duplicate",
        label="매출 변화율",
        value="20%",
        source="mart",
        tool="get_brand_metric",
        path="derived",
        period="2026-04~2026-05",
        allowed_numbers=("20%",),
        entity="리바로",
        metric="매출 변화율",
        unit="%",
        source_grade=SourceGrade.AUTHORITATIVE.value,
        operand_fact_ids=("missing:left", "missing:right"),
    )

    result = verify_claim_bindings(
        question="리바로 매출 변화율은?",
        answer="리바로 매출 변화율은 20%입니다.",
        facts=(direct, derived),
        expected_entities=("리바로",),
    )

    assert result.status == "pass"


def test_web_presentation_rejects_every_non_upstream_failure_reason() -> None:
    for reason in (
        "NO_MATCH",
        "CAPABILITY_NOT_IMPLEMENTED",
        "INVALID_INPUT",
        "AMBIGUOUS_INPUT",
    ):
        decision = web_presentation_policy(
            "상병코드 D693의 환자수 추이를 알려줘",
            (
                _hira_call(status="error", error_code=reason),
                _web_call(
                    {
                        "title": "HIRA 통계 안내",
                        "url": "https://opendata.hira.or.kr/guide",
                        "snippet": "공식 사이트 안내",
                    }
                ),
            ),
        )

        assert decision.accepted_urls == (), reason
        assert decision.show_all_results is False, reason
        assert decision.reason_code == reason, reason


def test_compute_final_answer_blocks_e11_patient_count_for_h360_question() -> None:
    evidence = _fact(
        value="67890명",
        entity="E11",
        metric="환자수",
        period="2024",
        unit="명",
    )
    result = {
        "general_view_ready": True,
        "answer": "당뇨병성 망막병증의 2024년 환자수는 67890명입니다.",
        "markdown_response": {"evidence": [asdict(evidence)]},
        "sources": ["HIRA"],
        "router_diagnostics": {
            "routing_v4": {
                "proposed_routing_signature": {
                    "proposed_calls": [{"normalized_args": {"sick_cd": "H36.0"}}]
                }
            }
        },
    }

    final = compute_final_answer(
        "당뇨병성 망막병증의 2024년 환자수는?",
        result,
        "claim-binding-integration",
    )

    assert "67890" not in final.text
    assert "ENTITY_MISMATCH" in final.trace["qa_trace"]["claims"]["blocked_reasons"]


def test_final_general_view_contract_cannot_reappend_an_ungrounded_numeric_section(monkeypatch) -> None:
    from jw_chat_agent_poc.service.genos_client import GenosClient

    monkeypatch.setattr(
        GenosClient,
        "stream_answer",
        lambda *_args: iter(("리바로의 2026-05 매출은 80.39억원입니다.",)),
    )
    result = {
        "answer": "",
        "context_scope": "MARKET",
        "markdown_response": {"fact_md": ""},
        "tool_calls": [
            {
                "tool": "get_brand_metric",
                "source": "UBIST",
                "status": "ok",
                "render_data": {
                    "brand": "리바로",
                    "metric": "sales",
                    "period": "2026-05",
                    "sales_억원": 80.39,
                },
            }
        ],
        "general_view_contract": {
            "mode": "dual",
            "atc4_code": "C10A1",
            "source": "UBIST",
            "measure": "sales",
            "period": "2026-05",
            "section_markdown": "## 일반뷰\n\n반드시 반영할 내용: 999.99억원",
        },
        "router_diagnostics": {
            "routing_v4": {
                "proposed_routing_signature": {
                    "proposed_calls": [{"normalized_args": {"brand": "리바로"}}]
                }
            }
        },
        "sources": ["UBIST"],
    }

    final = compute_final_answer("리바로 매출은?", result, "general-contract-binding")

    assert "999.99" not in final.text
    assert "MISSING_EVIDENCE_BINDING" in final.trace["qa_trace"]["claims"]["blocked_reasons"]
