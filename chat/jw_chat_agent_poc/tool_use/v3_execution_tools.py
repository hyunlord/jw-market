from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol

from pydantic import BaseModel

from jw_chat_agent_poc.tool_use.specs import ToolSpec
from jw_chat_agent_poc.tool_use.v3_execution_contracts import ExecutableTool
from jw_chat_agent_poc.tool_use.v3_execution_conversion import tool_domain
from jw_chat_agent_poc.tools.external.mcp_client import mcp_execution_budget


DEFAULT_INTERNAL_TIMEOUT_S = 20.0


class InternalExecutionRegistry(Protocol):
    def names(self) -> tuple[str, ...]: ...

    def execute(self, name: str, arguments: dict[str, object]) -> object: ...


def external_executable_tools(
    specs: Sequence[ToolSpec],
) -> tuple[ExecutableTool, ...]:
    return tuple(
        ExecutableTool(
            name=spec.name,
            domain=tool_domain(spec.name),
            timeout_s=spec.timeout_s,
            execute=_external_executor(spec),
        )
        for spec in specs
    )


def internal_executable_tools(
    registry: InternalExecutionRegistry,
    *,
    timeout_s: float = DEFAULT_INTERNAL_TIMEOUT_S,
) -> tuple[ExecutableTool, ...]:
    from jw_chat_agent_poc.tool_use.v3_selection import selection_tool_specs

    input_models = {
        spec.name: spec.input_model
        for spec in selection_tool_specs()
    }
    return tuple(
        ExecutableTool(
            name=name,
            domain=tool_domain(name),
            timeout_s=timeout_s,
            execute=_internal_executor(registry, name, input_models[name]),
        )
        for name in registry.names()
    )


def _external_executor(spec: ToolSpec) -> Callable[[dict[str, object]], object]:
    def execute(arguments: dict[str, object]) -> object:
        payload: BaseModel = spec.input_model.model_validate(arguments)
        with mcp_execution_budget(spec.timeout_s):
            return spec.execute(payload)

    return execute


def _internal_executor(
    registry: InternalExecutionRegistry,
    tool_name: str,
    input_model: type[BaseModel],
) -> Callable[[dict[str, object]], object]:
    def execute(arguments: dict[str, object]) -> object:
        payload = input_model.model_validate(arguments)
        validated = payload.model_dump(exclude_unset=True)
        if tool_name.startswith("file."):
            validated = _file_adapter_arguments(validated)
        return registry.execute(tool_name, validated)

    return execute


def _file_adapter_arguments(
    arguments: dict[str, object],
) -> dict[str, object]:
    from jw_chat_agent_poc.service.file_sql_query import SqlFileSource

    raw_sources = arguments.get("sources")
    if not isinstance(raw_sources, Sequence) or isinstance(
        raw_sources,
        (str, bytes, bytearray),
    ):
        return arguments
    sources = tuple(
        SqlFileSource(**source) if isinstance(source, dict) else source
        for source in raw_sources
    )
    return {**arguments, "sources": sources}
