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


def _semantic_result() -> csd_keyword_publish_runner.KeywordSemanticRefreshResult:
    return csd_keyword_publish_runner.KeywordSemanticRefreshResult(
        stage_generation_id="generation-1",
        bridge_inserted_rows=0,
        bridge_reused_rows=10,
        bridge_generation_rows=10,
        reused_semantic_identities=8,
        new_semantic_identities=0,
        planned_calls=0,
        llm_calls=0,
        estimated_usd=0.0,
        active_release_id="release-1",
        pointer_generation=1,
        pointer_changed=False,
    )


def test_assignment_runs_even_when_retired_flag_is_false(monkeypatch) -> None:
    ledger = Mock()
    runner = Mock(return_value=_semantic_result())
    monkeypatch.setattr(csd_keyword_publish_runner.config, "keyword_topic_assign_enabled", lambda: False)

    csd_keyword_publish_runner._record_topic_assignment(  # noqa: SLF001
        connection=Mock(),
        schema="jw_brand_activity_stage",
        ledger=ledger,
        identity=("2025-10", "iqvia_csd_keyword", "a" * 64),
        run_id="run-1",
        runner=runner,
    )

    runner.assert_called_once()
    assert ledger.record_stage.call_args.kwargs["status"] == "complete"
    assert ledger.record_stage.call_args.kwargs["duration_ms"] >= 1


def test_enabled_assignment_records_counts_and_exact_scope(monkeypatch) -> None:
    ledger = Mock()
    runner = Mock(return_value=_semantic_result())
    monkeypatch.setattr(csd_keyword_publish_runner.config, "keyword_topic_assign_enabled", lambda: True)
    ticks = iter((10.0, 10.125))
    monkeypatch.setattr(csd_keyword_publish_runner.time, "monotonic", lambda: next(ticks))
    csd_keyword_publish_runner._record_topic_assignment(  # noqa: SLF001
        connection=Mock(),
        schema="jw_brand_activity_stage",
        ledger=ledger,
        identity=("2025-10", "iqvia_csd_keyword", "a" * 64),
        run_id="run-1",
        runner=runner,
    )

    assert runner.call_args.kwargs["schema"] == "jw_brand_activity_stage"
    assert runner.call_args.kwargs["ingest_run_id"] == "run-1"
    assert ledger.record_stage.call_args.kwargs["duration_ms"] == 125


def test_assignment_failure_is_recorded_and_raised(monkeypatch) -> None:
    ledger = Mock()
    runner = Mock(side_effect=RuntimeError("GENOS_BEARER_TOKEN is required"))
    monkeypatch.setattr(csd_keyword_publish_runner.config, "keyword_topic_assign_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="GENOS_BEARER_TOKEN"):
        csd_keyword_publish_runner._record_topic_assignment(  # noqa: SLF001
            connection=Mock(),
            schema="jw_brand_activity_stage",
            ledger=ledger,
            identity=("2025-10", "iqvia_csd_keyword", "a" * 64),
            run_id="run-1",
            runner=runner,
        )

    assert ledger.record_stage.call_args.kwargs["status"] == "failed"
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
