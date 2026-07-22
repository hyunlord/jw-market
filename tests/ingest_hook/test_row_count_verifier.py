from __future__ import annotations

import pytest

from pipeline.scripts.ingest_hook.row_count_verifier import (
    LoadKind,
    RowCountEvidence,
    RowCountVerificationError,
    verify_row_counts,
)


def _evidence(
    *,
    kind: LoadKind = LoadKind.APPEND,
    rows_before: int = 10,
    rows_after: int = 13,
    rows_loaded: int = 3,
    source_rows: int = 3,
    difference_reasons: tuple[str, ...] = (),
) -> RowCountEvidence:
    return RowCountEvidence(
        schema="jw_ingest_stage_test",
        table="raw_events",
        kind=kind,
        rows_before=rows_before,
        rows_after=rows_after,
        rows_loaded=rows_loaded,
        source_rows=source_rows,
        difference_reasons=difference_reasons,
    )


def test_append_accepts_a_matching_positive_delta() -> None:
    evidence = _evidence()

    verified = verify_row_counts(evidence)

    assert verified == evidence


def test_append_rejects_claimed_load_without_growth() -> None:
    evidence = _evidence(rows_after=10, rows_loaded=3)

    with pytest.raises(RowCountVerificationError, match="did not grow"):
        verify_row_counts(evidence)


def test_append_rejects_row_loss() -> None:
    evidence = _evidence(rows_after=9, rows_loaded=0, difference_reasons=("deduplicated=3",))

    with pytest.raises(RowCountVerificationError, match="decreased"):
        verify_row_counts(evidence)


def test_idempotent_retry_accepts_zero_growth_with_reason() -> None:
    evidence = _evidence(
        rows_after=10,
        rows_loaded=0,
        difference_reasons=("duplicate_or_previously_loaded=3",),
    )

    verified = verify_row_counts(evidence)

    assert verified.rows_loaded == 0


def test_source_difference_requires_an_explicit_reason() -> None:
    evidence = _evidence(source_rows=5)

    with pytest.raises(RowCountVerificationError, match="difference has no reason"):
        verify_row_counts(evidence)


def test_replace_allows_shrink_when_after_equals_rebuilt_rows() -> None:
    evidence = _evidence(
        kind=LoadKind.REPLACE,
        rows_before=20,
        rows_after=7,
        rows_loaded=7,
        source_rows=9,
        difference_reasons=("non_total_or_deduplicated=2",),
    )

    verified = verify_row_counts(evidence)

    assert verified.rows_after == 7


def test_replace_rejects_a_count_that_does_not_match_the_rebuild() -> None:
    evidence = _evidence(
        kind=LoadKind.REPLACE,
        rows_before=20,
        rows_after=7,
        rows_loaded=8,
        source_rows=8,
    )

    with pytest.raises(RowCountVerificationError, match="rebuilt count"):
        verify_row_counts(evidence)
