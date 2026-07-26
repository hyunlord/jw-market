from __future__ import annotations

from types import SimpleNamespace

import pytest

from pipeline.scripts.ingest_hook import job_runner


def test_downstream_failure_restores_tables_then_refreshes_previous_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    spec = SimpleNamespace(key="iqvia_nsa")
    activation = SimpleNamespace(published=True)

    monkeypatch.setattr(
        "pipeline.scripts.ingest_hook.category_activation.restore",
        lambda result: events.append(("restore", result)),
    )
    monkeypatch.setattr(
        job_runner,
        "_refresh_category",
        lambda received: events.append(("refresh", received)),
    )

    job_runner._restore_category_after_downstream_failure(spec, activation)

    assert events == [("restore", activation), ("refresh", spec)]


def test_compensation_does_not_hide_restore_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = SimpleNamespace(key="iqvia_nsa")
    activation = SimpleNamespace(published=True)

    def fail_restore(_result) -> None:
        raise RuntimeError("restore failed")

    monkeypatch.setattr(
        "pipeline.scripts.ingest_hook.category_activation.restore",
        fail_restore,
    )
    monkeypatch.setattr(
        job_runner,
        "_refresh_category",
        lambda _received: pytest.fail("refresh must not run after restore failure"),
    )

    with pytest.raises(RuntimeError, match="restore failed"):
        job_runner._restore_category_after_downstream_failure(spec, activation)


@pytest.mark.parametrize(
    ("status", "publication_epoch", "expected"),
    (
        ("recorded", 42, True),
        ("planned", None, False),
        ("failed", None, False),
        ("recorded", None, False),
    ),
)
def test_only_durable_publication_is_a_compensation_boundary(
    status: str,
    publication_epoch: int | None,
    expected: bool,
) -> None:
    publication = SimpleNamespace(
        status=status,
        mart_publication_epoch=publication_epoch,
    )

    assert job_runner._publication_was_committed(publication) is expected
