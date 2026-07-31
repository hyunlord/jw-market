from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any


ANSWER_BRANCHES = frozenset(
    {
        "mixed",
        "conversation_fallback",
        "general_view_ready",
        "file_only",
        "typed_terminal",
        "typed_partial",
        "app_deterministic_market",
        "app_deterministic_file",
        "app_generation_request_fallback",
        "genos_single_period_sales",
        "genos_tool_fail_closed",
        "genos_tool_clinical_registry",
        "genos_tool_external",
        "genos_tool_markdown_web_search",
        "genos_tool_markdown_deterministic_market",
        "genos_tool_markdown_llm",
        "genos_tool_markdown_request_fallback",
        "genos_tool_markdown_empty_fallback",
        "genos_tool_verified_fallback",
        "genos_external_relay",
        "genos_concentration",
        "genos_top_n",
        "genos_markdown_web_search",
        "genos_markdown_deterministic_market",
        "genos_markdown_llm",
        "genos_markdown_request_fallback",
        "genos_markdown_empty_fallback",
        "genos_cache",
        "genos_legacy_llm",
    }
)
_METADATA_KEY = "_answer_delivery"


def record_answer_delivery(
    result: MutableMapping[str, Any],
    *,
    answer_branch: str,
    source_notice_attached: bool | None,
) -> None:
    if answer_branch not in ANSWER_BRANCHES:
        raise ValueError(f"unregistered answer branch: {answer_branch}")
    result[_METADATA_KEY] = {
        "answer_branch": answer_branch,
        "source_notice_attached": source_notice_attached,
    }


def record_source_notice_attachment(
    result: MutableMapping[str, Any],
    *,
    attached: bool,
) -> None:
    raw = result.get(_METADATA_KEY)
    if not isinstance(raw, MutableMapping):
        result[_METADATA_KEY] = {
            "answer_branch": None,
            "source_notice_attached": attached,
        }
        return
    raw["source_notice_attached"] = attached


def project_answer_delivery(result: Mapping[str, Any]) -> dict[str, str | bool | None]:
    raw = result.get(_METADATA_KEY)
    items = raw if isinstance(raw, Mapping) else {}
    branch = items.get("answer_branch")
    attached = items.get("source_notice_attached")
    return {
        "answer_branch": branch if isinstance(branch, str) and branch in ANSWER_BRANCHES else None,
        "source_notice_attached": attached if isinstance(attached, bool) else None,
    }
