from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.shadow_archive_provenance import finalize_archive
from scripts.shadow_transition_contract import (
    PRECONDITION_STOP_RC,
    OutcomeConsistencyError,
    TransitionObservation,
    audit_legacy_outcome_files,
    classify_rc,
    record_outcome,
    validate_outcome_files,
)


def test_precondition_stop_has_a_distinct_classification() -> None:
    classification = classify_rc(PRECONDITION_STOP_RC)

    assert classification.kind == "precondition_stop"
    assert classification.verdict == "PRECONDITION_STOP_IDENTITY_DRIFT"
    assert classification.unexpected is False


def test_unrecognised_rc_is_an_unexpected_harness_error() -> None:
    classification = classify_rc(99)

    assert classification.kind == "unexpected_error"
    assert classification.verdict == "UNEXPECTED_ERROR_ROLLED_BACK"
    assert classification.unexpected is True


def test_no_mutation_uses_observed_mode_instead_of_target_mode(tmp_path: Path) -> None:
    outcome = record_outcome(
        tmp_path,
        rc=PRECONDITION_STOP_RC,
        observation=TransitionObservation(
            patched=False,
            rolled_back=False,
            observed_mode="OFF",
            target_mode="SHADOW",
        ),
    )

    assert outcome.patched is False
    assert outcome.final_mode == "OFF"
    assert "SHADOW" not in (tmp_path / "disposition.txt").read_text(encoding="utf-8")
    assert validate_outcome_files(tmp_path) == outcome


def test_success_requires_observed_target_mode_after_real_mutation(tmp_path: Path) -> None:
    with pytest.raises(OutcomeConsistencyError, match="successful transition"):
        record_outcome(
            tmp_path,
            rc=0,
            observation=TransitionObservation(
                patched=False,
                rolled_back=False,
                observed_mode="OFF",
                target_mode="SHADOW",
            ),
        )


def test_verdict_channel_drift_is_detected(tmp_path: Path) -> None:
    record_outcome(
        tmp_path,
        rc=PRECONDITION_STOP_RC,
        observation=TransitionObservation(
            patched=False,
            rolled_back=False,
            observed_mode="OFF",
            target_mode="SHADOW",
        ),
    )
    (tmp_path / "verdict.txt").write_text("UNEXPECTED_ERROR_ROLLED_BACK\n", encoding="utf-8")

    with pytest.raises(OutcomeConsistencyError, match="verdict.txt"):
        validate_outcome_files(tmp_path)


def test_patched_false_target_mode_injection_is_detected(tmp_path: Path) -> None:
    record_outcome(
        tmp_path,
        rc=PRECONDITION_STOP_RC,
        observation=TransitionObservation(
            patched=False,
            rolled_back=False,
            observed_mode="OFF",
            target_mode="SHADOW",
        ),
    )
    disposition_path = tmp_path / "disposition.txt"
    disposition_path.write_text(
        disposition_path.read_text(encoding="utf-8").replace("final_mode=OFF", "final_mode=SHADOW"),
        encoding="utf-8",
    )

    with pytest.raises(OutcomeConsistencyError, match="disposition.txt"):
        validate_outcome_files(tmp_path)


def test_archive_rebuild_records_stale_and_authoritative_sha(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "result.txt").write_text("authoritative\n", encoding="utf-8")
    archive_path = tmp_path / "shadow.zip"
    archive_path.write_bytes(b"stale candidate")
    stale_sha = hashlib.sha256(b"stale candidate").hexdigest()

    provenance = finalize_archive(
        evidence_dir,
        archive_path,
        rebuild_reason="final disposition replaced the candidate report",
    )

    assert provenance.stale_sha256 == stale_sha
    assert provenance.authoritative_sha256 == hashlib.sha256(archive_path.read_bytes()).hexdigest()
    sidecar = json.loads(archive_path.with_suffix(".zip.provenance.json").read_text(encoding="utf-8"))
    assert sidecar == {
        "archive": provenance.archive,
        "authoritative_sha256": provenance.authoritative_sha256,
        "authoritative_sha_location": provenance.authoritative_sha_location,
        "stale_reason": provenance.stale_reason,
        "stale_sha256": provenance.stale_sha256,
    }
    assert archive_path.with_suffix(".zip.sha256").read_text(encoding="utf-8").startswith(
        provenance.authoritative_sha256
    )


def test_legacy_rc41_contradiction_fixture_reproduces_all_channel_defects(
    tmp_path: Path,
) -> None:
    (tmp_path / "result_rc.txt").write_text("41\n", encoding="utf-8")
    (tmp_path / "verdict.txt").write_text(
        "UNEXPECTED_ERROR_ROLLED_BACK\n",
        encoding="utf-8",
    )
    (tmp_path / "disposition.txt").write_text(
        "patched=false\nrolled_back=false\nfinal_mode=SHADOW\n",
        encoding="utf-8",
    )

    issues = audit_legacy_outcome_files(tmp_path)

    assert issues == [
        "missing_authoritative_outcome",
        "rc_verdict_mismatch",
        "missing_observed_mode",
        "target_mode_recorded_without_mutation",
    ]
