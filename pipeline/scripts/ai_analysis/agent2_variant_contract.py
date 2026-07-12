from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping


STAGES = ("phenomenon", "cause", "prediction", "recommendation")
VARIANTS = ("short", "long")
GENERATION_STATUSES = {"complete", "legacy_unbound", "invalid", "retired"}


class VariantContractError(ValueError):
    pass


@dataclass(frozen=True)
class VariantLineage:
    workflow_id: int | None
    workflow_revision_id: int | None
    generation_id: str | None
    input_hash: str | None
    generated_at: datetime | None
    source_epoch: str | None
    generation_status: str
    deterministic: bool = False

    def __post_init__(self) -> None:
        if self.generation_status not in GENERATION_STATUSES:
            raise VariantContractError(f"unsupported generation status: {self.generation_status}")
        if self.generation_status == "complete":
            required = {
                "generation_id": self.generation_id,
                "input_hash": self.input_hash,
                "generated_at": self.generated_at,
                "source_epoch": self.source_epoch,
            }
            if not self.deterministic:
                required.update(
                    workflow_id=self.workflow_id,
                    workflow_revision_id=self.workflow_revision_id,
                )
            missing = [name for name, value in required.items() if value in (None, "")]
            if missing:
                raise VariantContractError(f"complete lineage is missing: {', '.join(missing)}")


def validate_variant_payload(payload: Mapping[str, Any], expected_variant: str) -> None:
    """Fail closed on the four persisted narrative stages.

    Top-level metadata remains extensible, but every persisted stage must have
    the exact structural content consumed by the serving cache.
    """

    if expected_variant not in VARIANTS:
        raise VariantContractError(f"unsupported variant: {expected_variant}")
    if payload.get("analysis_variant") != expected_variant:
        raise VariantContractError("analysis_variant does not match the target column")
    for stage in STAGES:
        value = payload.get(stage)
        if not isinstance(value, Mapping):
            raise VariantContractError(f"{stage} must be an object")
        for field in ("title", "body"):
            text = value.get(field)
            if not isinstance(text, str) or not text.strip():
                raise VariantContractError(f"{stage}.{field} must be a non-empty string")
        bullets = value.get("bullets")
        if not isinstance(bullets, list) or not all(isinstance(item, str) for item in bullets):
            raise VariantContractError(f"{stage}.bullets must be a string array")


def parse_legacy_lineage(payload_text: str | None) -> VariantLineage | None:
    """Recover only facts present in an existing payload; never synthesize lineage."""

    if not payload_text:
        return None
    try:
        payload = json.loads(payload_text)
    except (TypeError, json.JSONDecodeError):
        return VariantLineage(None, None, None, None, None, None, "invalid")
    generated_at = None
    raw_generated_at = payload.get("generated_at")
    if isinstance(raw_generated_at, str):
        try:
            generated_at = datetime.fromisoformat(raw_generated_at.replace("Z", "+00:00"))
        except ValueError:
            pass
    run_id = payload.get("run_id_phase_zeta")
    generation_id = f"zeta-run-{run_id}" if isinstance(run_id, int) else None
    return VariantLineage(
        workflow_id=None,
        workflow_revision_id=None,
        generation_id=generation_id,
        input_hash=None,
        generated_at=generated_at,
        source_epoch=None,
        generation_status="legacy_unbound",
    )
