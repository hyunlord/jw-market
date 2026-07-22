from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Final, Mapping


PRECONDITION_STOP_RC: Final = 41


@dataclass(frozen=True, slots=True)
class RcClassification:
    kind: str
    verdict: str
    unexpected: bool = False


RC_CLASSIFICATIONS: Final[Mapping[int, RcClassification]] = {
    0: RcClassification("success", "RETAINED_SHADOW_PASS"),
    PRECONDITION_STOP_RC: RcClassification(
        "precondition_stop",
        "PRECONDITION_STOP_IDENTITY_DRIFT",
    ),
    42: RcClassification("precondition_stop", "PRECONDITION_STOP_ENV_DRIFT"),
    43: RcClassification("precondition_stop", "PRECONDITION_STOP_ANNOTATION_DRIFT"),
    46: RcClassification("precondition_stop", "PRECONDITION_STOP_AUTH_UNAVAILABLE"),
    47: RcClassification("precondition_stop", "PRECONDITION_STOP_PROBE_UNAVAILABLE"),
    51: RcClassification("rollback", "ROLLED_BACK_RESPONSE_PARITY"),
    52: RcClassification("rollback", "ROLLED_BACK_SHADOW_RECORD_LEAK"),
    53: RcClassification("retained_failure", "RETAINED_SHADOW_RECORDING_MISOPERATIVE"),
    73: RcClassification("precondition_stop", "PRECONDITION_STOP_REMOTE_LOCK_BUSY"),
    75: RcClassification("precondition_stop", "PRECONDITION_STOP_DIRECT_SESSION_BUSY"),
    76: RcClassification("cleanup_failure", "OWNED_RESOURCE_RESIDUE"),
}
UNEXPECTED_CLASSIFICATION: Final = RcClassification(
    "unexpected_error",
    "UNEXPECTED_ERROR_ROLLED_BACK",
    unexpected=True,
)


@dataclass(frozen=True, slots=True)
class TransitionObservation:
    patched: bool
    rolled_back: bool
    observed_mode: str
    target_mode: str = "SHADOW"


@dataclass(frozen=True, slots=True)
class TransitionOutcome:
    schema_version: int
    rc: int
    classification: str
    verdict: str
    unexpected: bool
    patched: bool
    rolled_back: bool
    observed_mode: str
    target_mode: str
    final_mode: str


class OutcomeConsistencyError(ValueError):
    pass


def classify_rc(rc: int) -> RcClassification:
    return RC_CLASSIFICATIONS.get(rc, UNEXPECTED_CLASSIFICATION)


def _normalise_mode(value: str, *, field: str) -> str:
    mode = value.strip().upper()
    if mode not in {"OFF", "SHADOW", "ENFORCE"}:
        raise OutcomeConsistencyError(f"{field} has unsupported mode: {value!r}")
    return mode


def build_outcome(*, rc: int, observation: TransitionObservation) -> TransitionOutcome:
    classification = classify_rc(rc)
    observed_mode = _normalise_mode(observation.observed_mode, field="observed_mode")
    target_mode = _normalise_mode(observation.target_mode, field="target_mode")

    if observation.rolled_back and not observation.patched:
        raise OutcomeConsistencyError("rolled_back requires a preceding mutation")
    if classification.kind == "success" and (
        not observation.patched
        or observation.rolled_back
        or observed_mode != target_mode
    ):
        raise OutcomeConsistencyError(
            "successful transition requires a real mutation and the observed target mode"
        )
    if classification.kind == "precondition_stop" and observation.patched:
        raise OutcomeConsistencyError("precondition stop must occur before mutation")
    if observation.rolled_back and observed_mode != "OFF":
        raise OutcomeConsistencyError("rolled-back transition must be observed in OFF mode")

    return TransitionOutcome(
        schema_version=1,
        rc=rc,
        classification=classification.kind,
        verdict=classification.verdict,
        unexpected=classification.unexpected,
        patched=observation.patched,
        rolled_back=observation.rolled_back,
        observed_mode=observed_mode,
        target_mode=target_mode,
        final_mode=observed_mode,
    )


def atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def json_text(value: RcClassification | TransitionOutcome) -> str:
    return json.dumps(asdict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def record_outcome(
    output_dir: Path,
    *,
    rc: int,
    observation: TransitionObservation,
) -> TransitionOutcome:
    """Write one authoritative outcome and compatibility views derived from it."""

    outcome = build_outcome(rc=rc, observation=observation)
    atomic_write_text(output_dir / "transition_outcome.json", json_text(outcome))
    atomic_write_text(output_dir / "verdict.txt", f"{outcome.verdict}\n")
    atomic_write_text(output_dir / "result_rc.txt", f"{outcome.rc}\n")
    atomic_write_text(
        output_dir / "disposition.txt",
        "".join(
            (
                f"patched={str(outcome.patched).lower()}\n",
                f"rolled_back={str(outcome.rolled_back).lower()}\n",
                f"observed_mode={outcome.observed_mode}\n",
                f"final_mode={outcome.final_mode}\n",
            )
        ),
    )
    return validate_outcome_files(output_dir)


def _parse_disposition(path: Path) -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if not separator or not key or key in rows:
            raise OutcomeConsistencyError(f"invalid disposition line in {path.name}: {line!r}")
        rows[key] = value
    return rows


def audit_legacy_outcome_files(output_dir: Path) -> list[str]:
    """Diagnose pre-contract evidence without treating it as authoritative."""

    issues: list[str] = []
    if not (output_dir / "transition_outcome.json").is_file():
        issues.append("missing_authoritative_outcome")

    rc = int((output_dir / "result_rc.txt").read_text(encoding="utf-8").strip())
    verdict = (output_dir / "verdict.txt").read_text(encoding="utf-8").strip()
    if verdict != classify_rc(rc).verdict:
        issues.append("rc_verdict_mismatch")

    disposition = _parse_disposition(output_dir / "disposition.txt")
    if "observed_mode" not in disposition:
        issues.append("missing_observed_mode")
    patched = disposition.get("patched", "").lower() == "true"
    final_mode = disposition.get("final_mode", "").upper()
    if not patched and final_mode == "SHADOW":
        issues.append("target_mode_recorded_without_mutation")
    return issues


def _parse_authoritative_outcome(path: Path) -> TransitionOutcome:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise OutcomeConsistencyError("transition_outcome.json must contain an object")
    try:
        return TransitionOutcome(
            schema_version=int(raw["schema_version"]),
            rc=int(raw["rc"]),
            classification=str(raw["classification"]),
            verdict=str(raw["verdict"]),
            unexpected=bool(raw["unexpected"]),
            patched=bool(raw["patched"]),
            rolled_back=bool(raw["rolled_back"]),
            observed_mode=str(raw["observed_mode"]),
            target_mode=str(raw["target_mode"]),
            final_mode=str(raw["final_mode"]),
        )
    except KeyError as exc:
        raise OutcomeConsistencyError(f"transition_outcome.json is missing {exc.args[0]}") from exc


def validate_outcome_files(output_dir: Path) -> TransitionOutcome:
    outcome = _parse_authoritative_outcome(output_dir / "transition_outcome.json")
    expected = build_outcome(
        rc=outcome.rc,
        observation=TransitionObservation(
            patched=outcome.patched,
            rolled_back=outcome.rolled_back,
            observed_mode=outcome.observed_mode,
            target_mode=outcome.target_mode,
        ),
    )
    if outcome != expected:
        raise OutcomeConsistencyError("transition_outcome.json contradicts its derived fields")

    verdict = (output_dir / "verdict.txt").read_text(encoding="utf-8").strip()
    if verdict != outcome.verdict:
        raise OutcomeConsistencyError("verdict.txt contradicts transition_outcome.json")
    result_rc = (output_dir / "result_rc.txt").read_text(encoding="utf-8").strip()
    if result_rc != str(outcome.rc):
        raise OutcomeConsistencyError("result_rc.txt contradicts transition_outcome.json")

    disposition = _parse_disposition(output_dir / "disposition.txt")
    expected_disposition = {
        "patched": str(outcome.patched).lower(),
        "rolled_back": str(outcome.rolled_back).lower(),
        "observed_mode": outcome.observed_mode,
        "final_mode": outcome.final_mode,
    }
    if disposition != expected_disposition:
        raise OutcomeConsistencyError("disposition.txt contradicts transition_outcome.json")
    return outcome
