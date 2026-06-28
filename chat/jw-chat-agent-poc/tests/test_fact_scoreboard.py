from __future__ import annotations

from scripts.compose_ab_poc.questions import EvalQuestion
from scripts.fact_scoreboard.answer_viz import _question_block
from scripts.fact_scoreboard.scoring import GoldFact, NumericMention, match_mentions
from scripts.fact_scoreboard.text_numbers import extract_numeric_mentions
from scripts.fact_scoreboard.gold import GoldStore, MartRow
from scripts.fact_scoreboard.recalibration import CalibratedQuestion, _add_molecule_segments, _add_sales_delta, choose_molecule_population
from scripts.fact_scoreboard.recal_runner import _is_degraded_answer, _multistep_comparison_ok, compare_observed_facts


def test_extract_numeric_mentions_when_korean_market_answer_contains_units() -> None:
    """Given a Korean answer, When extracting numbers, Then material metric units are retained."""

    # Given
    answer = "로수젯은 시장점유율 9.17%, 매출 206.85억원으로 1위입니다. 2026년 4월 기준입니다."

    # When
    mentions = extract_numeric_mentions(answer)

    # Then
    extracted = {(mention.raw, mention.value, mention.unit) for mention in mentions}
    assert ("9.17%", 9.17, "percent") in extracted
    assert ("206.85억원", 206.85, "eok") in extracted
    assert ("1위", 1.0, "rank") in extracted
    assert all("2026" not in mention.raw for mention in mentions)


def test_match_mentions_when_values_are_rounded() -> None:
    """Given rounded answer values, When matching gold facts, Then tolerance is unit-aware."""

    # Given
    facts = (
        GoldFact("top.ro수젯.share", "로수젯 점유율", 9.1659106, "percent", "Q12", True),
        GoldFact("top.ro수젯.sales", "로수젯 매출", 206.853859, "eok", "Q12", True),
    )
    mentions = (
        NumericMention("9.17%", 9.17, "percent", "로수젯 점유율 9.17%, 매출"),
        NumericMention("206.85억원", 206.85, "eok", "로수젯 점유율 9.17%, 매출 206.85억원"),
    )

    # When
    result = match_mentions(facts, mentions)

    # Then
    assert result.answer_fact_ok is True
    assert result.required_coverage == 1.0
    assert result.unmatched_mentions == ()


def test_extract_numeric_mentions_when_delta_is_negative() -> None:
    """Given a signed delta, When extracting numbers, Then the sign is retained."""

    # Given
    answer = "리바로는 87.11억원에서 84.93억원으로 -2.18억원(-2.50%) 감소했습니다."

    # When
    mentions = extract_numeric_mentions(answer)

    # Then
    extracted = {(mention.raw, mention.value, mention.unit) for mention in mentions}
    assert ("-2.18억원", -2.18, "eok") in extracted
    assert ("-2.50%", -2.5, "percent") in extracted


def test_extract_numeric_mentions_when_rank_is_markdown_bullet() -> None:
    """Given a markdown bullet rank, When extracting numbers, Then the bullet marker is not a negative sign."""

    # Given
    answer = "- 1위 로수젯: 시장점유율 9.17%, 매출 206.85억원"

    # When
    mentions = extract_numeric_mentions(answer)

    # Then
    extracted = {(mention.raw, mention.value, mention.unit) for mention in mentions}
    assert ("1위", 1.0, "rank") in extracted
    assert all(mention.value >= 0 for mention in mentions if mention.unit == "rank")


def test_choose_molecule_population_when_channel_filter_but_whole_market_values_returned() -> None:
    """Given channel intent and whole-market values, When calibrating, Then mismatch is explicit."""

    # Given
    trace_segments = {"A": 60.0, "B": 40.0}
    whole = {"A": 60.0, "B": 40.0}
    channel = {"A": 80.0, "B": 20.0}

    # When
    choice = choose_molecule_population(trace_segments, whole, channel, requested_channel="의원")

    # Then
    assert choice.population == "whole_market"
    assert choice.intent_aligned is False
    assert "requested channel" in choice.note


def test_compare_observed_facts_when_same_fact_id_is_within_rounding_tolerance() -> None:
    """Given calibrated facts, When trace values share ids, Then only true deltas fail."""

    # Given
    gold = (
        GoldFact("Q:brand:share", "share", 33.1919, "percent", "Q", False),
        GoldFact("Q:brand:sales", "sales", 120.094, "eok", "Q", False),
    )
    observed = (
        GoldFact("Q:brand:share", "share", 33.19, "percent", "Q", False),
        GoldFact("Q:brand:sales", "sales", 120.09, "eok", "Q", False),
    )

    # When
    result = compare_observed_facts(gold, observed)

    # Then
    assert result.ok is True
    assert result.match_rate == 1.0
    assert result.mismatches == ()


def test_scoreboard_detects_degraded_fallback_marker() -> None:
    """Given a delayed fallback answer, When scoring quality, Then degraded is flagged."""

    # Given
    answer = "답변 생성이 지연되어 검증된 fact만 최소 정리합니다.\n\n- 시장/브랜드 변화율 대조: ..."

    # When / Then
    assert _is_degraded_answer(answer) is True
    assert _is_degraded_answer("확정 데이터 기준으로 정리하면 다음과 같습니다.") is False


def test_scoreboard_detects_multistep_market_vs_brand_execution() -> None:
    """Given Q04 trace facts, When scoring, Then multistep comparison execution is explicit."""

    # Given
    calibrated = CalibratedQuestion(
        question=EvalQuestion("Q04", "advanced", "q", "market_vs_brand_feb"),
        facts=(),
        trace_facts=(GoldFact("Q04:brand Jan-Feb sales pct change", "brand Jan-Feb sales pct change", -9.58, "percent", "Q04", False),),
        schema_execution_ok=True,
        schema_intent_ok=True,
        population_notes=(),
    )

    # When / Then
    assert _multistep_comparison_ok("market_vs_brand_feb", calibrated) == "Y"
    assert _multistep_comparison_ok("atozet_threat", calibrated) == "N"
    assert _multistep_comparison_ok("livaro_yoy_growth", calibrated) == "NA"


def test_recalibration_adds_molecule_segment_sales_facts() -> None:
    """Given molecule segments, When calibrating, Then sales facts are scoreable too."""

    # Given
    store = GoldStore(
        (
            MartRow("제품A", {"2026-04": {"raw_value": 100_000_000, "ms": 60.0, "rank": 1}}, {}, {}, {}, {"molecule": "PTV"}),
            MartRow("제품B", {"2026-04": {"raw_value": 50_000_000, "ms": 40.0, "rank": 2}}, {}, {}, {}, {"molecule": "OTHER"}),
        )
    )
    facts: list[GoldFact] = []
    trace_facts: list[GoldFact] = []

    # When
    _add_molecule_segments(
        store,
        "M01",
        "2026-04",
        {},
        ({"name": "PTV", "rank": 1, "ms_recent_pct": 66.6667, "value_억원": 1.0},),
        facts,
        trace_facts,
        [],
    )

    # Then
    assert any(fact.fact_id == "M01:molecule:all:PTV:sales" and fact.value == 1.0 for fact in facts)
    assert any(fact.fact_id == "M01:molecule:all:PTV:sales" and fact.value == 1.0 for fact in trace_facts)


def test_recalibration_adds_yoy_growth_delta_facts() -> None:
    """Given a YoY render payload, When calibrating, Then delta and growth are gold facts."""

    # Given
    store = GoldStore(
        (
            MartRow(
                "리바로",
                {
                    "2025-04": {"raw_value": 100_000_000, "ms": 3.0, "rank": 1},
                    "2026-04": {"raw_value": 120_000_000, "ms": 4.0, "rank": 1},
                },
                {},
                {},
                {},
                {},
            ),
        )
    )
    facts: list[GoldFact] = []
    trace_facts: list[GoldFact] = []

    # When
    _add_sales_delta(
        store,
        "Q09",
        {
            "brand": "리바로",
            "metric": "yoy_growth",
            "period": "2025-04→2026-04",
            "sales_delta_억원": 0.2,
            "growth_pct": 20.0,
        },
        facts,
        trace_facts,
    )

    # Then
    assert any(fact.fact_id == "Q09:delta:리바로:2025-04→2026-04:sales" and fact.value == 0.2 for fact in facts)
    assert any(fact.fact_id == "Q09:delta:리바로:2025-04→2026-04:pct" and fact.value == 20.0 for fact in facts)
    assert any(fact.fact_id == "Q09:delta:리바로:2025-04→2026-04:pct" and fact.value == 20.0 for fact in trace_facts)


def test_answer_viz_collapses_query_facts_as_scoring_evidence() -> None:
    """Given trace facts, When rendering HTML, Then facts are collapsed and rounded as scoring evidence."""

    # Given
    detail = {
        "row": {
            "qid": "Q04",
            "question": "리바로 2월 하락이 시장 영향인지 브랜드 고유인지 봐줘",
            "schema_ok": "Y",
            "degraded": "N",
            "multistep_comparison_ok": "Y",
            "query_fact_ok": "Y",
            "answer_fact_ok": "Y",
        },
        "answer_markdown": "리바로 하락은 시장 동반 하락에 가깝습니다.",
        "trace_facts": [
            {
                "fact_id": "Q04:brand:jan_feb:pct",
                "label": "리바로 Jan-Feb sales pct change",
                "value": -9.584321,
                "unit": "percent",
                "required": True,
            }
        ],
    }

    # When
    html = _question_block(detail)

    # Then
    assert "<details" in html
    assert "채점 근거(출처 아님)" in html
    assert "<h3>Query fact</h3>" not in html
    assert "-9.58" in html
    assert "-9.584321" not in html
