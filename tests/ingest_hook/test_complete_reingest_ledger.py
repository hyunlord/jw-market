from __future__ import annotations

from dataclasses import asdict

import pytest


IDENTITY = ("2026-06", "ubist", "a" * 64)
OTHER_IDENTITY = ("2026-05", "ubist", "c" * 64)
MANIFEST_PATH = "_manifests/ubist/2026-06/manifest.json"
REQUEST_ID = "b6a8e00f-7717-4697-9230-e45192d5d7d2"
ATTEMPT_RUN_ID = "20260809221530123456"


def _complete_identity(sqlite_ledger, identity=IDENTITY) -> None:
    sqlite_ledger.receive(
        *identity,
        manifest_path=MANIFEST_PATH,
        uploaded_by="original@jw.example",
    )
    assert sqlite_ledger.mark_running(
        *identity,
        job_name="jw-ingest-ubist-original",
        run_id="20260809010101000000",
    )
    sqlite_ledger.mark_complete(*identity, row_counts={"epoch:2026-06": 137_836})


def _record_request(sqlite_ledger, **overrides):
    values = {
        "request_id": REQUEST_ID,
        "run_id": ATTEMPT_RUN_ID,
        "mode": "mart_from_existing_raw",
        "requested_by": "operator@jw.example",
        "reason": "MI Master definition changed",
        "affected_scope": {
            "dimension": "atc4",
            "count": 2,
            "values": ["C10A", "C10B"],
        },
        "code_revision": "d4565cee41dad177cc91e56d277ca23a01ed8e98",
        "image_digest": "sha256:" + "b" * 64,
    }
    values.update(overrides)
    return sqlite_ledger.record_complete_reingest_request(*IDENTITY, **values)


def test_complete_reingest_request_is_append_only_and_history_visible(sqlite_ledger) -> None:
    _complete_identity(sqlite_ledger)
    before = asdict(sqlite_ledger.status(*IDENTITY))

    decision = _record_request(sqlite_ledger)

    assert decision.created is True
    assert decision.request_id == REQUEST_ID
    assert decision.run_id == ATTEMPT_RUN_ID
    assert decision.manifest_path == MANIFEST_PATH
    assert asdict(sqlite_ledger.status(*IDENTITY)) == before

    transition = sqlite_ledger.status_transitions(*IDENTITY)[-1]
    assert transition.event_id == REQUEST_ID
    assert transition.previous_status == "complete"
    assert transition.status == "complete"
    assert transition.source == "complete_reingest_request"
    assert transition.actor == "operator@jw.example"
    assert transition.evidence == {
        "affected_scope": {
            "count": 2,
            "dimension": "atc4",
            "values": ["C10A", "C10B"],
        },
        "code_revision": "d4565cee41dad177cc91e56d277ca23a01ed8e98",
        "image_digest": "sha256:" + "b" * 64,
        "manifest_path": MANIFEST_PATH,
        "mode": "mart_from_existing_raw",
        "request_id": REQUEST_ID,
        "run_id": ATTEMPT_RUN_ID,
    }
    identities, _ = sqlite_ledger.history_identities(limit=100)
    assert ATTEMPT_RUN_ID in {identity.run_id for identity in identities}


def test_complete_reingest_request_is_idempotent_for_same_uuid(sqlite_ledger) -> None:
    _complete_identity(sqlite_ledger)
    first = _record_request(sqlite_ledger)
    transition_count = len(sqlite_ledger.status_transitions(*IDENTITY))

    second = _record_request(sqlite_ledger)

    assert first.created is True
    assert second.created is False
    assert second.run_id == first.run_id
    assert second.manifest_path == first.manifest_path
    assert len(sqlite_ledger.status_transitions(*IDENTITY)) == transition_count


def test_complete_reingest_request_allows_second_uuid_to_wait(sqlite_ledger) -> None:
    _complete_identity(sqlite_ledger)
    _record_request(sqlite_ledger)

    decision = _record_request(
        sqlite_ledger,
        request_id="d985181b-e8ab-4910-9138-4203f3054d1d",
        run_id="20260809223000000000",
    )

    assert decision.created is True
    assert [attempt.status for attempt in sqlite_ledger.complete_reingest_attempts()] == [
        "queued",
        "queued",
    ]


def test_complete_reingest_request_allows_same_category_attempt_to_wait(
    sqlite_ledger,
) -> None:
    _complete_identity(sqlite_ledger)
    _complete_identity(sqlite_ledger, OTHER_IDENTITY)
    _record_request(sqlite_ledger)

    decision = sqlite_ledger.record_complete_reingest_request(
        *OTHER_IDENTITY,
        request_id="d985181b-e8ab-4910-9138-4203f3054d1d",
        run_id="20260809223000000000",
        mode="mart_from_existing_raw",
        requested_by="operator@jw.example",
        reason="another completed period",
        affected_scope={
            "dimension": "atc4",
            "count": 1,
            "values": ["C10A"],
        },
    )

    assert decision.created is True


def test_complete_reingest_request_allows_new_uuid_after_terminal(sqlite_ledger) -> None:
    _complete_identity(sqlite_ledger)
    _record_request(sqlite_ledger)
    sqlite_ledger.record_complete_reingest_terminal(
        *IDENTITY,
        request_id=REQUEST_ID,
        run_id=ATTEMPT_RUN_ID,
        status="failed",
        reason="first attempt failed",
        actor="complete_reingest_runner",
        job_name="jw-complete-reingest-ubist-aabbccdd",
        affected_scope={
            "dimension": "atc4",
            "count": 2,
            "values": ["C10A", "C10B"],
        },
    )

    decision = _record_request(
        sqlite_ledger,
        request_id="d985181b-e8ab-4910-9138-4203f3054d1d",
        run_id="20260809223000000000",
    )

    assert decision.created is True


def test_complete_reingest_request_rejects_uuid_reuse_with_changed_contract(
    sqlite_ledger,
) -> None:
    _complete_identity(sqlite_ledger)
    _record_request(sqlite_ledger)

    with pytest.raises(ValueError, match="request UUID already belongs"):
        _record_request(sqlite_ledger, reason="different operator intent")


def test_complete_reingest_request_requires_complete_parent(sqlite_ledger) -> None:
    sqlite_ledger.receive(*IDENTITY, manifest_path=MANIFEST_PATH)
    before = asdict(sqlite_ledger.status(*IDENTITY))

    with pytest.raises(RuntimeError, match="parent identity must be complete"):
        _record_request(sqlite_ledger)

    assert asdict(sqlite_ledger.status(*IDENTITY)) == before
    assert all(
        transition.event_id != REQUEST_ID
        for transition in sqlite_ledger.status_transitions(*IDENTITY)
    )


def test_complete_reingest_terminal_is_append_only_and_idempotent(sqlite_ledger) -> None:
    _complete_identity(sqlite_ledger)
    _record_request(sqlite_ledger)
    before = asdict(sqlite_ledger.status(*IDENTITY))

    created = sqlite_ledger.record_complete_reingest_terminal(
        *IDENTITY,
        request_id=REQUEST_ID,
        run_id=ATTEMPT_RUN_ID,
        status="complete",
        reason="mart-only publish and scoped refresh completed",
        actor="complete_reingest_runner",
        job_name="jw-complete-reingest-ubist-aabbccdd",
        affected_scope={
            "dimension": "atc4",
            "count": 2,
            "values": ["C10A", "C10B"],
        },
    )
    replayed = sqlite_ledger.record_complete_reingest_terminal(
        *IDENTITY,
        request_id=REQUEST_ID,
        run_id=ATTEMPT_RUN_ID,
        status="complete",
        reason="mart-only publish and scoped refresh completed",
        actor="complete_reingest_runner",
        job_name="jw-complete-reingest-ubist-aabbccdd",
        affected_scope={
            "dimension": "atc4",
            "count": 2,
            "values": ["C10A", "C10B"],
        },
    )

    assert created is True
    assert replayed is False
    assert asdict(sqlite_ledger.status(*IDENTITY)) == before
    terminal = sqlite_ledger.status_transitions(*IDENTITY)[-1]
    assert terminal.source == "complete_reingest_terminal"
    assert terminal.status == "complete"
    assert terminal.job_name == "jw-complete-reingest-ubist-aabbccdd"
    assert terminal.evidence == {
        "affected_scope": {
            "count": 2,
            "dimension": "atc4",
            "values": ["C10A", "C10B"],
        },
        "attempt_status": "complete",
        "request_id": REQUEST_ID,
        "run_id": ATTEMPT_RUN_ID,
    }


def test_complete_reingest_terminal_rejects_scope_drift(sqlite_ledger) -> None:
    _complete_identity(sqlite_ledger)
    _record_request(sqlite_ledger)

    with pytest.raises(ValueError, match="affected_scope does not match"):
        sqlite_ledger.record_complete_reingest_terminal(
            *IDENTITY,
            request_id=REQUEST_ID,
            run_id=ATTEMPT_RUN_ID,
            status="failed",
            reason="scope drift",
            actor="complete_reingest_runner",
            job_name=None,
            affected_scope={
                "dimension": "atc4",
                "count": 1,
                "values": ["C10A"],
            },
        )


def test_complete_reingest_terminal_rejects_unknown_or_mismatched_request(
    sqlite_ledger,
) -> None:
    _complete_identity(sqlite_ledger)
    _record_request(sqlite_ledger)

    with pytest.raises(RuntimeError, match="complete reingest request does not exist"):
        sqlite_ledger.record_complete_reingest_terminal(
            *IDENTITY,
            request_id="0a162e07-5f10-46b7-8f84-8ea65130de56",
            run_id=ATTEMPT_RUN_ID,
            status="failed",
            reason="missing request",
            actor="complete_reingest_runner",
            job_name=None,
            affected_scope={"dimension": "source", "count": 1, "values": ["ubist"]},
        )


def test_complete_reingest_request_lookup_uses_configured_sql_marker(
    sqlite_ledger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _complete_identity(sqlite_ledger)
    _record_request(sqlite_ledger)
    statements: list[str] = []
    execute = sqlite_ledger._execute

    def capture(sql: str, params: tuple):
        statements.append(sql)
        return execute(sql, params)

    monkeypatch.setattr(sqlite_ledger, "_execute", capture)

    transition = sqlite_ledger.complete_reingest_request(
        *IDENTITY,
        request_id=REQUEST_ID,
    )

    assert transition is not None
    assert "?" in statements[-1]

    with pytest.raises(ValueError, match="run_id does not match"):
        sqlite_ledger.record_complete_reingest_terminal(
            *IDENTITY,
            request_id=REQUEST_ID,
            run_id="different-run",
            status="failed",
            reason="wrong run",
            actor="complete_reingest_runner",
            job_name=None,
            affected_scope={"dimension": "source", "count": 1, "values": ["ubist"]},
        )


def test_running_upload_blocks_complete_reingest_start(sqlite_ledger) -> None:
    # Given: a durable reingest request and an upload that already owns the slot.
    _complete_identity(sqlite_ledger)
    upload = ("2026-Q1", "iqvia_nsa", "d" * 64)
    sqlite_ledger.receive(*upload, manifest_path="/input/nsa.json")
    assert sqlite_ledger.claim_queued(
        *upload,
        job_name="jw-ingest-iqvia-nsa-dddddddd-run",
        run_id="upload-run",
    )
    _record_request(sqlite_ledger)

    # When: the reingest process attempts to reserve the same global slot.
    claimed = sqlite_ledger.record_complete_reingest_started(
        *IDENTITY,
        request_id=REQUEST_ID,
        run_id=ATTEMPT_RUN_ID,
        job_name="jw-complete-reingest-ubist-aaaaaaaa-run",
    )

    # Then: it remains queued instead of overlapping the upload.
    assert claimed is False
    assert sqlite_ledger.complete_reingest_attempts()[0].status == "queued"


def test_running_reingest_blocks_upload_claim(sqlite_ledger) -> None:
    # Given: a reingest attempt that owns the global slot.
    _complete_identity(sqlite_ledger)
    _record_request(sqlite_ledger)
    assert sqlite_ledger.record_complete_reingest_started(
        *IDENTITY,
        request_id=REQUEST_ID,
        run_id=ATTEMPT_RUN_ID,
        job_name="jw-complete-reingest-ubist-aaaaaaaa-run",
    )
    upload = ("2026-Q1", "iqvia_nsa", "d" * 64)
    sqlite_ledger.receive(*upload, manifest_path="/input/nsa.json")

    # When: the upload attempts to reserve the same slot.
    claimed = sqlite_ledger.claim_queued(
        *upload,
        job_name="jw-ingest-iqvia-nsa-dddddddd-run",
        run_id="upload-run",
    )

    # Then: the upload remains queued.
    assert claimed is False
    assert sqlite_ledger.status(*upload).status == "queued"


def test_mixed_fifo_prefers_older_upload_before_reingest(sqlite_ledger) -> None:
    # Given: an upload queued one second before a complete-reingest request.
    _complete_identity(sqlite_ledger)
    upload = ("2026-Q1", "iqvia_nsa", "d" * 64)
    sqlite_ledger.receive(*upload, manifest_path="/input/nsa.json")
    sqlite_ledger._conn.execute(
        "UPDATE ingest_ledger SET received_at='2026-08-10 00:00:00' "
        "WHERE epoch=? AND category=? AND manifest_sha=?",
        upload,
    )
    _record_request(sqlite_ledger)
    sqlite_ledger._conn.execute(
        "UPDATE ingest_status_transition SET created_at='2026-08-10 00:00:01' "
        "WHERE event_id=?",
        (REQUEST_ID,),
    )
    sqlite_ledger._conn.commit()

    # When: the younger reingest attempts to start first.
    reingest_claimed = sqlite_ledger.record_complete_reingest_started(
        *IDENTITY,
        request_id=REQUEST_ID,
        run_id=ATTEMPT_RUN_ID,
        job_name="jw-complete-reingest-ubist-aaaaaaaa-run",
    )

    # Then: FIFO rejects it and lets the older upload claim the slot.
    assert reingest_claimed is False
    assert sqlite_ledger.claim_queued(
        *upload,
        job_name="jw-ingest-iqvia-nsa-dddddddd-run",
        run_id="upload-run",
    )


def test_mixed_fifo_prefers_older_reingest_before_upload(sqlite_ledger) -> None:
    # Given: a complete-reingest request queued one second before an upload.
    _complete_identity(sqlite_ledger)
    _record_request(sqlite_ledger)
    sqlite_ledger._conn.execute(
        "UPDATE ingest_status_transition SET created_at='2026-08-10 00:00:00' "
        "WHERE event_id=?",
        (REQUEST_ID,),
    )
    upload = ("2026-Q1", "iqvia_nsa", "d" * 64)
    sqlite_ledger.receive(*upload, manifest_path="/input/nsa.json")
    sqlite_ledger._conn.execute(
        "UPDATE ingest_ledger SET received_at='2026-08-10 00:00:01' "
        "WHERE epoch=? AND category=? AND manifest_sha=?",
        upload,
    )
    sqlite_ledger._conn.commit()

    # When: the older reingest reserves the slot.
    reingest_claimed = sqlite_ledger.record_complete_reingest_started(
        *IDENTITY,
        request_id=REQUEST_ID,
        run_id=ATTEMPT_RUN_ID,
        job_name="jw-complete-reingest-ubist-aaaaaaaa-run",
    )

    # Then: it starts and the younger upload cannot overlap it.
    assert reingest_claimed is True
    assert sqlite_ledger.claim_queued(
        *upload,
        job_name="jw-ingest-iqvia-nsa-dddddddd-run",
        run_id="upload-run",
    ) is False
