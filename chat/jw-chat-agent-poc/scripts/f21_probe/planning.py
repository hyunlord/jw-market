from __future__ import annotations

from pathlib import Path, PurePosixPath
from uuid import uuid4

from scripts.f21_probe.models import QuestionSet
from scripts.f21_probe.types import ConversationPlan


def conversation_plans(question_set: QuestionSet) -> list[ConversationPlan]:
    plans: list[ConversationPlan] = []
    order = 0
    for stage in question_set.stages:
        for scenario in stage.scenarios:
            repetitions = scenario.repetitions or question_set.defaults.repetitions
            for repetition in range(1, repetitions + 1):
                order += 1
                plans.append(
                    ConversationPlan(
                        order=order,
                        stage=stage,
                        scenario=scenario,
                        repetition=repetition,
                        session_id=(
                            f"chat-f21-{stage.id.lower()}-{scenario.id}-"
                            f"r{repetition}-{uuid4()}"
                        ),
                    )
                )
    return plans


def artifact_path(plan: ConversationPlan, case_id: str, turn: int) -> Path:
    values = _format_values(plan, case_id, turn)
    subdir = plan.scenario.artifact_subdir_template.format_map(values)
    name = plan.scenario.artifact_name_template.format_map(values)
    return _safe_relative(PurePosixPath(plan.stage.directory) / subdir / name)


def scenario_directory(plan: ConversationPlan) -> Path:
    values = _format_values(plan, "", 0)
    subdir = plan.scenario.artifact_subdir_template.format_map(values)
    return _safe_relative(PurePosixPath(plan.stage.directory) / subdir)


def formatted_case_id(value: str, plan: ConversationPlan, turn: int) -> str:
    return value.format(
        scenario=plan.scenario.id,
        repetition=plan.repetition,
        turn=turn,
    )


def _format_values(
    plan: ConversationPlan,
    case_id: str,
    turn: int,
) -> dict[str, str | int]:
    return {
        "scenario": plan.scenario.id,
        "repetition": plan.repetition,
        "turn": turn,
        "case_id": formatted_case_id(case_id, plan, turn),
    }


def _safe_relative(relative: PurePosixPath) -> Path:
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"artifact path escapes output root: {relative}")
    return Path(relative)
