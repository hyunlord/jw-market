"""R15 STAGE 3/4 — stop spending the retrieval budget on calls that cannot pay."""

from __future__ import annotations

from jw_chat_agent_poc.service.v4.adapters import (
    _INGREDIENT_NAME_ALIASES,
    _ingredient_search_term,
    _matched_ingredient_alias,
    _names_an_ingredient,
)


def test_a_molecule_name_still_launches_the_expansion() -> None:
    assert _names_an_ingredient("피타바스타틴 매출 알려줘") is True
    assert _names_an_ingredient("Pitavastatin sales") is True
    assert _names_an_ingredient("pitavastatin calcium 매출") is True
    assert _ingredient_search_term("피타바스타틴 매출 알려줘") == "피타바스타틴"


def test_a_brand_word_no_longer_launches_the_expansion() -> None:
    # "리바로제트" is a misspelling of the real brand; the resolver rejects it and
    # the old fallback searched products for the molecule behind "리바로".
    assert _matched_ingredient_alias("리바로제트 매출 알려줘") == "리바로"
    assert _names_an_ingredient("리바로제트 매출 알려줘") is False
    assert _names_an_ingredient("리바로 매출 알려줘") is False


def test_a_drug_class_word_no_longer_launches_the_expansion() -> None:
    assert _matched_ingredient_alias("스타틴 시장 매출") == "스타틴"
    assert _names_an_ingredient("스타틴 시장 매출") is False


def test_a_company_name_never_matched_an_ingredient() -> None:
    assert _matched_ingredient_alias("JW중외제약 매출 알려줘") is None
    assert _names_an_ingredient("JW중외제약 매출 알려줘") is False
    assert _ingredient_search_term("JW중외제약 매출 알려줘") is None


def test_the_molecule_subset_is_a_subset_of_the_alias_table() -> None:
    from jw_chat_agent_poc.service.v4.adapters import _INGREDIENT_ALIASES

    assert _INGREDIENT_NAME_ALIASES <= set(_INGREDIENT_ALIASES)


def test_longest_alias_wins_so_the_molecule_is_not_shadowed_by_its_class() -> None:
    # "피타바스타틴" contains "스타틴"; the molecule must win.
    assert _matched_ingredient_alias("피타바스타틴 매출") == "피타바스타틴"


# --- STAGE 4: the year axis --------------------------------------------------


QUESTION = "리바로 최근 3년 매출 알려줘"


def _plan(*, period_from, period_to, mart_queries, answer_sources=("mart",)):
    from jw_chat_agent_poc.service.v4.contracts import (
        PlannerOutput,
        RequestedAnswerShape,
        ToolQueries,
    )

    return PlannerOutput(
        resolved_question=QUESTION,
        expanded_intents=(QUESTION,),
        answer_sources=tuple(answer_sources),
        tool_queries=ToolQueries(
            mart=tuple(mart_queries),
            nedrug=(QUESTION,),
            hira=("리바로 처방 실적",),
            openfda=(QUESTION,),
            clinicaltrials=(QUESTION,),
            web=(QUESTION,),
            patent=(QUESTION,),
        ),
        linking_plan="deterministic",
        requested_answer_shape=RequestedAnswerShape(
            entities=("리바로",),
            measure_or_attribute=("매출액",),
            period_from=period_from,
            period_to=period_to,
        ),
    )


def _expand(plan):
    from datetime import date

    from jw_chat_agent_poc.service.v4.expansion import expand_parameter_axes

    return expand_parameter_axes(plan, QUESTION, observed_on=date(2026, 8, 16))


def test_bounded_period_collapses_the_duplicate_year_variants() -> None:
    plan = _plan(
        period_from="2023-09",
        period_to="2026-08",
        mart_queries=("리바로 최근 3년 매출 알려줘", "피타바스타틴 최근 3년 매출 알려줘"),
    )
    outcome = _expand(plan)
    mart = outcome.plan.tool_queries.mart
    assert len(mart) == 2
    assert not any(query.endswith("년") and query[-5:-1].isdigit() for query in mart)
    # Every original subject survives: nothing valid was dropped.
    assert any("리바로" in query for query in mart)
    assert any("피타바스타틴" in query for query in mart)


def test_f4_failure_injection_without_bounds_the_year_axis_still_expands() -> None:
    """F4 (negative arm): with no period bounds the year still selects the data."""
    plan = _plan(
        period_from=None,
        period_to=None,
        mart_queries=("리바로 최근 3년 매출 알려줘",),
    )
    outcome = _expand(plan)
    mart = outcome.plan.tool_queries.mart
    assert len(mart) > 1
    assert any(query.endswith("2024년") for query in mart)


def test_only_the_mart_lane_is_collapsed() -> None:
    plan = _plan(
        period_from="2023-09",
        period_to="2026-08",
        mart_queries=("리바로 최근 3년 매출 알려줘",),
        answer_sources=("mart", "hira"),
    )
    outcome = _expand(plan)
    assert len(outcome.plan.tool_queries.mart) == 1
    assert len(outcome.plan.tool_queries.hira) > 1


def test_the_year_suffix_provably_changes_no_mart_call_under_bounds() -> None:
    """The premise of the collapse, asserted against the mart call builder."""
    from jw_chat_agent_poc.service.v4 import adapters as v4_adapters

    recorded: list[tuple] = []

    class Layer:
        def market_scope(self, brand):
            return {"source": "UBIST", "render_data": {"period": "2026-06"}}

        def brand_metric(self, brand, metric, period, *, market, history_points):
            recorded.append((brand, metric, period, market, history_points))
            return {"source": "UBIST", "render_data": {}}

        def top_brands(self, *args, **kwargs):
            raise LookupError

        def cause_card_data(self, *args, **kwargs):
            raise LookupError

    def calls_for(query: str):
        recorded.clear()
        v4_adapters._strategic_mart_calls(
            Layer(),
            "리바로",
            query,
            period_from="2023-09",
            period_to="2026-08",
        )
        return list(recorded)

    assert calls_for("리바로 최근 매출 알려줘 2024년") == calls_for(
        "리바로 최근 매출 알려줘 2025년"
    )
    assert calls_for("리바로 최근 매출 알려줘 2024년") == calls_for(
        "리바로 최근 매출 알려줘"
    )
    assert calls_for("리바로 최근 매출 알려줘 2024년")
