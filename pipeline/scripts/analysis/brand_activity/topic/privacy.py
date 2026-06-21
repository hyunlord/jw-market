"""Privacy helpers for redacted audit artifacts."""

from __future__ import annotations

from .models import JsonValue, MessageRecord
from .text_tokens import language_bucket


def audit_safe_record(row: MessageRecord) -> dict[str, JsonValue]:
    """Serialize a message row without raw source text."""
    return {
        "source": row.source,
        "market": row.market,
        "message_id": row.message_id,
        "period_ym": row.period_ym,
        "product_name": row.product_name,
        "message_hash": row.message_hash,
        "message_len": len(row.message_text),
        "language": language_bucket(row.message_text),
        "frequency": row.frequency,
    }
