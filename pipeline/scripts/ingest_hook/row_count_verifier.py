"""Fail-closed row-count evidence for ingest table loads."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class LoadKind(StrEnum):
    APPEND = "append"
    UPSERT = "upsert"
    REPLACE = "replace"


@dataclass(frozen=True, slots=True)
class RowCountEvidence:
    schema: str
    table: str
    kind: LoadKind
    rows_before: int
    rows_after: int
    rows_loaded: int
    source_rows: int
    difference_reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, str | int | list[str]]:
        return {
            "schema": self.schema,
            "table": self.table,
            "kind": self.kind.value,
            "rows_before": self.rows_before,
            "rows_after": self.rows_after,
            "rows_loaded": self.rows_loaded,
            "source_rows": self.source_rows,
            "difference_reasons": list(self.difference_reasons),
        }


class RowCountVerificationError(RuntimeError):
    pass


def verify_row_counts(evidence: RowCountEvidence) -> RowCountEvidence:
    """Reject impossible loader claims while preserving replace semantics."""
    counts = (
        evidence.rows_before,
        evidence.rows_after,
        evidence.rows_loaded,
        evidence.source_rows,
    )
    if any(value < 0 for value in counts):
        raise RowCountVerificationError(f"negative row count for {evidence.schema}.{evidence.table}: {counts}")

    if evidence.source_rows != evidence.rows_loaded and not evidence.difference_reasons:
        raise RowCountVerificationError(
            f"source/load difference has no reason for {evidence.schema}.{evidence.table}: "
            f"source={evidence.source_rows} loaded={evidence.rows_loaded}"
        )

    match evidence.kind:
        case LoadKind.APPEND | LoadKind.UPSERT:
            if evidence.rows_after < evidence.rows_before:
                raise RowCountVerificationError(
                    f"table decreased for {evidence.schema}.{evidence.table}: "
                    f"before={evidence.rows_before} after={evidence.rows_after}"
                )
            if evidence.rows_loaded > 0 and evidence.rows_after == evidence.rows_before:
                raise RowCountVerificationError(
                    f"loader claimed {evidence.rows_loaded} rows but table did not grow: "
                    f"{evidence.schema}.{evidence.table}"
                )
            delta = evidence.rows_after - evidence.rows_before
            if delta != evidence.rows_loaded:
                raise RowCountVerificationError(
                    f"append delta mismatch for {evidence.schema}.{evidence.table}: "
                    f"delta={delta} loaded={evidence.rows_loaded}"
                )
        case LoadKind.REPLACE:
            if evidence.rows_after != evidence.rows_loaded:
                raise RowCountVerificationError(
                    f"replace rebuilt count mismatch for {evidence.schema}.{evidence.table}: "
                    f"after={evidence.rows_after} rebuilt={evidence.rows_loaded}"
                )
    return evidence
