from __future__ import annotations

from dataclasses import dataclass

from scripts.fact_scoreboard.text_numbers import NumericMention, NumericUnit


@dataclass(frozen=True, slots=True)
class GoldFact:
    """One independently calculated numeric fact used as scoring ground truth."""

    fact_id: str
    label: str
    value: float
    unit: NumericUnit
    question_id: str
    required: bool


@dataclass(frozen=True, slots=True)
class FactMatch:
    """A matched answer mention and gold fact."""

    fact_id: str
    mention_raw: str
    mention_value: float
    gold_value: float
    delta: float


@dataclass(frozen=True, slots=True)
class MatchResult:
    """Question-level answer number scoring result."""

    answer_fact_ok: bool
    required_coverage: float
    matched: tuple[FactMatch, ...]
    missing_required: tuple[GoldFact, ...]
    unmatched_mentions: tuple[NumericMention, ...]


def match_mentions(gold_facts: tuple[GoldFact, ...], mentions: tuple[NumericMention, ...]) -> MatchResult:
    """Match extracted answer numbers to gold facts with unit-aware tolerance."""

    matched: list[FactMatch] = []
    covered_fact_ids: set[str] = set()
    unmatched: list[NumericMention] = []
    for mention in mentions:
        fact = _best_fact(gold_facts, mention)
        if fact is None:
            unmatched.append(mention)
            continue
        covered_fact_ids.add(fact.fact_id)
        matched.append(
            FactMatch(
                fact_id=fact.fact_id,
                mention_raw=mention.raw,
                mention_value=mention.value,
                gold_value=fact.value,
                delta=mention.value - fact.value,
            )
        )
    required = tuple(fact for fact in gold_facts if fact.required)
    missing = tuple(fact for fact in required if fact.fact_id not in covered_fact_ids)
    coverage = (len(required) - len(missing)) / len(required) if required else 1.0
    return MatchResult(
        answer_fact_ok=not unmatched and not missing,
        required_coverage=coverage,
        matched=tuple(matched),
        missing_required=missing,
        unmatched_mentions=tuple(unmatched),
    )


def _best_fact(gold_facts: tuple[GoldFact, ...], mention: NumericMention) -> GoldFact | None:
    candidates = [
        fact
        for fact in gold_facts
        if fact.unit == mention.unit and abs(fact.value - mention.value) <= _tolerance(fact)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda fact: abs(fact.value - mention.value))


def _tolerance(fact: GoldFact) -> float:
    match fact.unit:
        case "percent":
            return 0.08
        case "eok":
            return 0.08
        case "rank":
            return 0.01
        case "count":
            return 1.0
        case "plain":
            return max(0.05, abs(fact.value) * 0.001)
        case unreachable:
            raise AssertionError(f"unreachable fact unit: {unreachable}")
