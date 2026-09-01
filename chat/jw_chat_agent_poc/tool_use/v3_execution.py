from __future__ import annotations

from collections.abc import Sequence

from jw_chat_agent_poc.tool_use.v3_execution_contracts import (
    ClinicalTrialFact,
    ExecutableTool,
    FileCellFact,
    InsightFact,
    MarketMetricFact,
    MarketDefinitionFact,
    RegulatoryRuleFact,
    ToolExecutionRecord,
    ToolDeferredRecord,
    ToolFailureRecord,
    V3EvidenceBundle,
    V3EvidenceFact,
)
from jw_chat_agent_poc.tool_use.v3_execution_conversion import (
    bundle_status,
    convert_execution_facts,
    failure_sort_key,
)
from jw_chat_agent_poc.tool_use.v3_execution_normalization import (
    CanonicalArgumentKey,
    canonical_argument_key,
)
from jw_chat_agent_poc.tool_use.v3_execution_parallel import (
    PreparedCall,
    execute_parallel,
)
from jw_chat_agent_poc.tool_use.v3_execution_tools import (
    external_executable_tools,
    internal_executable_tools,
)
from jw_chat_agent_poc.tool_use.v3_selection import MultiToolChoice


DEFAULT_MAX_WORKERS = 8


class V3ShadowToolExecutor:
    """Execute V3 selections into evidence without rendering an answer."""

    def __init__(
        self,
        *,
        tools: Sequence[ExecutableTool],
        max_workers: int = DEFAULT_MAX_WORKERS,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least one")
        names = [tool.name for tool in tools]
        if len(names) != len(set(names)):
            raise ValueError("executable tool names must be unique")
        if any(tool.timeout_s <= 0 for tool in tools):
            raise ValueError("tool timeouts must be positive")
        self._tools = {tool.name: tool for tool in tools}
        self._max_workers = max_workers

    def execute(self, choices: Sequence[MultiToolChoice]) -> V3EvidenceBundle:
        original_count = len(choices)
        if not choices:
            return V3EvidenceBundle("no_selection", (), (), (), (), 0, 0, 0)

        prepared, dispatch_failures = self._prepare(choices)
        executions, execution_failures = execute_parallel(
            prepared,
            max_workers=self._max_workers,
        )
        facts, conversion_failures, deferred = self._convert(executions)
        failures = tuple(
            sorted(
                (*dispatch_failures, *execution_failures, *conversion_failures),
                key=failure_sort_key,
            )
        )
        return V3EvidenceBundle(
            status=bundle_status(executions, failures),
            facts=facts,
            failures=failures,
            deferred=deferred,
            executions=tuple(executions),
            original_call_count=original_count,
            executed_call_count=len(prepared),
            deduplicated_call_count=(
                original_count - len(prepared) - len(dispatch_failures)
            ),
        )

    def _prepare(
        self,
        choices: Sequence[MultiToolChoice],
    ) -> tuple[list[PreparedCall], list[ToolFailureRecord]]:
        prepared: list[PreparedCall] = []
        failures: list[ToolFailureRecord] = []
        seen: set[CanonicalArgumentKey] = set()
        for index, choice in enumerate(choices):
            arguments = dict(choice.arguments)
            tool = self._tools.get(choice.name)
            if tool is None:
                failures.append(
                    ToolFailureRecord(
                        choice.name,
                        arguments,
                        "dispatch",
                        "UNKNOWN_TOOL",
                        "selected tool has no shadow execution adapter",
                    )
                )
                continue
            key = canonical_argument_key(choice.name, arguments)
            if key in seen:
                continue
            seen.add(key)
            prepared.append(PreparedCall(index, tool, arguments))
        return prepared, failures

    def _convert(
        self,
        executions: Sequence[ToolExecutionRecord],
    ) -> tuple[
        tuple[V3EvidenceFact, ...],
        tuple[ToolFailureRecord, ...],
        tuple[ToolDeferredRecord, ...],
    ]:
        facts: list[V3EvidenceFact] = []
        failures: list[ToolFailureRecord] = []
        deferred: list[ToolDeferredRecord] = []
        for record in executions:
            converted_facts, failure, deferred_record = convert_execution_facts(
                record,
                self._tools[record.tool_name].domain,
            )
            facts.extend(converted_facts)
            if failure is not None:
                failures.append(failure)
            if deferred_record is not None:
                deferred.append(deferred_record)
        return tuple(facts), tuple(failures), tuple(deferred)


def build_default_shadow_executor(question: str) -> V3ShadowToolExecutor:
    from jw_chat_agent_poc.tool_use.v3_execution_factory import (
        build_default_shadow_executor as build,
    )

    return build(question)


__all__ = [
    "ClinicalTrialFact",
    "ExecutableTool",
    "FileCellFact",
    "InsightFact",
    "MarketMetricFact",
    "MarketDefinitionFact",
    "RegulatoryRuleFact",
    "V3ShadowToolExecutor",
    "build_default_shadow_executor",
    "canonical_argument_key",
    "external_executable_tools",
    "internal_executable_tools",
]
