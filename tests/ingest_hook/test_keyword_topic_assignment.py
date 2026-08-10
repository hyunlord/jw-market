from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from pipeline.scripts.ingest_hook import config, csd_keyword_publish_runner


def test_assignment_flag_defaults_off_and_is_strict(monkeypatch) -> None:
    monkeypatch.delenv("KEYWORD_TOPIC_ASSIGN_ENABLED", raising=False)
    assert config.keyword_topic_assign_enabled() is False
    monkeypatch.setenv("KEYWORD_TOPIC_ASSIGN_ENABLED", "true")
    assert config.keyword_topic_assign_enabled() is True
    monkeypatch.setenv("KEYWORD_TOPIC_ASSIGN_ENABLED", "maybe")
    with pytest.raises(RuntimeError, match="must be true or false"):
        config.keyword_topic_assign_enabled()


def test_disabled_assignment_records_truthful_null_duration(monkeypatch) -> None:
    ledger = Mock()
    runner = Mock()
    monkeypatch.setattr(csd_keyword_publish_runner.config, "keyword_topic_assign_enabled", lambda: False)

    csd_keyword_publish_runner._record_topic_assignment(  # noqa: SLF001
        ledger=ledger,
        identity=("2025-10", "iqvia_csd_keyword", "a" * 64),
        run_id="run-1",
        affected_scope={"dimension": "period_ym", "count": 1, "values": ["2025-10"]},
        runner=runner,
    )

    runner.assert_not_called()
    assert ledger.record_stage.call_args.kwargs["reason"] == "배정 비활성 (KEYWORD_TOPIC_ASSIGN_ENABLED=false)"
    assert ledger.record_stage.call_args.kwargs["duration_ms"] is None


def test_enabled_assignment_records_counts_and_exact_scope(monkeypatch) -> None:
    ledger = Mock()
    runner = Mock(return_value=SimpleNamespace(pending_rows=3, calls=2, inserts=5))
    monkeypatch.setattr(csd_keyword_publish_runner.config, "keyword_topic_assign_enabled", lambda: True)
    ticks = iter((10.0, 10.125))
    monkeypatch.setattr(csd_keyword_publish_runner.time, "monotonic", lambda: next(ticks))
    scope = {"dimension": "period_ym", "count": 1, "values": ["2025-10"]}

    csd_keyword_publish_runner._record_topic_assignment(  # noqa: SLF001
        ledger=ledger,
        identity=("2025-10", "iqvia_csd_keyword", "a" * 64),
        run_id="run-1",
        affected_scope=scope,
        runner=runner,
    )

    assert runner.call_args.kwargs["affected_scope"] == scope
    assert runner.call_args.kwargs["category"] == "iqvia_csd_keyword"
    assert ledger.record_stage.call_args.kwargs["reason"] == "배정 5건 생성 · LLM 호출 2회"
    assert ledger.record_stage.call_args.kwargs["duration_ms"] == 125


def test_assignment_failure_is_complete_and_does_not_raise(monkeypatch) -> None:
    ledger = Mock()
    runner = Mock(side_effect=RuntimeError("GENOS_BEARER_TOKEN is required"))
    monkeypatch.setattr(csd_keyword_publish_runner.config, "keyword_topic_assign_enabled", lambda: True)

    csd_keyword_publish_runner._record_topic_assignment(  # noqa: SLF001
        ledger=ledger,
        identity=("2025-10", "iqvia_csd_keyword", "a" * 64),
        run_id="run-1",
        affected_scope={"dimension": "period_ym", "count": 1, "values": ["2025-10"]},
        runner=runner,
    )

    assert ledger.record_stage.call_args.kwargs["status"] == "complete"
    assert ledger.record_stage.call_args.kwargs["reason"] == "배정 실패: RuntimeError: GENOS_BEARER_TOKEN is required"


def test_candidate_period_scope_is_sorted_and_exact() -> None:
    cursor = Mock()
    cursor.fetchall.return_value = ({"period_ym": "2025-10"}, {"period_ym": "2025-09"})
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    plan = SimpleNamespace(stage=SimpleNamespace(candidate=SimpleNamespace(schema="stage", table="candidate")))

    scope = csd_keyword_publish_runner._candidate_period_scope(connection, plan)  # noqa: SLF001

    assert scope == {"dimension": "period_ym", "count": 2, "values": ["2025-09", "2025-10"]}
    assert "ORDER BY period_ym" in cursor.execute.call_args.args[0]
