from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType

import pytest

from pipeline.scripts.deploy.brand_activity_307 import row_topic_monthly_wrapper as wrapper


class _Cursor:
    def __init__(self) -> None:
        self.sql = ""

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        return None

    def execute(self, sql: str) -> None:
        self.sql = sql

    def fetchall(self) -> list[dict[str, str]]:
        return [{"run_id": "pending-run"}]


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self._cursor


def test_no_pending_receipt_is_a_noop_without_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given no complete pending axis receipt, the monthly assignment never starts."""
    monkeypatch.setattr(wrapper, "_prepare_environment", lambda: Path.cwd())
    monkeypatch.setattr(wrapper.os, "chdir", lambda _path: None)
    monkeypatch.setattr(wrapper, "_pending_topic_set_versions", lambda: ())
    monkeypatch.setattr(wrapper, "_producer_observation", lambda: None)
    monkeypatch.setattr(
        wrapper,
        "_run_row_topic",
        lambda *_args, **_kwargs: pytest.fail("assignment must not run"),
    )
    monkeypatch.delenv("ROW_TOPIC_SET_VERSION", raising=False)
    monkeypatch.setenv("GATE_MODE", "auto")

    assert wrapper.main() == 0


@pytest.mark.parametrize(
    ("observation", "expected_outcome", "expected_reason"),
    [
        (
            wrapper.ProducerObservation(
                status=wrapper.PRODUCER_NOOP,
                reason="input_unchanged",
            ),
            "B_normal_noop",
            "producer_input_unchanged",
        ),
        (
            wrapper.ProducerObservation(
                status=wrapper.PRODUCER_FAILED,
                reason="topic_run_failed",
            ),
            "C_producer_failed",
            "producer_failed_before_receipt",
        ),
        (
            None,
            "D_producer_not_run",
            "producer_observation_missing",
        ),
    ],
)
def test_no_pending_receipt_distinguishes_b_c_d_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    observation: wrapper.ProducerObservation | None,
    expected_outcome: str,
    expected_reason: str,
) -> None:
    """Absent assignment work remains exit zero while B, C, and D stay observable."""
    monkeypatch.setattr(wrapper, "_prepare_environment", lambda: Path.cwd())
    monkeypatch.setattr(wrapper.os, "chdir", lambda _path: None)
    monkeypatch.setattr(wrapper, "_pending_topic_set_versions", lambda: ())
    monkeypatch.setattr(wrapper, "_producer_observation", lambda: observation)
    monkeypatch.setattr(
        wrapper,
        "_run_row_topic",
        lambda *_args, **_kwargs: pytest.fail("absence classification must not run assignment"),
    )
    monkeypatch.delenv("ROW_TOPIC_SET_VERSION", raising=False)
    monkeypatch.setenv("GATE_MODE", "auto")

    assert wrapper.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["producer_outcome"] == expected_outcome
    assert payload["reason"] == expected_reason
    assert payload["calls"] == 0
    assert payload["inserts"] == 0


def test_existing_pending_receipt_path_does_not_read_producer_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal A receipt keeps the existing reconciliation behavior unchanged."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(wrapper, "_prepare_environment", lambda: Path.cwd())
    monkeypatch.setattr(wrapper.os, "chdir", lambda _path: None)
    monkeypatch.setattr(wrapper, "_pending_topic_set_versions", lambda: ("pending-run",))
    monkeypatch.setattr(
        wrapper,
        "_producer_observation",
        lambda: pytest.fail("normal receipt path must not inspect absence evidence"),
    )

    def _run(mode: str, version: str, max_calls: int | None = None):
        del max_calls
        calls.append((mode, version))
        return (
            {"pending_rows": 0, "pending_batches": 0}
            if mode == "dry-run"
            else {"complete": True}
        )

    monkeypatch.setattr(wrapper, "_run_row_topic", _run)
    monkeypatch.delenv("ROW_TOPIC_SET_VERSION", raising=False)
    monkeypatch.setenv("GATE_MODE", "auto")

    assert wrapper.main() == 0
    assert calls == [("dry-run", "pending-run"), ("reconcile", "pending-run")]


def test_started_observation_is_classified_as_interrupted_failure() -> None:
    outcome = wrapper.classify_absent_receipt(
        wrapper.ProducerObservation(
            status=wrapper.PRODUCER_STARTED,
            reason="topic_run_started",
        )
    )

    assert outcome.code == "C_producer_failed"
    assert outcome.reason == "producer_started_but_receipt_missing"


def test_unknown_observation_status_stays_nonblocking_but_visible() -> None:
    outcome = wrapper.classify_absent_receipt(
        wrapper.ProducerObservation(status="future_status", reason="future_reason")
    )

    assert outcome.code == "C_producer_failed"
    assert outcome.reason == "producer_observation_unrecognized"


def test_observation_read_failure_stays_nonblocking_and_visible(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        wrapper,
        "_connect",
        lambda: (_ for _ in ()).throw(RuntimeError("injected read failure")),
    )

    observation = wrapper._producer_observation()

    assert observation == wrapper.ProducerObservation(
        status="observation_read_failed",
        reason="RuntimeError",
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "error_type": "RuntimeError",
        "event": "row_topic_observation_read_failed",
    }
    outcome = wrapper.classify_absent_receipt(observation)
    assert outcome.code == "C_producer_failed"
    assert outcome.reason == "producer_observation_read_failed"


def test_monthly_observation_key_matches_producer_contract() -> None:
    observed_at = datetime(2026, 8, 4, 13, 0, tzinfo=timezone.utc)

    assert wrapper._observation_run_id(observed_at) == "monthly-axis-observation:2026-08"


def test_pending_receipt_query_uses_exact_fail_closed_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given a durable pending receipt, the operational query discovers only ready runs."""
    cursor = _Cursor()
    monkeypatch.setattr(wrapper, "_connect", lambda: _Connection(cursor))

    assert wrapper._pending_topic_set_versions() == ("pending-run",)
    assert "axis_status='complete'" in cursor.sql
    assert "assignment_status IN ('pending','running','gap')" in cursor.sql


def test_pending_receipt_is_reconciled_even_when_no_calls_remain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given an artificial pending receipt, the wrapper discovers and reconciles it."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(wrapper, "_prepare_environment", lambda: Path.cwd())
    monkeypatch.setattr(wrapper.os, "chdir", lambda _path: None)
    monkeypatch.setattr(
        wrapper,
        "_pending_topic_set_versions",
        lambda: ("pending-run",),
    )

    def _run(mode: str, version: str, max_calls: int | None = None):
        del max_calls
        calls.append((mode, version))
        if mode == "dry-run":
            return {"pending_rows": 0, "pending_batches": 0}
        return {"complete": True}

    monkeypatch.setattr(wrapper, "_run_row_topic", _run)
    monkeypatch.delenv("ROW_TOPIC_SET_VERSION", raising=False)
    monkeypatch.setenv("GATE_MODE", "auto")

    assert wrapper.main() == 0
    assert calls == [
        ("dry-run", "pending-run"),
        ("reconcile", "pending-run"),
    ]
