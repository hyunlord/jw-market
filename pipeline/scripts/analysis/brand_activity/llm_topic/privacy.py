from __future__ import annotations

import hashlib
from collections.abc import Sequence

from .models import KeywordRow, RedactedAuditRow


def text_sha256(text: str) -> str:
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def estimate_tokens(text: str) -> int:
    return max(1, round(len(text) / 2.2))


def redacted_rows_for_audit(rows: Sequence[KeywordRow]) -> list[RedactedAuditRow]:
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
