"""Replacement diff, parity, and reference-policy gates."""

from __future__ import annotations

from typing import Literal, Mapping, Sequence

from pipeline.etl.mi_master_refresh.contracts import (
    ReferenceReport,
    RemovedIdApproval,
    ReplacementDiff,
    ReplacementReferencePolicy,
    ReplacementTableParity,
)
from pipeline.etl.mi_master_refresh.utils import SHA256_RE, sha256_json


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
        expected_after_counts={table: len(rows) for table, rows in new_by_table.items()},
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
    return sha256_json(payload)


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


def validate_replacement_parity(parity: Sequence[ReplacementTableParity]) -> None:
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
            if _removed_approval_mismatch(diff, removed_id_approval):
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


def _removed_approval_mismatch(
    diff: ReplacementDiff,
    approval: RemovedIdApproval | None,
) -> bool:
    return (
        approval is None
        or not approval.approved
        or tuple(sorted(approval.removed_ids)) != diff.removed_ids
        or not approval.approver.strip()
        or not approval.reason.strip()
    )
