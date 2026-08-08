from __future__ import annotations
from dataclasses import dataclass

from jw_chat_agent_poc.agent_loop.question_contracts import (
    AnswerIntent,
    QuestionSpec,
    question_spec_for,
)


@dataclass(frozen=True, slots=True)
class SemanticQuestion:
    intent: AnswerIntent
    targets: tuple[tuple[str, str], ...]
    market_scope: str
    time_mode: str
    analysis_modes: tuple[str, ...]
    source_constraints: tuple[str, ...]
    requested_dimensions: tuple[str, ...]
    anchor_provenance: str
    parser: str
    question_spec: QuestionSpec


def parse_semantic_question(question: str) -> SemanticQuestion:
    spec = question_spec_for(question)
    sources = tuple(
        source
        for token, source in (("ubist", "UBIST"), ("iqvia", "IQVIA_NSA"))
        if token in question.casefold()
    )
    fast_intents = {
        AnswerIntent.SOURCE_DIFFERENCE,
        AnswerIntent.MULTI_SOURCE_SNAPSHOT,
        AnswerIntent.COMPETITION_CHANGE,
        AnswerIntent.NEW_ENTRANT_THREAT,
    }
    return SemanticQuestion(
        intent=spec.intent,
        targets=(),
        market_scope="STRATEGIC",
        time_mode="RECENT_TREND",
        analysis_modes=spec.required_slots,
        source_constraints=sources,
        requested_dimensions=("BRAND",),
        anchor_provenance=spec.anchor_provenance or "UNRESOLVED",
        parser="deterministic_fast_path" if spec.intent in fast_intents else "deterministic_contract",
        question_spec=spec,
    )
