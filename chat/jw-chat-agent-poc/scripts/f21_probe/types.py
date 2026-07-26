from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from scripts.f21_probe.models import ScenarioSpec, StageSpec
from scripts.f21_probe.sse import JsonValue


OutputRow: TypeAlias = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class TargetIdentity:
    commit: str
    generation: str
    digest: str


@dataclass(frozen=True, slots=True)
class RunOptions:
    base_url: str
    stream_path: str
    output: Path
    question_set_path: Path
    target: TargetIdentity
    headers: dict[str, str]
    header_sources: dict[str, str]
    question_set_paths: tuple[Path, ...] = ()
    concurrency: int = 1
    interval_seconds: float = 2.0
    request_timeout_seconds: float = 360.0
    cleanup_url: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationPlan:
    order: int
    stage: StageSpec
    scenario: ScenarioSpec
    repetition: int
    session_id: str
