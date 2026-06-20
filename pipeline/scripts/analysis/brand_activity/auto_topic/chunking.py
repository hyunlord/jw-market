from __future__ import annotations

from collections.abc import Sequence

from .models import JsonValue, KeywordRow
from .privacy import estimate_tokens


def chunk_rows_by_token_budget(rows: Sequence[KeywordRow], *, token_budget: int) -> list[list[KeywordRow]]:
    """Split rows into deterministic batches whose estimated text tokens stay bounded."""
    if token_budget <= 0:
        return [list(rows)] if rows else []
    chunks: list[list[KeywordRow]] = []
    current: list[KeywordRow] = []
    current_tokens = 0
    for row in rows:
        row_tokens = estimate_tokens(row.keyword_text)
        if current and current_tokens + row_tokens > token_budget:
            chunks.append(current)
            current = []
            current_tokens = 0
        current.append(row)
        current_tokens += row_tokens
    if current:
        chunks.append(current)
    return chunks


def chunk_summary(chunks: Sequence[Sequence[KeywordRow]], *, token_budget: int) -> dict[str, JsonValue]:
    """Summarize chunk counts without exposing source text."""
    return {
        "chunk_count": len(chunks),
        "token_budget": token_budget,
        "row_counts": [len(chunk) for chunk in chunks],
        "estimated_input_tokens": [sum(estimate_tokens(row.keyword_text) for row in chunk) for chunk in chunks],
    }
