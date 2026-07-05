from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


FindingSeverity = Literal["flag", "confirm_needed"]


@dataclass(frozen=True, slots=True)
class BqCase:
    id: str
    brand: str
    type: str
    question: str
    cohort: str = ""
    source: str = ""


@dataclass(frozen=True, slots=True)
class BqScreenInput:
    case: BqCase
    status: int | None
    elapsed_s: float | None
    error: str | None
    text: str
    tools: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Finding:
    flag: str
    severity: FindingSeverity
    evidence: str


@dataclass(frozen=True, slots=True)
class ScreenResult:
    case_id: str
    flags: tuple[str, ...]
    confirm_needed: tuple[str, ...]
    findings: tuple[Finding, ...]

    @property
    def has_findings(self) -> bool:
        return bool(self.flags or self.confirm_needed)
