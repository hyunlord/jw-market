from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any, Final

from jw_chat_agent_poc.orchestrator.markdown_formatting import precise_eok_value
from jw_chat_agent_poc.orchestrator.markdown_renderers import call_data_md
from jw_chat_agent_poc.orchestrator.provenance import number_tokens
from jw_chat_agent_poc.service.markdown_cleanup import cleanup_markdown_answer
from jw_chat_agent_poc.service.web_mi_summary import web_search_mi_section_from_calls


PUBLIC_EVIDENCE_STATUSES: Final[frozenset[str]] = frozenset({"live", "ok", "partial", "success"})
BARE_URL_RE: Final[re.Pattern[str]] = re.compile(r"https?://[^\s|)>]+")


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
        allowed_set.update(number_tokens(BARE_URL_RE.sub("", call_data_md(dict(call)))))
        render_data = call.get("render_data")
        if (
            call.get("tool") == "get_brand_metric"
            and isinstance(render_data, Mapping)
            and render_data.get("metric") == "sales"
            and str(render_data.get("status") or "ok").lower() in PUBLIC_EVIDENCE_STATUSES
        ):
            allowed_set.update(
                number_tokens(
                    precise_eok_value(
                        render_data.get("sales_억원"),
                        render_data.get("sales_krw"),
                    )
                )
            )
    claim_text = BARE_URL_RE.sub("", _without_deterministic_web_appendix(answer, tool_calls))
    return tuple(sorted(token for token in number_tokens(claim_text) if token not in allowed_set))


def _markdown_field(markdown_response: Mapping[str, Any], field: str) -> str:
    value = markdown_response.get(field)
    return value if isinstance(value, str) else ""


def _without_deterministic_web_appendix(
    answer: str,
    tool_calls: Sequence[Mapping[str, Any]],
) -> str:
    section = web_search_mi_section_from_calls(tool_calls)
    if not section:
        return answer
    stripped_answer = answer.rstrip()
    for appendix in (section, cleanup_markdown_answer(section)):
        if stripped_answer.endswith(appendix):
            return stripped_answer[: -len(appendix)].rstrip()
    return answer
