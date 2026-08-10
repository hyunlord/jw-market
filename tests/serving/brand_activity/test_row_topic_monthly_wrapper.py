from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pipeline.scripts.deploy.brand_activity_307 import row_topic_monthly_wrapper as wrapper


def test_runner_passes_canonical_affected_scope(monkeypatch) -> None:
    captured: list[str] = []

    def run(cmd: list[str], **_kwargs: object) -> SimpleNamespace:
        captured.extend(cmd)
        return SimpleNamespace(returncode=0, stdout='{"pending_rows":0}\n', stderr="")

    monkeypatch.setattr(wrapper.subprocess, "run", run)
    scope = {"dimension": "period_ym", "count": 2, "values": ["2025-09", "2025-10"]}

    wrapper._run_row_topic("dry-run", "topic-v1", affected_scope=scope, run_id="run-1")  # noqa: SLF001

    index = captured.index("--affected-scope-json")
    assert json.loads(captured[index + 1]) == scope
    assert "/tmp/row_topic_assignment_checkpoint_run-1.jsonl" in captured


def test_ingest_runner_requires_token_only_when_scope_has_pending_rows(monkeypatch) -> None:
    monkeypatch.setattr(wrapper, "_prepare_environment", lambda: None)
    monkeypatch.setattr(wrapper, "_latest_topic_set_version", lambda: "topic-v1")
    monkeypatch.setattr(wrapper, "_run_row_topic", lambda *_args, **_kwargs: {"pending_rows": 1})
    monkeypatch.delenv("GENOS_BEARER_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="GENOS_BEARER_TOKEN is required"):
        wrapper.run_for_ingest(
            affected_scope={"dimension": "period_ym", "count": 1, "values": ["2025-10"]},
            category="iqvia_csd_keyword",
            epoch="2025-10",
            manifest_sha="a" * 64,
            run_id="run-1",
        )


def test_ingest_runner_reports_no_target_without_token(monkeypatch) -> None:
    monkeypatch.setattr(wrapper, "_prepare_environment", lambda: None)
    monkeypatch.setattr(wrapper, "_latest_topic_set_version", lambda: "topic-v1")
    monkeypatch.setattr(wrapper, "_run_row_topic", lambda *_args, **_kwargs: {"pending_rows": 0})
    monkeypatch.delenv("GENOS_BEARER_TOKEN", raising=False)

    result = wrapper.run_for_ingest(
        affected_scope={"dimension": "period_ym", "count": 1, "values": ["2025-10"]},
        category="iqvia_csd_keyword",
        epoch="2025-10",
        manifest_sha="a" * 64,
        run_id="run-1",
    )

    assert result == wrapper.RowTopicRunResult(pending_rows=0, calls=0, inserts=0)
