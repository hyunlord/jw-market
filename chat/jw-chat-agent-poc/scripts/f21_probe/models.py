from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TurnSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(min_length=1)
    question: str = Field(min_length=1)


class ScenarioSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    turns: tuple[TurnSpec, ...] = ()
    repetitions: int | None = Field(default=None, ge=1)
    artifact_subdir_template: str = ""
    artifact_name_template: str = "{case_id}"
    skip_reason: str | None = None
    requires: str | None = None

    @model_validator(mode="after")
    def validate_turns_or_skip(self) -> ScenarioSpec:
        if self.skip_reason and self.turns:
            raise ValueError("skipped scenarios must not contain turns")
        if not self.skip_reason and not self.turns:
            raise ValueError("non-skipped scenarios require at least one turn")
        return self


class StageSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    directory: str = Field(min_length=1)
    multiturn_sets: bool = False
    scenarios: tuple[ScenarioSpec, ...] = Field(min_length=1)


class DefaultsSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repetitions: int = Field(default=1, ge=1)


class QuestionSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_version: str = Field(alias="schema")
    defaults: DefaultsSpec = DefaultsSpec()
    stages: tuple[StageSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_schema_and_ids(self) -> QuestionSet:
        if self.schema_version != "chat_f21_question_set_v1":
            raise ValueError(
                f"unsupported question-set schema: {self.schema_version}"
            )
        stage_ids = [stage.id for stage in self.stages]
        if len(stage_ids) != len(set(stage_ids)):
            raise ValueError("stage ids must be unique")
        for stage in self.stages:
            scenario_ids = [scenario.id for scenario in stage.scenarios]
            if len(scenario_ids) != len(set(scenario_ids)):
                raise ValueError(f"scenario ids must be unique within stage {stage.id}")
        return self


@dataclass(frozen=True, slots=True)
class QuestionSetCounts:
    question_answer_pairs: int
    multiturn_sets: int
    skipped_multiturn_sets: int
    stage_question_counts: dict[str, int]


def load_question_set(path: Path) -> QuestionSet:
    return QuestionSet.model_validate_json(path.read_text(encoding="utf-8"))


def question_set_counts(question_set: QuestionSet) -> QuestionSetCounts:
    stage_counts: dict[str, int] = {}
    multiturn_sets = 0
    skipped_multiturn_sets = 0
    for stage in question_set.stages:
        stage_count = 0
        for scenario in stage.scenarios:
            repetitions = scenario.repetitions or question_set.defaults.repetitions
            stage_count += len(scenario.turns) * repetitions
            if stage.multiturn_sets:
                multiturn_sets += repetitions
                if scenario.skip_reason:
                    skipped_multiturn_sets += repetitions
        stage_counts[stage.id] = stage_count
    return QuestionSetCounts(
        question_answer_pairs=sum(stage_counts.values()),
        multiturn_sets=multiturn_sets,
        skipped_multiturn_sets=skipped_multiturn_sets,
        stage_question_counts=stage_counts,
    )


def dump_question_set(question_set: QuestionSet) -> str:
    return json.dumps(
        question_set.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        indent=2,
    )
