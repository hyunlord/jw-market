"""Content-addressed durable receipts for post-crawl Agent2 selection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Final

from pipeline.scripts.crawler.agent2_hook import (
    Agent2DetectionResult,
)
from pipeline.scripts.crawler.crawl_temporal_contract import RUN_ID_PATTERN


DETECTION_SCHEMA: Final = "agent2-post-crawl-detection/v1"
DETECTION_POINTER_SCHEMA: Final = "agent2-post-crawl-detection-pointer/v1"


def _detection_payload(result: Agent2DetectionResult) -> dict[str, object]:
    return {
        "snapshot_date": result.snapshot_date.isoformat(),
        "eligibility_revision": result.eligibility_revision,
        "selector_revision": result.selector_revision,
        "registry_revision": result.registry_revision,
        "target_count": result.target_count,
        "targets": [
            {
                "brand_key": target.brand_key,
                "canonical_brand_name": target.canonical_brand_name,
                "selected_news_ids": list(target.selected_news_ids),
                "effective_added_news_ids": list(target.effective_added_news_ids),
                "evidence": [
                    {
                        "news_id": item.news_id,
                        "published_date": (
                            item.published_date.isoformat()
                            if item.published_date is not None
                            else None
                        ),
                        "score": item.score,
                        "tag": item.tag,
                        "source_processor": item.source_processor,
                        "derivation": item.derivation,
                    }
                    for item in target.evidence
                ],
            }
            for target in result.targets
        ],
    }


def _canonical_json(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def write_detection_receipt(
    *,
    state_root: Path,
    run_id: str,
    result: Agent2DetectionResult,
) -> dict[str, object]:
    """Persist immutable selection evidence and a per-run pointer."""

    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("invalid jw-agent run_id")
    payload = {"schema": DETECTION_SCHEMA, **_detection_payload(result)}
    encoded = _canonical_json(payload)
    content_sha256 = hashlib.sha256(encoded).hexdigest()
    receipt_path = (
        state_root / "agent2-hook" / "detections" / f"{content_sha256}.json"
    )
    if receipt_path.exists():
        if receipt_path.read_bytes() != encoded:
            raise RuntimeError(f"content-address collision: {receipt_path}")
    else:
        _atomic_write(receipt_path, encoded)

    pointer_path = state_root / "runs" / run_id / "agent2_detection.json"
    pointer: dict[str, object] = {
        "schema": DETECTION_POINTER_SCHEMA,
        "run_id": run_id,
        "content_sha256": content_sha256,
        "receipt_path": str(receipt_path),
        "target_count": result.target_count,
        "receipt_hit": False,
    }
    if pointer_path.exists():
        saved = json.loads(pointer_path.read_text(encoding="utf-8"))
        if saved.get("content_sha256") != content_sha256:
            raise RuntimeError("Agent2 detection changed for an existing run_id")
        return {**saved, "receipt_hit": True}
    _atomic_write(pointer_path, _canonical_json(pointer))
    return pointer


def read_detection_receipt(pointer_path: Path) -> dict[str, object]:
    """Load and hash-verify a detection receipt from its per-run pointer."""

    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    receipt_path = Path(str(pointer["receipt_path"]))
    encoded = receipt_path.read_bytes()
    actual_sha256 = hashlib.sha256(encoded).hexdigest()
    if actual_sha256 != pointer.get("content_sha256"):
        raise RuntimeError("Agent2 detection receipt hash mismatch")
    payload = json.loads(encoded)
    if payload.get("schema") != DETECTION_SCHEMA:
        raise RuntimeError("Agent2 detection receipt schema mismatch")
    return payload
