from __future__ import annotations

import hashlib

from .models import JsonValue, KeywordRow


def text_sha256(text: str) -> str:
    """Hash normalized source text so audit can verify identity without storing content."""
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    """Estimate Korean-heavy input tokens from character length for pre-call planning."""
    return max(1, round(len(text) / 2.2))


def redacted_rows_for_audit(rows: list[KeywordRow]) -> list[dict[str, JsonValue]]:
    """Serialize input rows with text hash and length but without raw message text."""
    return [
        {
            "row_id": row.row_id,
            "period_ym": row.period_ym,
            "atc4": row.atc4,
            "brand": row.brand,
            "text_sha256": text_sha256(row.keyword_text),
            "text_length": len(row.keyword_text),
            "estimated_tokens": estimate_tokens(row.keyword_text),
            "interest": row.interest,
            "prescription_frequency": row.prescription_frequency,
            "prescription_evolution": row.prescription_evolution,
            "promotional_lit": row.promotional_lit,
            "abstract_lit": row.abstract_lit,
            "patient_lit": row.patient_lit,
            "specialty": row.specialty,
            "visit_location": row.visit_location,
            "stage_row_sha256": row.stage_row_sha256,
        }
        for row in rows
    ]
