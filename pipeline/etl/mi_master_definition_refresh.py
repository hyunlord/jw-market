"""Core gates for MI Master definition refresh candidate publication."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Literal, Mapping, Sequence


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SUPPORTED_REFRESH_CACHE_TABLES = ("cache_brands", "cache_market_status")
LIVE_CATALOG_TABLES = (
    "catalog_ml_market",
    "catalog_cd_market",
    "catalog_strategic_brand",
)
STRATEGIC_REFRESH_TABLES = (
    "mart_strategic_ml_brand_metric",
    "mart_strategic_ml_market_metric",
    "mart_strategic_cd_brand_metric",
    "mart_strategic_cd_market_metric",
)


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
    mi_master_sha256: str
    catalog_diff_hash: str
    run_id: str

    def as_dict(self) -> dict[str, str]:
        return {
            "mi_master_sha256": self.mi_master_sha256,
            "catalog_diff_hash": self.catalog_diff_hash,
            "run_id": self.run_id,
        }


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
class ReplacementTableParity:
    table_name: str
    row_count_before: int
    row_count_after: int
    row_count_expected: int
    removed_ids: tuple[str, ...]
    added_ids: tuple[str, ...]
    changed_ids: tuple[str, ...]
    before_parquet_sha256: str
    after_parquet_sha256: str


@dataclass(frozen=True, slots=True)
class AffectedDefinition:
    market_id: str
    atc4_codes: tuple[str, ...]
    cache_tables: tuple[str, ...]
    cd_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AffectedScopePlan:
    market_ids: tuple[str, ...]
    cache_tables: tuple[str, ...]
    general_rebuild_atc4: tuple[str, ...]
    affected_ml_ids: tuple[str, ...] = ()
    affected_cd_ids: tuple[str, ...] = ()
    unchanged_ml_ids: tuple[str, ...] = ()
    unchanged_cd_ids: tuple[str, ...] = ()

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
    corpus: RefreshCorpus | None = None
    approval_identity: DefinitionApprovalIdentity | None = None


@dataclass(frozen=True, slots=True)
class RefreshPublishResult:
    live_dir: Path
    backup_dir: Path
    journal_path: Path


@dataclass(frozen=True, slots=True)
class CatalogTableSnapshot:
    table_name: str
    row_count: int
    sha256: str


@dataclass(frozen=True, slots=True)
class CandidateSeedContract:
    live_catalog: tuple[CatalogTableSnapshot, ...]
    strategic_tables: tuple[CatalogTableSnapshot, ...]


@dataclass(frozen=True, slots=True)
class ReferenceReport:
    mart_references: Mapping[str, tuple[str, ...]]
    cache_references: Mapping[str, tuple[str, ...]]
    saved_filter_references: Mapping[str, tuple[str, ...]]
    inactive_decisions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StrategicMarketValidationInput:
    unchanged_market_hash_before: Mapping[str, str]
    unchanged_market_hash_after: Mapping[str, str]
    ml_members: Mapping[str, tuple[str, ...]]
    cd_members: Mapping[str, tuple[str, ...]]
    cd_parent_ml: Mapping[str, str]
    sigma_before: Mapping[str, int]
    sigma_after: Mapping[str, int]

    def with_overrides(
        self,
        **changes: Mapping[str, str] | Mapping[str, int] | Mapping[str, tuple[str, ...]],
    ) -> "StrategicMarketValidationInput":
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class RefreshCorpus:
    candidate_dir: Path
    backup_dir: Path


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
    if expected.mi_master_sha256 != candidate.mi_master_sha256:
        raise ValueError("definition approval identity does not match candidate")
    required: dict[str, object] = {"approved": True, **expected.as_dict()}
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


def build_catalog_diff_hash(
    prior_by_table: Mapping[str, Mapping[str, str]],
    new_by_table: Mapping[str, Mapping[str, str]],
) -> str:
    parity = build_replacement_parity(
        before_by_table=prior_by_table,
        after_by_table=new_by_table,
        before_parquet_hashes={table: "" for table in prior_by_table},
        after_parquet_hashes={table: "" for table in new_by_table},
        expected_after_counts={
            table: len(rows)
            for table, rows in new_by_table.items()
        },
    )
    payload = [
        {
            "table_name": row.table_name,
            "row_count_before": row.row_count_before,
            "row_count_after": row.row_count_after,
            "removed_ids": row.removed_ids,
            "added_ids": row.added_ids,
            "changed_ids": row.changed_ids,
        }
        for row in parity
    ]
    return _sha256_json(payload)


def build_replacement_parity(
    *,
    before_by_table: Mapping[str, Mapping[str, str]],
    after_by_table: Mapping[str, Mapping[str, str]],
    before_parquet_hashes: Mapping[str, str],
    after_parquet_hashes: Mapping[str, str],
    expected_after_counts: Mapping[str, int],
) -> tuple[ReplacementTableParity, ...]:
    rows: list[ReplacementTableParity] = []
    for table in sorted(set(before_by_table) | set(after_by_table)):
        before = dict(before_by_table.get(table, {}))
        after = dict(after_by_table.get(table, {}))
        before_ids = set(before)
        after_ids = set(after)
        shared = before_ids & after_ids
        rows.append(
            ReplacementTableParity(
                table_name=table,
                row_count_before=len(before),
                row_count_after=len(after),
                row_count_expected=int(expected_after_counts.get(table, len(after))),
                removed_ids=tuple(sorted(before_ids - after_ids)),
                added_ids=tuple(sorted(after_ids - before_ids)),
                changed_ids=tuple(
                    sorted(key for key in shared if before[key] != after[key])
                ),
                before_parquet_sha256=str(before_parquet_hashes.get(table, "")),
                after_parquet_sha256=str(after_parquet_hashes.get(table, "")),
            )
        )
    return tuple(rows)


def validate_replacement_parity(
    parity: Sequence[ReplacementTableParity],
) -> None:
    errors: list[str] = []
    for row in parity:
        if row.row_count_after != row.row_count_expected:
            errors.append(
                f"{row.table_name} row_count_after={row.row_count_after} "
                f"expected={row.row_count_expected}"
            )
        if row.changed_ids:
            errors.append(f"{row.table_name} changed_ids={row.changed_ids}")
        for field, value in (
            ("before_parquet_sha256", row.before_parquet_sha256),
            ("after_parquet_sha256", row.after_parquet_sha256),
        ):
            if value and not SHA256_RE.fullmatch(value):
                errors.append(f"{row.table_name} invalid {field}")
    if errors:
        raise ValueError("replacement parity mismatch: " + "; ".join(errors))


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


def validate_removed_id_references(
    removed_ids: Sequence[str],
    report: ReferenceReport,
) -> None:
    inactive = set(report.inactive_decisions)
    blocked: list[str] = []
    for removed_id in sorted(set(removed_ids)):
        references = (
            tuple(report.mart_references.get(removed_id, ()))
            + tuple(report.cache_references.get(removed_id, ()))
            + tuple(report.saved_filter_references.get(removed_id, ()))
        )
        if references and removed_id not in inactive:
            blocked.append(f"{removed_id}: {references}")
    if blocked:
        raise ValueError(
            "referenced removals require inactive decision: " + "; ".join(blocked)
        )


def plan_affected_scope(
    *,
    affected_definitions: Sequence[AffectedDefinition],
    existing_general_atc4: Sequence[str],
    all_ml_ids: Sequence[str] = (),
    all_cd_ids: Sequence[str] = (),
) -> AffectedScopePlan:
    existing = set(existing_general_atc4)
    affected_ml_ids = tuple(sorted({item.market_id for item in affected_definitions}))
    affected_cd_ids = tuple(
        sorted({cd_id for item in affected_definitions for cd_id in item.cd_ids})
    )
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
    unchanged_ml_ids = tuple(sorted(set(all_ml_ids) - set(affected_ml_ids)))
    unchanged_cd_ids = tuple(sorted(set(all_cd_ids) - set(affected_cd_ids)))
    return AffectedScopePlan(
        affected_ml_ids,
        cache_tables,
        general_rebuild,
        affected_ml_ids=affected_ml_ids,
        affected_cd_ids=affected_cd_ids,
        unchanged_ml_ids=unchanged_ml_ids,
        unchanged_cd_ids=unchanged_cd_ids,
    )


def validate_strategic_market_refresh(
    payload: StrategicMarketValidationInput,
) -> None:
    changed_unchanged = [
        market_id
        for market_id, before_hash in payload.unchanged_market_hash_before.items()
        if payload.unchanged_market_hash_after.get(market_id) != before_hash
    ]
    if changed_unchanged:
        raise ValueError(
            "unchanged market hash changed: " + ", ".join(sorted(changed_unchanged))
        )
    for cd_id, members in payload.cd_members.items():
        parent = payload.cd_parent_ml.get(cd_id)
        if parent is None:
            raise ValueError(f"CD membership is not a subset of parent ML: {cd_id}")
        if not set(members) <= set(payload.ml_members.get(parent, ())):
            raise ValueError(f"CD membership is not a subset of parent ML: {cd_id}")
    sigma_mismatches = [
        market_id
        for market_id, before_value in payload.sigma_before.items()
        if payload.sigma_after.get(market_id) != before_value
    ]
    if sigma_mismatches:
        raise ValueError("sigma mismatch: " + ", ".join(sorted(sigma_mismatches)))


def validate_candidate_seed_contract(contract: CandidateSeedContract) -> None:
    _validate_snapshot_group(
        contract.live_catalog,
        required=LIVE_CATALOG_TABLES,
        label="live catalog",
    )
    _validate_snapshot_group(
        contract.strategic_tables,
        required=STRATEGIC_REFRESH_TABLES,
        label="strategic tables",
    )


def validate_refresh_publish_plan(plan: RefreshPublishPlan) -> None:
    validate_candidate_seed(plan.candidate)
    if plan.corpus is None:
        raise ValueError("publish plan requires candidate and backup corpus")
    if plan.approval_identity is None:
        raise ValueError("publish plan requires approval identity")
    if not plan.corpus.candidate_dir.is_dir() or not plan.corpus.backup_dir.is_dir():
        raise ValueError("publish plan corpus paths must exist")
    if plan.corpus.candidate_dir != plan.candidate_dir:
        raise ValueError("publish plan candidate corpus does not match candidate_dir")
    if not plan.journal_path.is_file():
        raise ValueError("publish plan requires pre-created journal")
    if plan.approval_identity.mi_master_sha256 != plan.candidate.mi_master_sha256:
        raise ValueError("publish plan approval identity does not match candidate")


def atomic_publish_candidate(plan: RefreshPublishPlan) -> RefreshPublishResult:
    validate_refresh_publish_plan(plan)
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


def _validate_snapshot_group(
    snapshots: Sequence[CatalogTableSnapshot],
    *,
    required: Sequence[str],
    label: str,
) -> None:
    by_table = {snapshot.table_name: snapshot for snapshot in snapshots}
    missing = [table for table in required if table not in by_table]
    if missing:
        raise ValueError(f"{label} missing snapshots: {', '.join(missing)}")
    for table in required:
        snapshot = by_table[table]
        if snapshot.row_count < 0:
            raise ValueError(f"{table} row_count must be non-negative")
        _require_sha256(snapshot.sha256, f"{table}.sha256")


def _sha256_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
