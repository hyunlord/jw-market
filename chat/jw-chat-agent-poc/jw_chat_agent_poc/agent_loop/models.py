from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping, Protocol


@dataclass(frozen=True, slots=True)
class ToolCallPlan:
    name: str
    arguments: Mapping[str, str]
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "arguments": dict(self.arguments), "reason": self.reason}


@dataclass(frozen=True, slots=True)
class AgentDecision:
    tool_calls: tuple[ToolCallPlan, ...] = ()
    final_answer: str | None = None


@dataclass(frozen=True, slots=True)
class AgentObservation:
    step: int
    tool_name: str
    arguments: Mapping[str, str]
    status: str
    preview: str
    call: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["arguments"] = dict(self.arguments)
        return payload


@dataclass(frozen=True, slots=True)
class AgentTraceStep:
    step: int
    decision: dict[str, Any]
    observations: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ToolPlanner(Protocol):
    def decide(
        self,
        question: str,
        observations: tuple[AgentObservation, ...],
        schemas: tuple[dict[str, Any], ...],
        allowed_brands: tuple[str, ...] = (),
        allowed_periods: tuple[str, ...] = (),
    ) -> AgentDecision: ...
