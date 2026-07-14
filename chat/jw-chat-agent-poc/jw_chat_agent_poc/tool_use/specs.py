from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from jw_chat_agent_poc.tool_use.contracts import ToolEnvelope


ToolExecutor = Callable[[BaseModel], ToolEnvelope]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    execute: ToolExecutor
    timeout_s: float
    tags: tuple[str, ...]

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_model.model_json_schema(),
                "strict": True,
            },
        }


class BrandInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    brand: str


class IngredientInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ingredient: str


class OpenFdaInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ingredient: str
    evidence_type: Literal["label", "adverse_event"]


class QueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str
    brand: str | None = None
    topic: Literal["general", "news"] = "general"


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ItemSequenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_seq: str


class ClinicalQueryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str


class DiseaseCodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sick_cd: str
    year: str = "2024"


class ProcedureCodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    st5_cd: str
    year: str = "2024"
    std_type: str = "1"
