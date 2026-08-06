"""Core gates for MI Master definition refresh candidate publication."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Literal, Mapping, Sequence


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SUPPORTED_REFRESH_CACHE_TABLES = ("cache_brands", "cache_market_status")


class ReplacementReferencePolicy:
    APPEND_ONLY = "append_only"
    APPEND_OR_APPROVED_REMOVAL = "append_or_approved_removal"


@dataclass(frozen=True, slots=True)
class MiMasterRefreshCandidate:
    candidate_id: str
    mi_master_sha256: str
    manifest_sha256: str
    allowed_cache_tables: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DefinitionApprovalIdentity:
    candidate_id: str
    mi_master_sha256: str
    manifest_sha256: str
    approver: str


@dataclass(frozen=True, slots=True)
class RemovedIdApproval:
    approved: bool
    removed_ids: tuple[str, ...]
    approver: str
    reason: str


@dataclass(frozen=True, slots=True)
class ReplacementDiff:
    removed_ids: tuple[str, ...]
    added_ids: tuple[str, ...]
    unchanged_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AffectedDefinition:
    market_id: str
    atc4_codes: tuple[str, ...]
    cache_tables: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AffectedScopePlan:
    market_ids: tuple[str, ...]
    cache_tables: tuple[str, ...]
    general_rebuild_atc4: tuple[str, ...]

    @property
    def general_rebuild_count(self) -> int:
        return len(self.general_rebuild_atc4)


@dataclass(frozen=True, slots=True)
class RefreshPublishPlan:
    candidate: MiMasterRefreshCandidate
    candidate_dir: Path
    live_dir: Path
    backup_dir: Path
    journal_path: Path


@dataclass(frozen=True, slots=True)
class RefreshPublishResult:
    live_dir: Path
    backup_dir: Path
    journal_path: Path


def mi_master_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_candidate_seed(path: Path) -> MiMasterRefreshCandidate:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"candidate seed is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("candidate seed must be a JSON object")
    tables = payload.get("allowed_cache_tables")
    if not isinstance(tables, list):
        raise ValueError("candidate seed allowed_cache_tables must be a list")
    return MiMasterRefreshCandidate(
        candidate_id=str(payload.get("candidate_id") or ""),
        mi_master_sha256=str(payload.get("mi_master_sha256") or ""),
        manifest_sha256=str(payload.get("manifest_sha256") or ""),
        allowed_cache_tables=tuple(str(table) for table in tables),
    )


def validate_candidate_seed(candidate: MiMasterRefreshCandidate) -> None:
    if not candidate.candidate_id.strip():
        raise ValueError("candidate_id is required")
    _require_sha256(candidate.mi_master_sha256, "mi_master_sha256")
    _require_sha256(candidate.manifest_sha256, "manifest_sha256")
    unsupported = sorted(set(candidate.allowed_cache_tables) - set(SUPPORTED_REFRESH_CACHE_TABLES))
    if unsupported:
        raise ValueError(f"unsupported cache table for MI Master refresh: {unsupported}")


def validate_definition_approval(
    candidate: MiMasterRefreshCandidate,
    payload: Mapping[str, object],
    *,
    expected: DefinitionApprovalIdentity,
) -> None:
    required: dict[str, object] = {
        "approved": True,
        "candidate_id": candidate.candidate_id,
        "mi_master_sha256": candidate.mi_master_sha256,
        "manifest_sha256": candidate.manifest_sha256,
        "approver": expected.approver,
    }
    if (
        expected.candidate_id != candidate.candidate_id
        or expected.mi_master_sha256 != candidate.mi_master_sha256
        or expected.manifest_sha256 != candidate.manifest_sha256
    ):
        raise ValueError("definition approval identity does not match candidate")
    for field, value in required.items():
        if payload.get(field) != value:
            raise ValueError(f"definition approval {field} does not match")


def validate_manifest_equality(
    candidate_hashes: Mapping[str, str],
    serving_hashes: Mapping[str, str],
) -> None:
    tables = sorted(set(candidate_hashes) | set(serving_hashes))
    mismatched = [
        table
        for table in tables
        if candidate_hashes.get(table) != serving_hashes.get(table)
    ]
    if mismatched:
        raise ValueError(
            "catalog manifest equality failed: " + ", ".join(mismatched)
        )


def build_replacement_diff(
    *,
    reference_ids: Sequence[str],
    candidate_ids: Sequence[str],
) -> ReplacementDiff:
    reference = set(reference_ids)
    candidate = set(candidate_ids)
    return ReplacementDiff(
        removed_ids=tuple(sorted(reference - candidate)),
        added_ids=tuple(sorted(candidate - reference)),
        unchanged_ids=tuple(sorted(reference & candidate)),
    )


def validate_replacement_diff(
    diff: ReplacementDiff,
    *,
    policy: Literal["append_only", "append_or_approved_removal"],
    removed_id_approval: RemovedIdApproval | None,
) -> None:
    if not diff.removed_ids:
        return
    match policy:
        case ReplacementReferencePolicy.APPEND_ONLY:
            raise ValueError("removed IDs are not allowed by append-only policy")
        case ReplacementReferencePolicy.APPEND_OR_APPROVED_REMOVAL:
            if (
                removed_id_approval is None
                or not removed_id_approval.approved
                or tuple(sorted(removed_id_approval.removed_ids)) != diff.removed_ids
                or not removed_id_approval.approver.strip()
                or not removed_id_approval.reason.strip()
            ):
                raise ValueError("removed IDs require approval")
        case unreachable:
            raise ValueError(f"unsupported replacement reference policy: {unreachable}")


def plan_affected_scope(
    *,
    affected_definitions: Sequence[AffectedDefinition],
    existing_general_atc4: Sequence[str],
) -> AffectedScopePlan:
    existing = set(existing_general_atc4)
    market_ids = tuple(sorted({item.market_id for item in affected_definitions}))
    cache_tables = tuple(
        table
        for table in SUPPORTED_REFRESH_CACHE_TABLES
        if any(table in item.cache_tables for item in affected_definitions)
    )
    general_rebuild = tuple(
        sorted(
            {
                code
                for item in affected_definitions
                for code in item.atc4_codes
                if code not in existing
            }
        )
    )
    return AffectedScopePlan(market_ids, cache_tables, general_rebuild)


def atomic_publish_candidate(plan: RefreshPublishPlan) -> RefreshPublishResult:
    validate_candidate_seed(plan.candidate)
    if not plan.candidate_dir.is_dir():
        raise ValueError(f"candidate_dir is not a directory: {plan.candidate_dir}")
    if not plan.live_dir.is_dir():
        raise ValueError(f"live_dir is not a directory: {plan.live_dir}")
    backup_target = plan.backup_dir / plan.candidate.candidate_id
    if backup_target.exists():
        raise ValueError(f"backup already exists: {backup_target}")
    plan.backup_dir.mkdir(parents=True, exist_ok=True)
    plan.journal_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(plan.live_dir, backup_target)
    _append_journal(
        plan.journal_path,
        "backup_created",
        plan.candidate,
        {"backup_dir": str(backup_target)},
    )
    temporary_live = (
        plan.live_dir.parent
        / f".{plan.live_dir.name}.{plan.candidate.candidate_id}.tmp"
    )
    if temporary_live.exists():
        shutil.rmtree(temporary_live)
    shutil.copytree(plan.candidate_dir, temporary_live)
    old_live = plan.live_dir.parent / f".{plan.live_dir.name}.{plan.candidate.candidate_id}.old"
    if old_live.exists():
        shutil.rmtree(old_live)
    os.replace(plan.live_dir, old_live)
    try:
        os.replace(temporary_live, plan.live_dir)
    except OSError:
        os.replace(old_live, plan.live_dir)
        raise
    shutil.rmtree(old_live)
    _append_journal(
        plan.journal_path,
        "candidate_published",
        plan.candidate,
        {"live_dir": str(plan.live_dir)},
    )
    return RefreshPublishResult(plan.live_dir, backup_target, plan.journal_path)


def _append_journal(
    path: Path,
    event: str,
    candidate: MiMasterRefreshCandidate,
    extra: Mapping[str, str],
) -> None:
    payload = {
        "event": event,
        "candidate_id": candidate.candidate_id,
        "mi_master_sha256": candidate.mi_master_sha256,
        "manifest_sha256": candidate.manifest_sha256,
        **dict(extra),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _require_sha256(value: str, field: str) -> None:
    if not SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase sha256 hex digest")
