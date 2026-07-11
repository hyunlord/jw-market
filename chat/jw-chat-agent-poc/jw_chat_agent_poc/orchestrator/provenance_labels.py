from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from jw_chat_agent_poc.orchestrator.provenance_calls import provenance_rows_from_calls
from jw_chat_agent_poc.orchestrator.provenance_facts import (
    provenance_row_from_file_context,
    provenance_rows_from_fact_markdown,
)
from jw_chat_agent_poc.orchestrator.provenance_model import (
    ProvenanceRow,
    dedupe_rows,
    render_provenance_table,
    sanitize_internal_provenance_labels,
)


def provenance_fact_markdown(calls: Sequence[Mapping[str, Any]], sources: Sequence[str]) -> str:
    """Build the internal, field-aligned provenance fact used by every renderer."""

    return render_provenance_table("### provenance fact", provenance_rows_from_calls(calls, sources))


def provenance_source_block(
    calls: Sequence[Mapping[str, Any]],
    sources: Sequence[str],
    *,
    file_context: str = "",
) -> str:
    """Render the one public seven-field provenance schema from structured calls."""

    rows = list(provenance_rows_from_calls(calls, sources))
    return _render_source_rows(rows, file_context=file_context)


def provenance_source_block_from_facts(fact_md: str, *, file_context: str = "") -> str:
    """Render the public schema from fact markdown without positional fallback filling."""

    rows = list(provenance_rows_from_fact_markdown(fact_md))
    return _render_source_rows(rows, file_context=file_context)


def _render_source_rows(rows: list[ProvenanceRow], *, file_context: str) -> str:
    file_row = provenance_row_from_file_context(file_context)
    if file_row is not None:
        if rows == [ProvenanceRow()]:
            rows.clear()
        rows.append(file_row)
    return render_provenance_table("## 출처", dedupe_rows(rows))
