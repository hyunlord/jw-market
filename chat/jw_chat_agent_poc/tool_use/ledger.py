from __future__ import annotations

from dataclasses import dataclass, field

from jw_chat_agent_poc.tool_use.contracts import EvidenceFact, ToolEnvelope


@dataclass(slots=True)
class EvidenceLedger:
    """Mutable, request-scoped accumulator for verified public evidence."""

    facts: list[EvidenceFact] = field(default_factory=list)
    envelopes: list[ToolEnvelope] = field(default_factory=list)

    def add(self, envelope: ToolEnvelope) -> None:
        self.envelopes.append(envelope)
        if not envelope.ok:
            return
        for fact in envelope.evidence:
            if _is_public_fact(fact):
                self.facts.append(fact)

    def is_complete(self) -> bool:
        return bool(self.facts)

    def sources(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(fact.source_name for fact in self.facts))


def _is_public_fact(fact: EvidenceFact) -> bool:
    forbidden = ("temp_document", "document_id", "cache_", "table", "query_id")
    text = " ".join(
        value
        for value in (
            fact.fact_id,
            fact.subject,
            fact.metric,
            fact.source_name,
            fact.source_locator or "",
            fact.raw_ref or "",
        )
        if value
    ).casefold()
    return not any(token in text for token in forbidden)
