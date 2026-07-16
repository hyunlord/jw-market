from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from jw_chat_agent_poc.orchestrator.markdown_renderers import call_data_md
from jw_chat_agent_poc.orchestrator.provenance import number_tokens


PUBLIC_EVIDENCE_STATUSES: Final[frozenset[str]] = frozenset({"live", "ok", "partial", "success"})


def ungrounded_numbers(
    answer: str,
    markdown_response: Mapping[str, Any],
    tool_calls: Sequence[Mapping[str, Any]] = (),
) -> tuple[str, ...]:
    allowed = markdown_response.get("allowed_numbers")
    allowed_set = {str(item) for item in allowed} if isinstance(allowed, (list, tuple)) else set()
    allowed_set.update(number_tokens(_markdown_field(markdown_response, "fact_md")))
    allowed_set.update(number_tokens(_markdown_field(markdown_response, "data_md")))
    for call in tool_calls:
        if call.get("status") not in PUBLIC_EVIDENCE_STATUSES:
            continue
        allowed_set.update(number_tokens(call_data_md(dict(call))))
    return tuple(sorted(token for token in number_tokens(answer) if token not in allowed_set))


def _markdown_field(markdown_response: Mapping[str, Any], field: str) -> str:
    value = markdown_response.get(field)
    return value if isinstance(value, str) else ""
