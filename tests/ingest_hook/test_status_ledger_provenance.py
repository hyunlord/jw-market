"""상태 API 가 어느 원장의 값을 답했는지 밝히는지 검증한다.

두 원장(리허설 sqlite · 운영 mart)이 같은 식별자를 다른 상태로 들고 있을 수 있다.
바인딩은 적재 출력 env 의 부수 효과라, '이 pod 이 연 원장' 과 '적재를 기록한 원장' 이
다를 수 있다. 응답은 어느 쪽 값인지와 다른 쪽이 뭐라 하는지를 함께 말해야 한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pipeline.scripts.ingest_hook import app as app_mod
from pipeline.scripts.ingest_hook import config
from pipeline.scripts.ingest_hook.app import IngestService, create_app
from pipeline.scripts.ingest_hook.ledger import open_sqlite_ledger

SHA = "9efc902bb5c3119dd94760f53e606c00d1b9a67b5ba288897ff90a27de3de311"
IDENTITY = {"epoch": "2026-03", "category": "ubist", "manifest_sha": SHA}


def _seed(path: Path, final_status: str | None):
    ledger = open_sqlite_ledger(path)
    if final_status is None:
        return ledger
    ledger.receive("2026-03", "ubist", SHA, "/nfs/x.json", uploaded_by="probe")
    if final_status == "queued":
        return ledger
    ledger.mark_running("2026-03", "ubist", SHA, job_name="job-x", run_id="run-x")
    if final_status == "complete":
        ledger.mark_complete("2026-03", "ubist", SHA, row_counts={"ingest_ledger": 1})
    elif final_status == "failed":
        ledger.mark_failed("2026-03", "ubist", SHA, reason="BackoffLimitExceeded")
    return ledger


@pytest.fixture
def bind(tmp_path, monkeypatch):
    """이 pod 을 shadow 에 묶고, d2 역할 원장을 주입한다 (라이브 trigger 와 같은 배치)."""

    def _bind(*, shadow_status: str | None, d2_status: str | None, d2_broken: bool = False):
        shadow_path = tmp_path / "shadow.sqlite"
        d2_path = tmp_path / "d2.sqlite"
        shadow = _seed(shadow_path, shadow_status)
        _seed(d2_path, d2_status)

        monkeypatch.setenv("INGEST_LOAD_SHADOW_ROOT", "/market-output/shadow")
        monkeypatch.setenv("INGEST_SHADOW_LEDGER_SQLITE", str(shadow_path))
        monkeypatch.delenv("INGEST_LOAD_STAGING_ROOT", raising=False)
        monkeypatch.delenv("INGEST_LOAD_TARGET_ROOT", raising=False)
        monkeypatch.delenv("INGEST_LEDGER_SQLITE", raising=False)

        real_open = config.open_ledger_by_source

        def fake_open(source: str):
            if source == "d2":
                if d2_broken:
                    raise RuntimeError("Can't connect to MySQL server (111)")
                return open_sqlite_ledger(d2_path)
            return real_open(source)

        monkeypatch.setattr(config, "open_ledger_by_source", fake_open)
        monkeypatch.setattr(app_mod.config, "open_ledger_by_source", fake_open)
        service = IngestService(shadow, tmp_path / "input")
        return TestClient(create_app(service))

    return _bind


def _get(client) -> tuple[int, dict]:
    response = client.get("/ingest/status", params=IDENTITY)
    return response.status_code, response.json()


def test_operational_ledger_answers_when_it_has_the_row(bind):
    client = bind(shadow_status="failed", d2_status="complete")
    code, body = _get(client)
    assert code == 200
    # 리허설 원장은 failed 라고 하지만 적재를 실제로 기록한 쪽은 d2 다.
    assert body["status"] == "complete"
    assert body["ledger_source"] == "d2"
    assert body["ledger_bound"] == "shadow"


def test_the_other_ledgers_disagreement_is_reported_not_hidden(bind):
    client = bind(shadow_status="failed", d2_status="complete")
    _, body = _get(client)
    # counterpart_* 는 ★답하지 않은 쪽을 가리켜야 한다. 답한 쪽을 두 번 적으면
    # 이 필드가 존재하는 이유인 불일치가 가려진다.
    assert body["counterpart_source"] == "shadow"
    assert body["counterpart_status"] == "failed"
    assert body["ledgers_agree"] is False


def test_row_present_only_in_the_operational_ledger_is_not_a_404(bind):
    client = bind(shadow_status=None, d2_status="queued")
    code, body = _get(client)
    assert code == 200
    assert body["status"] == "queued"
    assert body["ledger_source"] == "d2"


def test_row_present_only_in_the_bound_ledger_still_answers(bind):
    client = bind(shadow_status="failed", d2_status=None)
    code, body = _get(client)
    assert code == 200
    assert body["ledger_source"] == "shadow"
    assert body["counterpart_available"] is True
    assert body["counterpart_status"] is None


def test_unreadable_counterpart_is_unknown_not_absent(bind):
    client = bind(shadow_status="failed", d2_status=None, d2_broken=True)
    code, body = _get(client)
    assert code == 200
    # "모른다" 와 "없다" 가 같은 출구로 나가면 안 된다.
    assert body["counterpart_available"] is False
    assert "MySQL" in body["counterpart_error"]
    assert body["counterpart_status"] is None
    assert body["ledgers_agree"] is None


def test_absent_everywhere_says_which_ledger_could_not_be_read(bind):
    client = bind(shadow_status=None, d2_status=None, d2_broken=True)
    code, body = _get(client)
    assert code == 404
    assert "unreadable" in body["detail"]


def test_original_keys_and_types_are_unchanged(bind):
    client = bind(shadow_status="failed", d2_status="complete")
    _, body = _get(client)
    for key in (
        "epoch", "category", "manifest_sha", "status", "reason", "job_name",
        "uploaded_by", "received_at", "finished_at",
    ):
        assert key in body, key
    assert isinstance(body["status"], str)
    assert isinstance(body["epoch"], str)


def _configure_reverted_binding(monkeypatch, *, shadow_ledger: Path | None) -> None:
    monkeypatch.setenv("INGEST_LOAD_STAGING_ROOT", "/tmp/ingest-load-staging")
    monkeypatch.delenv("INGEST_LOAD_SHADOW_ROOT", raising=False)
    monkeypatch.delenv("INGEST_LOAD_TARGET_ROOT", raising=False)
    monkeypatch.delenv("INGEST_LEDGER_SQLITE", raising=False)
    if shadow_ledger is None:
        monkeypatch.delenv("INGEST_SHADOW_LEDGER_SQLITE", raising=False)
    else:
        monkeypatch.setenv("INGEST_SHADOW_LEDGER_SQLITE", str(shadow_ledger))


def test_revert_option_a_binds_d2_and_keeps_shadow_counterpart(
    tmp_path: Path, monkeypatch
) -> None:
    d2 = _seed(tmp_path / "d2.sqlite", "complete")
    shadow_path = tmp_path / "shadow.sqlite"
    _seed(shadow_path, "failed")
    _configure_reverted_binding(monkeypatch, shadow_ledger=shadow_path)

    client = TestClient(create_app(IngestService(d2, tmp_path / "input")))
    code, body = _get(client)

    assert config.configured_ledger_source() == "d2"
    assert config.counterpart_ledger_source() == "shadow"
    assert code == 200
    assert body["ledger_source"] == "d2"
    assert body["counterpart_source"] == "shadow"
    assert body["counterpart_available"] is True
    assert body["counterpart_status"] == "failed"


def test_revert_option_b_reports_unconfigured_counterpart_without_404(
    tmp_path: Path, monkeypatch
) -> None:
    d2 = _seed(tmp_path / "d2.sqlite", "complete")
    _configure_reverted_binding(monkeypatch, shadow_ledger=None)

    client = TestClient(create_app(IngestService(d2, tmp_path / "input")))
    code, body = _get(client)

    assert config.configured_ledger_source() == "d2"
    assert config.counterpart_ledger_source() is None
    assert code == 200
    assert body["ledger_source"] == "d2"
    assert body["counterpart_source"] is None
    assert body["counterpart_available"] is False
    assert body["counterpart_error"] == "counterpart ledger is not configured"
    assert body["counterpart_status"] is None
    assert body["ledgers_agree"] is None


def test_shadow_root_without_shadow_ledger_fails_closed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("INGEST_LOAD_SHADOW_ROOT", "/market-output/shadow")
    monkeypatch.delenv("INGEST_LOAD_STAGING_ROOT", raising=False)
    monkeypatch.delenv("INGEST_LOAD_TARGET_ROOT", raising=False)
    monkeypatch.delenv("INGEST_SHADOW_LEDGER_SQLITE", raising=False)
    monkeypatch.delenv("INGEST_LEDGER_SQLITE", raising=False)

    assert config.configured_ledger_source() == "shadow"
    with pytest.raises(
        RuntimeError,
        match="shadow mode requires INGEST_SHADOW_LEDGER_SQLITE",
    ):
        config.open_configured_ledger()
