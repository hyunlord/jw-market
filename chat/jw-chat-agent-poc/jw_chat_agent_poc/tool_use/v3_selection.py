from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from jw_chat_agent_poc.tool_use.catalog import TOOL_DESCRIPTION_CATALOG
from jw_chat_agent_poc.tool_use.specs import (
    BrandInput,
    ClinicalQueryInput,
    DiseaseCodeInput,
    IngredientInput,
    ItemSequenceInput,
    NctIdInput,
    OpenFdaInput,
    ProcedureCodeInput,
    QueryInput,
)
from jw_chat_agent_poc.tool_use.v3_intent import IntentFrame, extract_intent_frame


class _SelectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MarketScopeSpecInput(_SelectionInput):
    kind: Literal["strategic", "general_atc4", "general_composite"]
    market_id: str | None = None
    atc4: tuple[str, ...] = ()
    filters: dict[str, tuple[str, ...]] = Field(default_factory=dict)


class _MarketSelectionInput(_SelectionInput):
    view: Literal["strategic", "general"] | None = None
    scope: MarketScopeSpecInput | None = None


class MarketBrandMetricInput(_MarketSelectionInput):
    brand: str
    metric: str
    period: str = "latest"
    market: str | None = None
    source: str = ""
    history_points: int = 10


class MarketScopeInput(_MarketSelectionInput):
    brand: str
    market: str | None = None


class MarketTimeseriesInput(_MarketSelectionInput):
    brand: str
    metric: str = "sales"
    period: str = "latest"
    market: str | None = None
    source: str = ""
    history_points: int = 10


class MarketChannelBreakdownInput(_MarketSelectionInput):
    brand: str
    source: str = ""
    period: str = "latest"
    limit: int = 10
    market: str | None = None
    metric: str = "sales"


class MarketDerivedMetricInput(_MarketSelectionInput):
    brand: str
    period: str = "latest"
    market: str | None = None
    source: str = ""
    history_points: int = 10


class MarketComparisonInput(_MarketSelectionInput):
    brand: str
    comparison_brand: str
    market: str | None = None
    metric: str = "series"


class FileSourceInput(_SelectionInput):
    logical_name: str
    file_name: str
    sheet_name: str
    document_id: int | None = None
    row_count: int | None = None
    column_count: int | None = None


class FileSchemaInput(_SelectionInput):
    conversation_id: str
    sources: tuple[FileSourceInput, ...] = Field(min_length=1)


class FileQueryInput(FileSchemaInput):
    question: str


@dataclass(frozen=True, slots=True)
class SelectionToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    domain: str

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


@dataclass(frozen=True, slots=True)
class MultiToolChoice:
    name: str
    arguments: dict[str, Any]
    call_id: str | None = None


class MultiToolChoiceProvider(Protocol):
    def choose_many(
        self,
        *,
        user_text: str,
        messages: list[dict],
        tools: list[dict],
    ) -> tuple[MultiToolChoice, ...]: ...


@dataclass(frozen=True, slots=True)
class V3SelectionResult:
    intent: IntentFrame
    candidate_names: tuple[str, ...]
    choices: tuple[MultiToolChoice, ...]
    unknown_tool_names: tuple[str, ...]
    provider_choice_count: int


_EXTERNAL_INPUT_MODELS: dict[str, type[BaseModel]] = {
    "local_molecule_lookup": BrandInput,
    "get_drug_main_ingredient": BrandInput,
    "openfda_label_search": OpenFdaInput,
    "web_search": QueryInput,
    "mfds_permission_search": BrandInput,
    "mfds_permission_detail": ItemSequenceInput,
    "mfds_composition": BrandInput,
    "mfds_easy_drug": BrandInput,
    "mfds_clinical_trial_kr": ClinicalQueryInput,
    "clinicaltrials_v2_search": ClinicalQueryInput,
    "clinicaltrials_study_details": NctIdInput,
    "mfds_patent": IngredientInput,
    "mfds_fda_orangebook": IngredientInput,
    "hira_disease_name_code": DiseaseCodeInput,
    "hira_disease_hospitalization_outpatient_stats": DiseaseCodeInput,
    "hira_disease_gender_age_stats": DiseaseCodeInput,
    "hira_disease_institution_class_stats": DiseaseCodeInput,
    "hira_disease_area_stats": DiseaseCodeInput,
    "hira_reimbursement_criteria": BrandInput,
    "hira_procedure_gender_ipat_opat_stats": ProcedureCodeInput,
    "hira_procedure_gender_age_stats": ProcedureCodeInput,
    "hira_procedure_institution_class_stats": ProcedureCodeInput,
    "hira_procedure_area_stats": ProcedureCodeInput,
}
_INTERNAL_INPUT_MODELS: dict[str, type[BaseModel]] = {
    "market.get_brand_metric": MarketBrandMetricInput,
    "market.get_market_size": MarketScopeInput,
    "market.get_market_members": MarketScopeInput,
    "market.get_timeseries": MarketTimeseriesInput,
    "market.get_channel_breakdown": MarketChannelBreakdownInput,
    "market.get_hhi": MarketDerivedMetricInput,
    "market.get_growth_contribution": MarketDerivedMetricInput,
    "market.compare_brands": MarketComparisonInput,
    "file.get_schema": FileSchemaInput,
    "file.query": FileQueryInput,
}


def selection_tool_specs() -> tuple[SelectionToolSpec, ...]:
    input_models = {**_EXTERNAL_INPUT_MODELS, **_INTERNAL_INPUT_MODELS}
    return tuple(
        SelectionToolSpec(
            name=record.name,
            description=record.catalog_description,
            input_model=input_models[record.name],
            domain=_tool_domain(record.name),
        )
        for record in TOOL_DESCRIPTION_CATALOG
    )


class V3ToolSelector:
    def __init__(
        self,
        *,
        provider: MultiToolChoiceProvider,
        max_calls: int = 8,
        tools: tuple[SelectionToolSpec, ...] | None = None,
    ) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be at least one")
        self._provider = provider
        self._max_calls = max_calls
        self._tools = tools or selection_tool_specs()

    def select(self, question: str) -> V3SelectionResult:
        intent = extract_intent_frame(question)
        candidates = _prioritized(self._tools, intent)
        messages = [
            {
                "role": "system",
                "content": (
                    "Select zero or more tools that together address the request. "
                    f"Return no more than {self._max_calls} tool calls. "
                    "Tools may be selected in parallel. Do not invent tool names or "
                    "arguments that are not grounded in the request. IntentFrame only "
                    "prioritizes tools; every catalog tool remains eligible."
                ),
            },
            {
                "role": "system",
                "content": f"IntentFrame: {intent.model_dump_json()}",
            },
            {"role": "user", "content": question},
        ]
        proposed = tuple(
            self._provider.choose_many(
                user_text=question,
                messages=messages,
                tools=[tool.openai_schema() for tool in candidates],
            )
        )
        choices = proposed[: self._max_calls]
        known_names = {tool.name for tool in candidates}
        unknown = tuple(choice.name for choice in choices if choice.name not in known_names)
        return V3SelectionResult(
            intent=intent,
            candidate_names=tuple(tool.name for tool in candidates),
            choices=choices,
            unknown_tool_names=unknown,
            provider_choice_count=len(proposed),
        )


def _prioritized(
    tools: tuple[SelectionToolSpec, ...],
    intent: IntentFrame,
) -> tuple[SelectionToolSpec, ...]:
    priorities = {domain: index for index, domain in enumerate(intent.domains)}
    indexed = tuple(enumerate(tools))
    return tuple(
        tool
        for _, tool in sorted(
            indexed,
            key=lambda item: (
                priorities.get(item[1].domain, len(priorities)),
                item[0],
            ),
        )
    )


def _tool_domain(name: str) -> str:
    if name.startswith("market."):
        return "market"
    if name.startswith("file."):
        return "file"
    if name.startswith("clinicaltrials_") or name == "mfds_clinical_trial_kr":
        return "clinical"
    if name.startswith(("mfds_", "hira_", "openfda_")):
        return "regulatory"
    if name in {"local_molecule_lookup", "get_drug_main_ingredient"}:
        return "regulatory"
    return "general"
