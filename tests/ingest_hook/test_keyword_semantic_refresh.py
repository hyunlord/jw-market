from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pipeline.scripts.ingest_hook import keyword_semantic_refresh


def test_existing_semantic_results_backfill_without_llm(monkeypatch, capsys) -> None:
    connection = Mock()
    classify = Mock(side_effect=AssertionError("LLM must not run for reused identities"))
    monkeypatch.setattr(
        keyword_semantic_refresh,
        "_active_release_snapshot",
        lambda *_args, **_kwargs: keyword_semantic_refresh.ActiveReleaseSnapshot(
            pointer_name="brand_activity",
            release_id="release-old",
            generation=1,
            stage_generation_id="old-generation",
            manifest=(
                keyword_semantic_refresh.ManifestEntry(
                    scope_id="scope-1",
                    topic_set_version="topics-1",
                    assignment_contract="semantic_v1",
                    stage_generation_id="old-generation",
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        keyword_semantic_refresh,
        "backfill_current_generation",
        lambda *_args, **_kwargs: {
            "stage_generation_id": "new-generation",
            "inserted_rows": 10,
            "reused_rows": 0,
            "generation_rows": 10,
        },
    )
    monkeypatch.setattr(keyword_semantic_refresh, "_stage_row_count", lambda *_args, **_kwargs: 10)
    monkeypatch.setattr(
        keyword_semantic_refresh,
        "_missing_semantic_work",
        lambda *_args, **_kwargs: ((), 10),
    )
    monkeypatch.setattr(keyword_semantic_refresh, "_execute_missing_work", classify)
    publish = Mock(return_value=("release-new", 2))
    monkeypatch.setattr(keyword_semantic_refresh, "_publish_release", publish)

    result = keyword_semantic_refresh.refresh_keyword_semantic(
        connection,
        schema="jw_brand_activity_stage",
        ingest_run_id="ingest-1",
        created_by="test",
    )

    classify.assert_not_called()
    assert result.llm_calls == 0
    assert result.reused_semantic_identities == 10
    assert result.new_semantic_identities == 0
    assert result.bridge_inserted_rows == 10
    assert result.active_release_id == "release-new"
    assert result.pointer_generation == 2
    stdout = capsys.readouterr().out
    assert '"event": "keyword_semantic_zero_case"' in stdout
    assert '"actual_llm_calls": 0' in stdout


def test_refresh_rejects_estimated_cost_over_cap(monkeypatch) -> None:
    connection = Mock()
    monkeypatch.setattr(
        keyword_semantic_refresh,
        "_active_release_snapshot",
        lambda *_args, **_kwargs: keyword_semantic_refresh.ActiveReleaseSnapshot(
            pointer_name="brand_activity",
            release_id="release-old",
            generation=1,
            stage_generation_id="old-generation",
            manifest=(),
        ),
    )
    monkeypatch.setattr(
        keyword_semantic_refresh,
        "backfill_current_generation",
        lambda *_args, **_kwargs: {
            "stage_generation_id": "new-generation",
            "inserted_rows": 1,
            "reused_rows": 0,
            "generation_rows": 1,
        },
    )
    monkeypatch.setattr(keyword_semantic_refresh, "_stage_row_count", lambda *_args, **_kwargs: 1)
    monkeypatch.setattr(
        keyword_semantic_refresh,
        "_missing_semantic_work",
        lambda *_args, **_kwargs: ((SimpleNamespace(occurrences=(object(),)),), 0),
    )
    monkeypatch.setattr(
        keyword_semantic_refresh,
        "build_wave_plan",
        lambda *_args, **_kwargs: SimpleNamespace(estimated_usd=12.1),
    )

    with pytest.raises(RuntimeError, match="cost cap"):
        keyword_semantic_refresh.refresh_keyword_semantic(
            connection,
            schema="jw_brand_activity_stage",
            ingest_run_id="ingest-1",
            created_by="test",
            max_usd=12.0,
        )
