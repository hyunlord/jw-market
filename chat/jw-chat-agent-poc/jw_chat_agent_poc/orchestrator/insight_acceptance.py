from __future__ import annotations

from dataclasses import dataclass

from jw_chat_agent_poc.orchestrator.market_insights import forbidden_claims
from jw_chat_agent_poc.orchestrator.provenance import EvidenceFact, verify_markdown_numbers


@dataclass(frozen=True, slots=True)
class InsightAcceptance:
    gate: str
    classification: str
    checked: int
    population: int
    missing: str
    tolerance: str
    failures: tuple[str, ...]
    exit_code: int
    environment: str

    def to_text(self) -> str:
        return "\n".join(
            (
                f"gate={self.gate}",
                f"classification={self.classification}",
                f"checked={self.checked}",
                f"population={self.population}",
                f"missing={self.missing}",
                f"tolerance={self.tolerance}",
                f"failures={len(self.failures)}",
                f"exit_code={self.exit_code}",
                f"environment={self.environment}",
            )
        )


def verify_insight_answer(
    *,
    gate: str,
    markdown: str,
    facts: tuple[EvidenceFact, ...],
    environment: str = "local",
) -> InsightAcceptance:
    numeric = verify_markdown_numbers(markdown, facts)
    failures = [f"unexpected_number:{value}" for value in numeric.unexpected_numbers]
    failures.extend(f"forbidden_claim:{value}" for value in forbidden_claims(markdown))
    population = len(facts)
    if population == 0:
        failures.append("empty_evidence_population")
    return InsightAcceptance(
        gate=gate,
        classification="census",
        checked=population,
        population=population,
        missing="fail",
        tolerance="exact",
        failures=tuple(failures),
        exit_code=1 if failures else 0,
        environment=environment,
    )
