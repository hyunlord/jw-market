from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from pipeline.scripts.ingest_hook import csd_publication
from pipeline.scripts.ingest_hook import csd_activation_journal
from pipeline.scripts.ingest_hook import csd_publication_backend
from pipeline.scripts.ingest_hook.csd_publication_provenance import bounded_id


class FakeBackend:
    def __init__(self) -> None:
        self.live = {
            "raw": {f"2022-{month:02d}" for month in range(1, 13)}
            | {f"2023-{month:02d}" for month in range(1, 13)}
            | {f"2024-{month:02d}" for month in range(1, 13)}
            | {f"2025-{month:02d}" for month in range(1, 13)},
            "stage": {f"2023-{month:02d}" for month in range(1, 13)}
            | {f"2024-{month:02d}" for month in range(1, 13)}
            | {f"2025-{month:02d}" for month in range(1, 13)},
        }
        self.candidate: dict[str, set[str]] | None = None
        self.published = False
        self.provenance: list[csd_publication.PublicationRecord] = []
        self.fail_load = False
        self.fail_refresh = False
        self.fail_provenance = False
        self.events: list[str] = []

    def acquire(self, _plan: csd_publication.PublicationPlan) -> None:
        self.events.append("lock_acquired")

    def recover(self, _plan: csd_publication.PublicationPlan) -> None:
        self.events.append("recovery_checked")

    def prepare(self, _plan: csd_publication.PublicationPlan) -> None:
        self.candidate = {name: set(periods) for name, periods in self.live.items()}

    def replace_periods(
        self, _plan: csd_publication.PublicationPlan, periods: tuple[str, ...]
    ) -> None:
        assert self.candidate is not None
        self.candidate["raw"].difference_update(periods)
        if self.fail_load:
            raise RuntimeError("injected load failure")
        self.candidate["raw"].update(periods)

    def apply_windows(self, _plan: csd_publication.PublicationPlan) -> None:
        assert self.candidate is not None
        retained = sorted(self.candidate["raw"])[-csd_publication.RETAIN_MONTHS :]
        self.candidate["raw"] = set(retained)
        self.candidate["stage"] = set(retained[-csd_publication.DISPLAY_MONTHS :])

    def publish(self, _plan: csd_publication.PublicationPlan) -> object:
        self.events.append("published")
        assert self.candidate is not None
        previous = {name: set(periods) for name, periods in self.live.items()}
        self.live = {name: set(periods) for name, periods in self.candidate.items()}
        self.published = True
        return previous

    def arm_recovery(self, _plan: csd_publication.PublicationPlan) -> None:
        self.events.append("recovery_armed")

    def record_provenance(
        self, _plan: csd_publication.PublicationPlan, record: csd_publication.PublicationRecord
    ) -> None:
        if self.fail_provenance:
            raise RuntimeError("injected provenance failure")
        self.provenance.append(record)

    def verify_refresh(self, _plan: csd_publication.PublicationPlan) -> None:
        self.events.append("refresh_verified")
        if self.fail_refresh:
            raise RuntimeError("injected refresh failure")

    def complete(self, _plan: csd_publication.PublicationPlan) -> None:
        self.events.append("complete")

    def rollback(self, _plan: csd_publication.PublicationPlan, token: object) -> None:
        self.events.append("rolled_back")
        self.live = token  # type: ignore[assignment]
        self.published = False

    def release(self, _plan: csd_publication.PublicationPlan) -> None:
        self.events.append("lock_released")


def _plan(**overrides: object) -> csd_publication.PublicationPlan:
    base = csd_publication.PublicationPlan(
        category="iqvia_csd_channel",
        run_id="run-1",
        epoch="2026-01",
        incoming_periods=("2026-01",),
        builder_commit="a" * 40,
        image_digest="sha256:" + "b" * 64,
        image_ref="registry/image@sha256:" + "b" * 64,
        inventory_sha256="c" * 64,
    )
    return replace(base, **overrides)


def test_same_month_is_replaced_without_duplicate_growth() -> None:
    backend = FakeBackend()
    backend.live["raw"].remove("2022-01")
    backend.live["raw"].add("2026-01")
    before = len(backend.live["raw"])

    csd_publication.activate(_plan(), backend)

    assert len(backend.live["raw"]) == before
    assert "2026-01" in backend.live["raw"]


def test_activation_holds_lock_and_arms_recovery_before_publish() -> None:
    backend = FakeBackend()

    csd_publication.activate(_plan(), backend)

    assert backend.events == [
        "lock_acquired",
        "recovery_checked",
        "recovery_armed",
        "published",
        "refresh_verified",
        "complete",
        "lock_released",
    ]


def test_load_failure_preserves_previous_live_tables() -> None:
    backend = FakeBackend()
    before = {name: set(periods) for name, periods in backend.live.items()}
    backend.fail_load = True

    with pytest.raises(RuntimeError, match="load failure"):
        csd_publication.activate(_plan(), backend)

    assert backend.live == before
    assert backend.published is False


def test_windows_keep_48_raw_and_36_display_months() -> None:
    backend = FakeBackend()

    csd_publication.activate(_plan(), backend)

    assert len(backend.live["raw"]) == 48
    assert len(backend.live["stage"]) == 36
    assert min(backend.live["raw"]) == "2022-02"
    assert min(backend.live["stage"]) == "2023-02"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("builder_commit", "", "40-character"),
        ("image_digest", "", "sha256"),
        ("image_ref", "registry/image:latest", "immutable"),
        ("inventory_sha256", "", "inventory"),
    ],
)
def test_missing_or_unpinned_provenance_blocks_publication(
    field: str, value: str, message: str
) -> None:
    backend = FakeBackend()

    with pytest.raises(RuntimeError, match=message):
        csd_publication.activate(_plan(**{field: value}), backend)

    assert backend.published is False


def test_provenance_failure_rolls_back_and_is_not_success() -> None:
    backend = FakeBackend()
    before = {name: set(periods) for name, periods in backend.live.items()}
    backend.fail_provenance = True

    with pytest.raises(RuntimeError, match="provenance failure"):
        csd_publication.activate(_plan(), backend)

    assert backend.live == before
    assert backend.published is False


def test_refresh_failure_rolls_back_and_is_not_success() -> None:
    backend = FakeBackend()
    before = {name: set(periods) for name, periods in backend.live.items()}
    backend.fail_refresh = True

    with pytest.raises(RuntimeError, match="refresh failure"):
        csd_publication.activate(_plan(), backend)

    assert backend.live == before
    assert backend.published is False


def test_keyword_agent_handoff_is_explicitly_disabled() -> None:
    payload = csd_publication.agent_handoff(
        category="iqvia_csd_keyword",
        run_id="run-1",
        periods=("2026-01",),
    )

    assert payload == {
        "enabled": False,
        "category": "iqvia_csd_keyword",
        "run_id": "run-1",
        "period_from": "2026-01",
        "period_to": "2026-01",
        "dispatch": "disabled_separate_jw_agent_round",
    }


class _RecordingCursor:
    def __init__(self) -> None:
        self.statements: list[tuple[str, object | None]] = []
        self._fetchone = (1,)
        self.fetchone_values: list[tuple[object, ...]] = []
        self.fetchall_values: list[tuple[tuple[object, ...], ...]] = []
        self.rowcount = 1

    def execute(self, sql: str, params: object | None = None) -> None:
        self.statements.append((sql, params))

    def fetchone(self) -> tuple[object, ...]:
        if self.fetchone_values:
            return self.fetchone_values.pop(0)
        return self._fetchone

    def fetchall(self) -> tuple[tuple[object, ...], ...]:
        if self.fetchall_values:
            return self.fetchall_values.pop(0)
        return ()

    def close(self) -> None:
        return


class _RecordingConnection:
    def __init__(self) -> None:
        self.cursor_instance = _RecordingCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _RecordingCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class _RecordingJournal:
    def __init__(self) -> None:
        self.phases: list[tuple[str, str]] = []

    def mark(self, *, run_id: str, phase: str) -> None:
        self.phases.append((run_id, phase))


def _mariadb_backend_without_connect() -> csd_publication_backend.MariaDbBackend:
    backend = object.__new__(csd_publication_backend.MariaDbBackend)
    backend.contract = csd_publication_backend.DATASETS["iqvia_csd_channel"]
    backend.live_raw = "live_raw"
    backend.live_stage = "live_stage"
    backend.build_raw = "build_raw"
    backend.build_stage = "build_stage"
    backend.safe_run = "run_1"
    backend._backup_raw = "raw_csd_channel_dynamics__old_run_1"
    backend._backup_stage = "csd_channel_dynamics_stage__old_run_1"
    backend._lock_name = "jw_ingest_csd:iqvia_csd_channel"
    backend._source_period_counts = {"2026-01": 1}
    backend._expected_raw_counts = None
    backend._expected_stage_counts = None
    backend.rows = SimpleNamespace(
        csd=[SimpleNamespace(period_ym="2026-01")],
        keyword=[],
    )
    backend.conn = _RecordingConnection()
    backend.journal = _RecordingJournal()
    return backend


def test_publish_switches_raw_and_stage_with_one_rename_statement() -> None:
    backend = _mariadb_backend_without_connect()

    backend.publish(_plan())

    statements = backend.conn.cursor_instance.statements
    rename_statements = [
        statement for statement, _params in statements if statement.startswith("RENAME TABLE ")
    ]
    assert len(rename_statements) == 1
    assert "`live_raw`.`raw_csd_channel_dynamics`" in rename_statements[0]
    assert "`live_stage`.`csd_channel_dynamics_stage`" in rename_statements[0]
    assert backend.journal.phases == [("run-1", "published")]
    assert backend.conn.commits == 1


def test_rollback_restores_both_tables_and_removes_invalid_provenance() -> None:
    backend = _mariadb_backend_without_connect()
    plan = _plan()

    backend.rollback(plan, object())

    statements = backend.conn.cursor_instance.statements
    assert statements[0][0].startswith("RENAME TABLE ")
    assert "_failed_run_1" in statements[0][0]
    assert statements[1][0].startswith("SELECT COUNT(*) FROM information_schema")
    assert statements[2][0].startswith("DELETE FROM ")
    assert statements[2][1] == (plan.category, plan.run_id)
    assert backend.conn.commits == 1


def test_refresh_verification_requires_every_submitted_period() -> None:
    backend = _mariadb_backend_without_connect()
    backend._expected_raw_counts = {"2026-01": 1, "2026-02": 1}
    backend._expected_stage_counts = {"2026-01": 1, "2026-02": 1}
    backend.conn.cursor_instance.fetchall_values = [
        (("2026-01", 1),),
        (("2026-01", 1), ("2026-02", 1)),
    ]
    plan = _plan(incoming_periods=("2026-01", "2026-02"))

    with pytest.raises(RuntimeError, match="raw publication row counts"):
        backend.verify_refresh(plan)


def test_candidate_load_rejects_partial_submitted_period(monkeypatch) -> None:
    backend = _mariadb_backend_without_connect()
    backend._source_period_counts = {"2026-01": 2}
    backend.rows = SimpleNamespace(
        csd=[
            SimpleNamespace(period_ym="2026-01"),
            SimpleNamespace(period_ym="2026-01"),
        ],
        keyword=[],
    )
    backend.conn.cursor_instance.fetchall_values = [
        (("2026-01", 1),),
    ]
    monkeypatch.setattr(
        "pipeline.scripts.etl.brand_activity.raw_db.load_sources",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(RuntimeError, match="expected=2 actual=1"):
        backend.replace_periods(_plan(), ("2026-01",))


def test_refresh_verification_matches_full_candidate_period_counts() -> None:
    backend = _mariadb_backend_without_connect()
    backend._expected_raw_counts = {"2025-12": 4, "2026-01": 2}
    backend._expected_stage_counts = {"2025-12": 3, "2026-01": 1}
    backend.conn.cursor_instance.fetchall_values = [
        (("2025-12", 4), ("2026-01", 2)),
        (("2025-12", 3), ("2026-01", 1)),
    ]

    backend.verify_refresh(_plan())


def test_writer_lock_contention_fails_before_candidate_build() -> None:
    connection = _RecordingConnection()
    connection.cursor_instance.fetchone_values = [(0,)]
    journal = csd_activation_journal.ActivationJournal(
        connection,
        category="iqvia_csd_channel",
        live_raw="live_raw",
        live_stage="live_stage",
        raw_table="raw_csd_channel_dynamics",
        stage_table="csd_channel_dynamics_stage",
        backup_raw="raw_old",
        backup_stage="stage_old",
    )

    with pytest.raises(RuntimeError, match="already held"):
        journal.acquire()


def test_recover_restores_both_tables_after_interrupted_publish() -> None:
    connection = _RecordingConnection()
    connection.cursor_instance.fetchone_values = [
        (
            "previous_run",
            "published",
            "raw_csd_channel_dynamics__old_previous_run",
            "csd_channel_dynamics_stage__old_previous_run",
        ),
        (1,),
        (1,),
        (0,),
    ]
    journal = csd_activation_journal.ActivationJournal(
        connection,
        category="iqvia_csd_channel",
        live_raw="live_raw",
        live_stage="live_stage",
        raw_table="raw_csd_channel_dynamics",
        stage_table="csd_channel_dynamics_stage",
        backup_raw="raw_old",
        backup_stage="stage_old",
    )

    journal.recover()

    statements = connection.cursor_instance.statements
    rename_statements = [
        statement for statement, _params in statements if statement.startswith("RENAME TABLE ")
    ]
    assert len(rename_statements) == 1
    assert "raw_csd_channel_dynamics__old_previous_run" in rename_statements[0]
    assert "csd_channel_dynamics_stage__old_previous_run" in rename_statements[0]
    assert any(
        "SET phase = %s" in statement and params is not None and params[0] == "recovered"
        for statement, params in statements
    )


def test_recover_fails_closed_when_published_backups_are_missing() -> None:
    connection = _RecordingConnection()
    connection.cursor_instance.fetchone_values = [
        ("previous_run", "published", "raw_old", "stage_old"),
        (0,),
        (0,),
    ]
    journal = csd_activation_journal.ActivationJournal(
        connection,
        category="iqvia_csd_channel",
        live_raw="live_raw",
        live_stage="live_stage",
        raw_table="raw_csd_channel_dynamics",
        stage_table="csd_channel_dynamics_stage",
        backup_raw="raw_old",
        backup_stage="stage_old",
    )

    with pytest.raises(RuntimeError, match="lost both backup tables"):
        journal.recover()


def test_recover_accepts_armed_journal_before_backups_exist() -> None:
    connection = _RecordingConnection()
    connection.cursor_instance.fetchone_values = [
        ("previous_run", "armed", "raw_old", "stage_old"),
        (0,),
        (0,),
    ]
    journal = csd_activation_journal.ActivationJournal(
        connection,
        category="iqvia_csd_channel",
        live_raw="live_raw",
        live_stage="live_stage",
        raw_table="raw_csd_channel_dynamics",
        stage_table="csd_channel_dynamics_stage",
        backup_raw="raw_old",
        backup_stage="stage_old",
    )

    journal.recover()

    assert connection.commits == 1


def test_bounded_identifier_is_stable_and_within_mariadb_limit() -> None:
    first = bounded_id("jw_brand_activity_ingest", "x" * 90, "stage")
    second = bounded_id("jw_brand_activity_ingest", "x" * 90, "stage")
    different = bounded_id("jw_brand_activity_ingest", "y" * 90, "stage")

    assert first == second
    assert first != different
    assert len(first) <= 64
