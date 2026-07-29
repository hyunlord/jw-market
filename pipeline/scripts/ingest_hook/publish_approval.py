"""Fail-closed approval gate immediately before serving publication."""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ENV_REQUIRE_EXACT_APPROVAL = "INGEST_REQUIRE_EXACT_PUBLISH_APPROVAL"
ENV_APPROVAL_FILE = "INGEST_PUBLISH_APPROVAL_FILE"


class PublishApprovalError(RuntimeError):
    """The publish approval contract is configured incorrectly or mismatched."""


@dataclass(frozen=True)
class PublishApprovalIdentity:
    epoch: str
    category: str
    manifest_sha: str
    run_id: str


def _validate_payload(payload: object, identity: PublishApprovalIdentity) -> None:
    if not isinstance(payload, dict):
        raise PublishApprovalError("approval payload must be a JSON object")
    expected: dict[str, object] = {
        "approved": True,
        "epoch": identity.epoch,
        "category": identity.category,
        "manifest_sha": identity.manifest_sha,
        "run_id": identity.run_id,
    }
    for field, value in expected.items():
        if payload.get(field) != value:
            raise PublishApprovalError(
                f"publish approval {field} does not match the exact run"
            )


def wait_for_exact_publish_approval(
    identity: PublishApprovalIdentity,
    *,
    poll_seconds: float = 5.0,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Wait for an exact-run approval file when the opt-in gate is enabled."""
    if os.environ.get(ENV_REQUIRE_EXACT_APPROVAL, "").strip() != "1":
        return
    raw_path = os.environ.get(ENV_APPROVAL_FILE, "").strip()
    if not raw_path:
        raise PublishApprovalError(
            f"{ENV_APPROVAL_FILE} is required when {ENV_REQUIRE_EXACT_APPROVAL}=1"
        )
    approval_path = Path(raw_path)
    print(
        "phase=mart_publish status=waiting_for_pl_approval "
        f"approval_file={approval_path}",
        flush=True,
    )
    while not approval_path.is_file():
        sleeper(poll_seconds)
    try:
        payload = json.loads(approval_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublishApprovalError(
            f"publish approval file is not valid UTF-8 JSON: {exc}"
        ) from exc
    _validate_payload(payload, identity)
    print(
        "phase=mart_publish status=approval_verified "
        f"manifest_sha={identity.manifest_sha} run_id={identity.run_id}",
        flush=True,
    )
