from __future__ import annotations

import hashlib
import json

from .models import JsonValue, KeywordRow
from .privacy import text_sha256


def stable_input_hash(rows: list[KeywordRow], *, prompt_version: str, axis_version: str, extra: JsonValue = None) -> str:
    """Hash prompt-driving row identity, text hashes, metadata, and axis version."""
    payload: dict[str, JsonValue] = {
        "prompt_version": prompt_version,
        "axis_version": axis_version,
        "extra": extra,
        "rows": [
            {
                "row_id": row.row_id,
                "period_ym": row.period_ym,
                "atc4": row.atc4,
                "brand": row.brand,
                "text_sha256": text_sha256(row.keyword_text),
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
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_cache_key(*, task: str, model_key: str, serving_id: str, prompt_version: str, axis_version: str, input_hash: str) -> str:
    """Create the deterministic cache key for a task/model/prompt/input tuple."""
    return f"{task}__{model_key}__serving-{serving_id}__{prompt_version}__{axis_version}__{input_hash}"
