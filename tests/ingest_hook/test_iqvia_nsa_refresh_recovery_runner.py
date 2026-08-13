from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from pipeline.scripts.deploy.mart_load_ops import PublishAction
from pipeline.scripts.ingest_hook import iqvia_nsa_refresh_recovery_runner as runner
from pipeline.scripts.ingest_hook.ledger import StageEvent


IDENTITY = (
    "2026-Q1",
    "iqvia_nsa",
    "95c9de6098a19f82edc0f9ad7a678c9c812c0f59b238838b40faaa58fbf4ad19",
)
FAILED_RUN_ID = "20260808182426423756"


class _Ledger:
    def __init__(
        self,
        *,
        fail_complete: bool = False,
        interrupted_recovery_run_id: str | None = None,
        persist_stage_records: bool = True,
    ) -> None:
        self.fail_complete = fail_complete
        self.completed: dict[str, int] | None = None
        self.stage_records: list[tuple[object, ...]] = []
        self.interrupted_recovery_run_id = interrupted_recovery_run_id
        self.interrupted_recovery_status = "running"
        self.interrupted_recovery_reason: str | None = None
        self.interrupted_recovery_finished_at: str | None = None
        self.persist_stage_records = persist_stage_records

    def status(self, *_identity: str):
        return SimpleNamespace(status="failed", run_id=FAILED_RUN_ID)

    def prepared_candidate(self, *_identity: str):
        return None

    def stage_events(self, *_identity: str):
        events = [
            StageEvent(FAILED_RUN_ID, 7, "mart_publish", "complete", None, None, None, 1),
            StageEvent(FAILED_RUN_ID, 8, "refresh", "failed", "wrong schema", None, None, 1),
        ]
        if self.interrupted_recovery_run_id is not None:
            events.append(
                StageEvent(
                    self.interrupted_recovery_run_id,
                    8,
                    "refresh",
                    self.interrupted_recovery_status,
                    self.interrupted_recovery_reason,
                    "2026-08-08 21:21:04",
                    self.interrupted_recovery_finished_at,
                    None,
                )
            )
        return events

    def record_stage(self, *args: object, **kwargs: object) -> None:
        self.stage_records.append((*args, kwargs))
        if (
            self.persist_stage_records
            and kwargs.get("run_id") == self.interrupted_recovery_run_id
            and kwargs.get("seq") == 8
        ):
            self.interrupted_recovery_status = str(kwargs["status"])
            self.interrupted_recovery_reason = str(kwargs["reason"])
            self.interrupted_recovery_finished_at = str(kwargs["finished_at"])

    def mark_complete(self, *_identity: str, row_counts: dict[str, int]) -> None:
        if self.fail_complete:
            raise RuntimeError("ledger unavailable")
        self.completed = row_counts


def _publication():
    inventory_json = '[{"path":"nsa.xlsx","rows":891567}]'
    return SimpleNamespace(
        mart_publication_epoch=12,
        epoch="2026-Q1",
        run_id=FAILED_RUN_ID,
        inventory_json=inventory_json,
        inventory_sha256=hashlib.sha256(inventory_json.encode("utf-8")).hexdigest(),
        window_start="2021-Q2",
        window_end="2026-Q1",
    )


def _actions() -> tuple[PublishAction, ...]:
    return (
        PublishAction("raw", "recovery_atomic_group_rename", "raw__failed", "raw__rr", 0),
    )


def test_recovery_reuses_preserved_tables_and_runs_refresh_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _Ledger()
    observed: list[str] = []
    monkeypatch.setattr(runner.publication, "read_rolled_back_publication", lambda *_a, **_k: _publication())
    monkeypatch.setattr(runner.activation, "promote_failed_publication_atomically", lambda *_a, **_k: observed.append("promote") or _actions())
    monkeypatch.setattr(runner.activation, "restore_failed_publication_atomically", lambda *_a, **_k: observed.append("restore"))
    monkeypatch.setattr(runner.publication, "mark_publication_recovered", lambda *_a, **_k: observed.append("provenance"))
    monkeypatch.setattr(runner.job_runner, "_run_commands_with_writer_lock", lambda *_a, **_k: observed.append("refresh"))
    monkeypatch.setattr(runner.ubist_activation, "acquire_writer_lock", lambda *_a, **_k: observed.append("lock"))
    monkeypatch.setattr(runner.ubist_activation, "release_writer_lock", lambda *_a, **_k: observed.append("unlock"))
    monkeypatch.setattr(runner.ubist_activation, "require_writer_lock_owner", lambda *_a, **_k: None)

    runner.recover(
        ledger=ledger,
        writer_conn=object(),
        activation_config=SimpleNamespace(target_db="jw_mart_d2"),
        identity=IDENTITY,
        failed_run_id=FAILED_RUN_ID,
        recovery_run_id="recovery-1",
        refresh_argv=("python", "-m", "pipeline.orchestrator"),
    )

    assert observed == [
        "lock",
        "promote",
        "refresh",
        "provenance",
        "unlock",
    ]
    assert ledger.completed == {"nsa.xlsx": 891567}
    assert {
        record[-1]["run_id"]
        for record in ledger.stage_records
    } == {"recovery-1"}


def test_recovery_resumes_an_already_promoted_table_group_without_renaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _Ledger(interrupted_recovery_run_id="recovery-1")
    observed: list[str] = []
    monkeypatch.setattr(runner.publication, "read_rolled_back_publication", lambda *_a, **_k: _publication())
    monkeypatch.setattr(
        runner.activation,
        "resume_failed_publication_actions",
        lambda *_a, **_k: observed.append("resume") or _actions(),
    )
    monkeypatch.setattr(
        runner.activation,
        "promote_failed_publication_atomically",
        lambda *_a, **_k: pytest.fail("resume must not rename the table group again"),
    )
    monkeypatch.setattr(runner.publication, "mark_publication_recovered", lambda *_a, **_k: observed.append("provenance"))
    monkeypatch.setattr(runner.job_runner, "_run_commands_with_writer_lock", lambda *_a, **_k: observed.append("refresh"))
    monkeypatch.setattr(runner.ubist_activation, "acquire_writer_lock", lambda *_a, **_k: observed.append("lock"))
    monkeypatch.setattr(runner.ubist_activation, "release_writer_lock", lambda *_a, **_k: observed.append("unlock"))
    monkeypatch.setattr(runner.ubist_activation, "require_writer_lock_owner", lambda *_a, **_k: None)

    runner.recover(
        ledger=ledger,
        writer_conn=object(),
        activation_config=SimpleNamespace(target_db="jw_mart_d2"),
        identity=IDENTITY,
        failed_run_id=FAILED_RUN_ID,
        recovery_run_id="recovery-2",
        promoted_recovery_run_id="recovery-1",
        refresh_argv=("refresh",),
    )

    assert observed == ["lock", "resume", "refresh", "provenance", "unlock"]
    assert ledger.completed == {"nsa.xlsx": 891567}
    interrupted_records = [
        record[-1]
        for record in ledger.stage_records
        if record[-1]["run_id"] == "recovery-1"
    ]
    assert len(interrupted_records) == 1
    assert interrupted_records[0]["status"] == "failed"
    assert interrupted_records[0]["finished_at"] is not None
    assert interrupted_records[0]["reason"] == "superseded by recovery run recovery-2"


def test_recovery_stops_before_lock_when_interrupted_stage_cannot_be_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _Ledger(
        interrupted_recovery_run_id="recovery-1",
        persist_stage_records=False,
    )
    monkeypatch.setattr(
        runner.publication,
        "read_rolled_back_publication",
        lambda *_args, **_kwargs: _publication(),
    )
    monkeypatch.setattr(
        runner.ubist_activation,
        "acquire_writer_lock",
        lambda *_args, **_kwargs: pytest.fail("writer lock must not be acquired"),
    )

    with pytest.raises(RuntimeError, match="interrupted refresh stage did not close"):
        runner.recover(
            ledger=ledger,
            writer_conn=object(),
            activation_config=SimpleNamespace(target_db="jw_mart_d2"),
            identity=IDENTITY,
            failed_run_id=FAILED_RUN_ID,
            recovery_run_id="recovery-2",
            promoted_recovery_run_id="recovery-1",
            refresh_argv=("refresh",),
        )


def test_ledger_completion_failure_restores_tables_and_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _Ledger(fail_complete=True)
    observed: list[str] = []
    monkeypatch.setattr(runner.publication, "read_rolled_back_publication", lambda *_a, **_k: _publication())
    monkeypatch.setattr(runner.activation, "promote_failed_publication_atomically", lambda *_a, **_k: _actions())
    monkeypatch.setattr(runner.activation, "restore_failed_publication_atomically", lambda *_a, **_k: observed.append("restore"))
    monkeypatch.setattr(runner.publication, "mark_publication_recovered", lambda *_a, **_k: observed.append("published"))
    monkeypatch.setattr(runner.publication, "mark_publication_recovery_rolled_back", lambda *_a, **_k: observed.append("rolled_back"))
    monkeypatch.setattr(runner.job_runner, "_run_commands_with_writer_lock", lambda *_a, **_k: observed.append("refresh"))
    monkeypatch.setattr(runner.ubist_activation, "acquire_writer_lock", lambda *_a, **_k: None)
    monkeypatch.setattr(runner.ubist_activation, "release_writer_lock", lambda *_a, **_k: observed.append("unlock"))
    monkeypatch.setattr(runner.ubist_activation, "require_writer_lock_owner", lambda *_a, **_k: None)

    with pytest.raises(RuntimeError, match="ledger unavailable"):
        runner.recover(
            ledger=ledger,
            writer_conn=object(),
            activation_config=SimpleNamespace(target_db="jw_mart_d2"),
            identity=IDENTITY,
            failed_run_id=FAILED_RUN_ID,
            recovery_run_id="recovery-1",
            refresh_argv=("refresh",),
        )

    assert observed == [
        "refresh",
        "published",
        "restore",
        "refresh",
        "rolled_back",
        "unlock",
    ]


def test_refresh_failure_restores_tables_and_refreshes_restored_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []
    refresh_calls = 0

    def refresh(*_args: object, **_kwargs: object) -> None:
        nonlocal refresh_calls
        refresh_calls += 1
        observed.append(f"refresh:{refresh_calls}")
        if refresh_calls == 1:
            raise RuntimeError("partial refresh")

    monkeypatch.setattr(runner.publication, "read_rolled_back_publication", lambda *_a, **_k: _publication())
    monkeypatch.setattr(runner.activation, "promote_failed_publication_atomically", lambda *_a, **_k: _actions())
    monkeypatch.setattr(runner.activation, "restore_failed_publication_atomically", lambda *_a, **_k: observed.append("restore"))
    monkeypatch.setattr(runner.job_runner, "_run_commands_with_writer_lock", refresh)
    monkeypatch.setattr(runner.ubist_activation, "acquire_writer_lock", lambda *_a, **_k: None)
    monkeypatch.setattr(runner.ubist_activation, "release_writer_lock", lambda *_a, **_k: observed.append("unlock"))
    monkeypatch.setattr(runner.ubist_activation, "require_writer_lock_owner", lambda *_a, **_k: None)

    with pytest.raises(RuntimeError, match="partial refresh"):
        runner.recover(
            ledger=_Ledger(),
            writer_conn=object(),
            activation_config=SimpleNamespace(target_db="jw_mart_d2"),
            identity=IDENTITY,
            failed_run_id=FAILED_RUN_ID,
            recovery_run_id="recovery-1",
            refresh_argv=("refresh",),
        )

    assert observed == ["refresh:1", "restore", "refresh:2", "unlock"]


def test_recovery_rejects_inventory_hash_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _publication()
    evidence.inventory_sha256 = "0" * 64
    monkeypatch.setattr(runner.publication, "read_rolled_back_publication", lambda *_a, **_k: evidence)

    with pytest.raises(RuntimeError, match="inventory SHA256 mismatch"):
        runner.recover(
            ledger=_Ledger(),
            writer_conn=object(),
            activation_config=SimpleNamespace(target_db="jw_mart_d2"),
            identity=IDENTITY,
            failed_run_id=FAILED_RUN_ID,
            recovery_run_id="recovery-1",
            refresh_argv=("refresh",),
        )


def test_recovery_rejects_a_prepared_candidate() -> None:
    ledger = _Ledger()
    ledger.prepared_candidate = lambda *_identity: object()  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="must not have a prepared candidate"):
        runner.recover(
            ledger=ledger,
            writer_conn=object(),
            activation_config=SimpleNamespace(target_db="jw_mart_d2"),
            identity=IDENTITY,
            failed_run_id=FAILED_RUN_ID,
            recovery_run_id="recovery-1",
            refresh_argv=("refresh",),
        )


def test_recovery_rejects_an_empty_refresh_command() -> None:
    with pytest.raises(RuntimeError, match="refresh command is empty"):
        runner.recover(
            ledger=_Ledger(),
            writer_conn=object(),
            activation_config=SimpleNamespace(target_db="jw_mart_d2"),
            identity=IDENTITY,
            failed_run_id=FAILED_RUN_ID,
            recovery_run_id="recovery-1",
            refresh_argv=(),
        )
