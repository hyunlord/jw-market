from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.scripts.ingest_hook import publish_approval


IDENTITY = publish_approval.PublishApprovalIdentity(
    epoch="2026-05",
    category="ubist",
    manifest_sha="a" * 64,
    run_id="run-a4",
)


def test_exact_publish_approval_waits_until_matching_file_exists(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    approval_path = tmp_path / "publish-approved.json"
    monkeypatch.setenv(publish_approval.ENV_REQUIRE_EXACT_APPROVAL, "1")
    monkeypatch.setenv(publish_approval.ENV_APPROVAL_FILE, str(approval_path))
    sleeps: list[float] = []

    def approve_after_first_poll(seconds: float) -> None:
        sleeps.append(seconds)
        approval_path.write_text(
            json.dumps(
                {
                    "approved": True,
                    "epoch": IDENTITY.epoch,
                    "category": IDENTITY.category,
                    "manifest_sha": IDENTITY.manifest_sha,
                    "run_id": IDENTITY.run_id,
                }
            ),
            encoding="utf-8",
        )

    publish_approval.wait_for_exact_publish_approval(
        IDENTITY, poll_seconds=0.25, sleeper=approve_after_first_poll
    )

    assert sleeps == [0.25]
    stdout = capsys.readouterr().out
    assert "status=waiting_for_pl_approval" in stdout
    assert "status=approval_verified" in stdout


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("epoch", "2026-04"),
        ("category", "iqvia_nsa"),
        ("manifest_sha", "a" * 63),
        ("run_id", "another-run"),
    ],
)
def test_exact_publish_approval_rejects_identity_mismatch(
    tmp_path: Path, monkeypatch, field: str, value: str
) -> None:
    approval_path = tmp_path / "publish-approved.json"
    payload = {
        "approved": True,
        "epoch": IDENTITY.epoch,
        "category": IDENTITY.category,
        "manifest_sha": IDENTITY.manifest_sha,
        "run_id": IDENTITY.run_id,
    }
    payload[field] = value
    approval_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv(publish_approval.ENV_REQUIRE_EXACT_APPROVAL, "1")
    monkeypatch.setenv(publish_approval.ENV_APPROVAL_FILE, str(approval_path))

    with pytest.raises(publish_approval.PublishApprovalError, match=field):
        publish_approval.wait_for_exact_publish_approval(IDENTITY)


def test_publish_approval_is_opt_in(monkeypatch) -> None:
    monkeypatch.delenv(publish_approval.ENV_REQUIRE_EXACT_APPROVAL, raising=False)
    monkeypatch.delenv(publish_approval.ENV_APPROVAL_FILE, raising=False)

    publish_approval.wait_for_exact_publish_approval(IDENTITY)
