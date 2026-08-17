from __future__ import annotations

from datetime import date

from jw_chat_agent_poc.service.v4.contracts import (
    ClinicalTrialConcept,
    PlannerOutput,
    RequestedAnswerShape,
    ToolQueries,
)
from jw_chat_agent_poc.service.v4.expansion import expand_parameter_axes
from jw_chat_agent_poc.service.v4.planner import _lock_exact_anchor
from jw_chat_agent_poc.service.v4.query_scope import (
    apply_source_call_cap,
    route_queries_by_grain,
)
from jw_chat_agent_poc.service.v4.source_tiers import (
    fan_out_tier_zero_queries,
    sanitize_planner_entities,
)


class _MoleculeReader:
    def brand_molecules(self) -> tuple[dict[str, str], ...]:
        return (
            {
                "brand_key": "HEMLIBRA",
                "brand_name": "헴리브라",
                "atc4_code": "B02B",
                "mart_source": "ubist",
                "molecule_norm": "emicizumab",
                "molecule_display": "Emicizumab",
            },
            {
                "brand_key": "OTHER",
                "brand_name": "다른브랜드",
                "atc4_code": "B02B",
                "mart_source": "ubist",
                "molecule_norm": "other ingredient",
                "molecule_display": "Other Ingredient",
            },
        )


class _ExactResolver:
    def canonicalize_exact(self, value: str) -> str | None:
        return {"앤커버": "엔커버", "헤모리브라": "헴리브라"}.get(value)


def _plan(
    question: str,
    *,
    entities: tuple[str, ...] = (),
    answer_sources: tuple[str, ...] = ("mart",),
    queries: dict[str, tuple[str, ...]] | None = None,
    ingredients: tuple[str, ...] = (),
) -> PlannerOutput:
    values = {source: (question,) for source in (
        "mart",
        "nedrug",
        "hira",
        "openfda",
        "clinicaltrials",
        "web",
        "patent",
    )}
    values.update(queries or {})
    return PlannerOutput(
        resolved_question=question,
        expanded_intents=(question,),
        answer_sources=answer_sources,
        tool_queries=ToolQueries(**values),
        linking_plan="deterministic",
        clinical_query_specs=(
            ClinicalTrialConcept(
                ingredients=ingredients,
                search_area="condition",
                source_queries=(question,),
            ),
        ) if ingredients else (),
        requested_answer_shape=RequestedAnswerShape(
            entities=entities,
            measure_or_attribute=("market_metric",),
        ),
    )


def test_f03_kcd_exact_anchor_only_rewrites_hira_query() -> None:
    question = "23년 상병코드 D69의 환자수 추이"
    plan = _plan(
        question,
        answer_sources=("hira",),
        queries={
            "nedrug": ("혈액질환 국내 허가 정보",),
            "openfda": ("blood disorder safety",),
            "clinicaltrials": ("blood disorder trials",),
            "patent": ("혈액질환 특허",),
        },
    )

    locked = _lock_exact_anchor(question, plan)

    assert locked.tool_queries.hira == ("D69 국내 급여 및 환자 통계",)
    for source in ("nedrug", "openfda", "clinicaltrials", "patent"):
        assert all("D69" not in query for query in getattr(locked.tool_queries, source))


def test_f03_execution_grain_filter_omits_kcd_from_non_hira_lanes() -> None:
    question = "23년 상병코드 D69의 환자수 추이"
    plan = _plan(
        question,
        answer_sources=("hira",),
        queries={
            "nedrug": ("D69 국내 허가 정보",),
            "hira": ("D69 의 환자수 추이 2023년",),
            "openfda": ("D69 미국 허가 및 안전성",),
            "clinicaltrials": ("D69 임상시험 상세",),
            "patent": ("D69 특허 공식 자료",),
        },
    )

    routed, trace = route_queries_by_grain(plan)

    assert routed.tool_queries.hira == ("D69 의 환자수 추이 2023년",)
    for source in ("nedrug", "openfda", "clinicaltrials", "patent"):
        assert getattr(routed.tool_queries, source) == ()
        assert trace["omitted"][source] == [
            {
                "query": getattr(plan.tool_queries, source)[0],
                "reason": "grain_mismatch_kcd",
            }
        ]


def test_f09_f10_planner_only_institution_is_removed_but_user_institution_remains() -> None:
    injected = _plan(
        "리바로젯 급여기준 알려줘",
        entities=("리바로젯", "보건복지부"),
    )
    direct = _plan(
        "보건복지부 고시 알려줘",
        entities=("보건복지부",),
    )

    sanitized, injected_trace = sanitize_planner_entities(
        "리바로젯 급여기준 알려줘", injected
    )
    retained, direct_trace = sanitize_planner_entities("보건복지부 고시 알려줘", direct)

    assert sanitized.requested_answer_shape.entities == ("리바로젯",)
    assert injected_trace["excluded"] == [
        {"entity": "보건복지부", "reason": "planner_only_institution"}
    ]
    assert retained.requested_answer_shape.entities == ("보건복지부",)
    assert direct_trace["excluded"] == []


def test_f11_f12_generic_noun_is_removed_without_damaging_market_phrase() -> None:
    plan = _plan(
        "이상지질혈증 시장 알려줘",
        entities=("제품", "시장", "이상지질혈증 시장"),
    )

    sanitized, trace = sanitize_planner_entities("이상지질혈증 시장 알려줘", plan)

    assert sanitized.requested_answer_shape.entities == ("이상지질혈증 시장",)
    assert trace["excluded"] == [
        {"entity": "제품", "reason": "generic_noun"},
        {"entity": "시장", "reason": "generic_noun"},
    ]


def test_f13_f14_brand_alias_uses_exact_canonicalization_and_preserves_unknown() -> None:
    plan = _plan(
        "JW 제품 매출 상위 10개",
        entities=("앤커버", "헤모리브라", "알수없는브랜드"),
    )

    sanitized, trace = sanitize_planner_entities(
        "JW 제품 매출 상위 10개",
        plan,
        resolver=_ExactResolver(),
    )

    assert sanitized.requested_answer_shape.entities == (
        "엔커버",
        "헴리브라",
        "알수없는브랜드",
    )
    assert trace["canonicalized"] == [
        {"input": "앤커버", "canonical": "엔커버"},
        {"input": "헤모리브라", "canonical": "헴리브라"},
    ]


def test_f15_f18_parenthetical_entity_uses_base_and_keeps_inner_query_only() -> None:
    plan = _plan(
        "JW 제품 매출 상위 10개",
        entities=(
            "리바로 (Pitavastatin)",
            "리바로젯 (Pitavastatin/Ezetimibe)",
            "위너프 (Winnuf)",
        ),
    )

    sanitized, trace = sanitize_planner_entities(plan.resolved_question, plan)

    assert sanitized.requested_answer_shape.entities == (
        "리바로",
        "리바로젯",
        "위너프",
    )
    assert trace["parenthetical_expansion_candidates"] == [
        "Pitavastatin",
        "Pitavastatin/Ezetimibe",
        "Winnuf",
    ]
    assert trace["parenthetical_candidate_scope"] == "query_only"


def test_f16_f17_parenthetical_normalization_does_not_merge_prefix_brands() -> None:
    plan = _plan(
        "브랜드 비교",
        entities=("리바로", "리바로젯", "위너프", "위너프페리"),
    )

    sanitized, trace = sanitize_planner_entities(plan.resolved_question, plan)

    assert sanitized.requested_answer_shape.entities == plan.requested_answer_shape.entities
    assert trace["parenthetical_expansion_candidates"] == []


def test_f01_disease_ingredient_expands_through_exact_mart_dictionary() -> None:
    question = "혈우병 치료제 시장 알려줘"
    plan = _plan(
        question,
        entities=("혈우병",),
        ingredients=("Emicizumab", "Unlisted Molecule"),
    )

    outcome = expand_parameter_axes(
        plan,
        question,
        observed_on=date(2026, 8, 18),
        molecule_reader=_MoleculeReader(),
    )

    expansion = outcome.trace["entity_expansion"]
    assert expansion["status"] == "expanded"
    assert expansion["original_entities"] == ["혈우병"]
    assert expansion["candidates"] == ["Emicizumab", "Unlisted Molecule"]
    assert expansion["validated_molecules"] == ["Emicizumab"]
    assert "헴리브라" in expansion["brands"]
    assert expansion["atc4_codes"] == ["B02B"]
    assert expansion["unvalidated_candidates"] == ["Unlisted Molecule"]
    assert any("헴리브라" in query for query in outcome.plan.tool_queries.mart)
    assert any("Emicizumab" in query for query in outcome.plan.tool_queries.mart)
    assert any("B02B" in query for query in outcome.plan.tool_queries.mart)
    assert all("Unlisted Molecule" not in query for query in outcome.plan.tool_queries.mart)
    assert any("Unlisted Molecule" in query for query in outcome.plan.tool_queries.nedrug)


def test_f01_hemophilia_fixture_expands_when_planner_only_returns_question() -> None:
    question = "혈우병 치료제 시장 알려줘"
    plan = _plan(question, entities=("혈우병",), ingredients=(question,))

    outcome = expand_parameter_axes(
        plan,
        question,
        observed_on=date(2026, 8, 18),
        molecule_reader=_MoleculeReader(),
    )

    expansion = outcome.trace["entity_expansion"]
    assert expansion["validated_molecules"] == ["Emicizumab"]
    assert expansion["brands"] == ["헴리브라", "록타비안", "헴제닉스"]
    assert expansion["atc4_codes"] == ["B02B"]


def test_f05_f06_expansion_entities_are_queries_not_answer_facts() -> None:
    question = "혈우병 치료제 시장 알려줘"
    plan = _plan(question, entities=("혈우병",), ingredients=("Emicizumab",))

    outcome = expand_parameter_axes(
        plan,
        question,
        observed_on=date(2026, 8, 18),
        molecule_reader=_MoleculeReader(),
    )

    assert outcome.plan.requested_answer_shape.entities == ("혈우병",)
    assert outcome.trace["entity_expansion"]["display_scope"] == "query_only"


def test_f04_irrelevant_llm_tail_is_not_multiplied_by_entities() -> None:
    plan = _plan(
        "첨부 제안서가 어떤 제안인지 설명해줘",
        entities=("한국투자증권", "AI PB 서비스"),
        answer_sources=("web",),
        queries={"web": ("의료 개혁 보건의료 체계 지속가능성 제안",)},
    )

    expanded = fan_out_tier_zero_queries(plan)

    assert expanded.tool_queries.web == plan.tool_queries.web


def test_f04_excess_queries_are_capped_and_preserved_as_omitted() -> None:
    queries = tuple(f"무관한 제안 주제 {index}" for index in range(18))
    plan = _plan(
        "첨부 제안서가 어떤 제안인지 설명해줘",
        entities=("한국투자증권", "AI PB 서비스"),
        answer_sources=("web",),
        queries={"web": queries},
    )

    limited = apply_source_call_cap(fan_out_tier_zero_queries(plan))

    assert len(limited.tool_queries.web) == 12
    assert limited.query_scope is not None
    assert limited.query_scope.requested_calls["web"] == 18
    assert limited.query_scope.executed_calls["web"] == 12
    assert limited.query_scope.omitted_queries["web"] == queries[12:]
