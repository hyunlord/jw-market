from __future__ import annotations

import hashlib
import json

from .models import KeywordRow


def stable_input_hash(rows: list[KeywordRow], *, prompt_version: str, axis_version: str) -> str:
    """Hash prompt-driving row identity and text hashes for cache/version keys."""
    payload = {
        "prompt_version": prompt_version,
        "axis_version": axis_version,
        "rows": [
            {
                "row_id": row.row_id,
                "atc4": row.atc4,
                "brand": row.brand,
                "stage_row_sha256": row.stage_row_sha256,
                "text_sha256": hashlib.sha256(" ".join(row.keyword_text.split()).encode("utf-8")).hexdigest(),
                "interest": row.interest,
                "prescription_evolution": row.prescription_evolution,
                "promotional_lit": row.promotional_lit,
            }
            for row in rows
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_cache_key(*, task: str, model_key: str, prompt_version: str, input_hash: str) -> str:
    """Build the deterministic cache key for one model/task/input tuple."""
    return f"{task}__{model_key}__{prompt_version}__{input_hash}"
