from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from pipeline.scripts.analysis.brand_activity.auto_topic import row_topic_semantic_cli as cli
from pipeline.scripts.analysis.brand_activity.auto_topic.row_topic_assignment import (
    AssignmentInputRow,
    AssignmentParseError,
    RowTopicAssignment,
)
from pipeline.scripts.analysis.brand_activity.auto_topic import row_topic_semantic_runner as runner


GENERATION = "a" * 64


def _occurrence(row_id: int, *, brand: str) -> runner.SemanticOccurrence:
    return runner.SemanticOccurrence(
        stage_generation_id=GENERATION,
        stage_row_id=row_id,
        semantic_event_key_v1=f"{row_id:064x}",
        scope_id="atc4:C10AA",
        brand=brand,
    )


def test_cli_defaults_to_dry_run_and_execute_is_explicit() -> None:
    args = cli.parse_args(
        [
            "--stage-generation-id",
            GENERATION,
            "--topic-set-version",
            "topics-v1",
            "--wave-no",
            "1",
        ]
    )

    assert args.execute is False
    assert args.dry_run is True


def test_cli_rejects_call_cap_above_350() -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(
            [
                "--stage-generation-id",
                GENERATION,
                "--topic-set-version",
                "topics-v1",
                "--wave-no",
                "1",
                "--max-calls",
                "351",
            ]
        )


def test_wave_plan_is_deterministic_and_splits_350_plus_192() -> None:
    occurrences = tuple(
        _occurrence(row_id, brand=f"brand-{row_id:03d}") for row_id in range(1, 543)
    )
    inputs = (cli.TopicOccurrenceSet("topics-v1", occurrences),)

    first = cli.build_wave_plan(
        inputs,
        prompt_version="row_topic_v1",
        batch_size=150,
        max_calls=350,
    )
    repeated = cli.build_wave_plan(
        inputs,
        prompt_version="row_topic_v1",
        batch_size=150,
        max_calls=350,
    )

    assert first == repeated
    assert tuple(len(wave.batches) for wave in first.waves) == (350, 192)
    assert first.total_calls == 542
    assert all(item.batch.wave_no == 1 for item in first.waves[0].batches)
    assert all(item.batch.wave_no == 2 for item in first.waves[1].batches)


@dataclass
class _FakeOutcome:
    status: str
    calls_used: int
    assignment_rows: int = 0
    status_rows: int = 0


def test_completed_batches_are_skipped_without_duplicate_calls() -> None:
    plan = cli.build_wave_plan(
        (cli.TopicOccurrenceSet("topics-v1", (_occurrence(1, brand="A"),)),),
        prompt_version="row_topic_v1",
        batch_size=150,
        max_calls=350,
    )
    paid_calls = 0
    completed: set[str] = set()

    def execute_batch(item: cli.PlannedSemanticBatch) -> _FakeOutcome:
        nonlocal paid_calls
        if item.batch.batch_id in completed:
            return _FakeOutcome(status="complete", calls_used=0)
        paid_calls += 1
        completed.add(item.batch.batch_id)
        return _FakeOutcome(status="complete", calls_used=1)

    first = cli.execute_wave(plan.waves[0], execute_batch=execute_batch)
    repeated = cli.execute_wave(plan.waves[0], execute_batch=execute_batch)

    assert paid_calls == 1
    assert first.calls_used == 1
    assert repeated.calls_used == 0
    assert repeated.skipped_batches == 1


def test_semantic_identity_crossing_batch_boundary_is_classified_once() -> None:
    repeated_key = "f" * 64
    occurrences = tuple(
        runner.SemanticOccurrence(
            stage_generation_id=GENERATION,
            stage_row_id=row_id,
            semantic_event_key_v1=repeated_key if row_id in (150, 151) else f"{row_id:064x}",
            scope_id="atc4:A02B2",
            brand="K-CAB",
        )
        for row_id in range(1, 152)
    )
    batches = runner.build_semantic_batches(
        occurrences,
        topic_set_version="topics-v1",
        prompt_version="row_topic_v1",
        wave_no=1,
        batch_size=150,
    )
    first_result = runner.CanonicalSemanticResult(
        semantic_event_key_v1=repeated_key,
        scope_id="atc4:A02B2",
        topic_set_version="topics-v1",
        topic_ids=("T2", "T3", "T5"),
    )

    first = runner.select_semantic_work(
        batches[0], topic_set_version="topics-v1", existing_results=()
    )
    second = runner.select_semantic_work(
        batches[1], topic_set_version="topics-v1", existing_results=(first_result,)
    )

    assert first.classification_batch is not None
    assert repeated_key in {
        item.semantic_event_key_v1 for item in first.classification_batch.occurrences
    }
    assert second.classification_batch is None
    assert second.reused_occurrence_count == 1
    assert first.covered_occurrence_count + second.covered_occurrence_count == 151


def test_duplicate_occurrences_in_one_batch_use_one_representative_without_losing_coverage() -> None:
    repeated_key = "e" * 64
    occurrences = tuple(
        runner.SemanticOccurrence(
            stage_generation_id=GENERATION,
            stage_row_id=row_id,
            semantic_event_key_v1=repeated_key,
            scope_id="atc4:A02B2",
            brand="K-CAB",
        )
        for row_id in (11, 12, 13)
    )
    batch = runner.build_semantic_batches(
        occurrences,
        topic_set_version="topics-v1",
        prompt_version="row_topic_v1",
        wave_no=1,
        batch_size=150,
    )[0]

    selected = runner.select_semantic_work(
        batch, topic_set_version="topics-v1", existing_results=()
    )

    assert selected.classification_batch is not None
    assert tuple(item.stage_row_id for item in selected.classification_batch.occurrences) == (11,)
    assert selected.covered_occurrence_count == 3


def test_parse_failure_preserves_paid_call_count_and_continues() -> None:
    plan = cli.build_wave_plan(
        (
            cli.TopicOccurrenceSet("topics-v1", (_occurrence(1, brand="A"),)),
            cli.TopicOccurrenceSet("topics-v2", (_occurrence(2, brand="B"),)),
        ),
        prompt_version="row_topic_v1",
        batch_size=150,
        max_calls=350,
    )

    def execute_batch(item: cli.PlannedSemanticBatch) -> _FakeOutcome:
        if item.topic_set_version == "topics-v1":
            raise cli.SemanticResponseParseError("invalid response", calls_used=2)
        return _FakeOutcome(status="complete", calls_used=1)

    summary = cli.execute_wave(plan.waves[0], execute_batch=execute_batch)

    assert summary.failed_batches == 1
    assert summary.completed_batches == 1
    assert summary.calls_used == 3


def test_parse_failure_can_stop_wave_before_next_paid_batch() -> None:
    plan = cli.build_wave_plan(
        (
            cli.TopicOccurrenceSet("topics-v1", (_occurrence(1, brand="A"),)),
            cli.TopicOccurrenceSet("topics-v2", (_occurrence(2, brand="B"),)),
        ),
        prompt_version="row_topic_v1",
        batch_size=150,
        max_calls=350,
    )
    attempted: list[str] = []

    def execute_batch(item: cli.PlannedSemanticBatch) -> _FakeOutcome:
        attempted.append(item.topic_set_version)
        if item.topic_set_version == "topics-v1":
            raise cli.SemanticResponseParseError("invalid response", calls_used=1)
        return _FakeOutcome(status="complete", calls_used=1)

    with pytest.raises(cli.SemanticResponseParseError, match="invalid response"):
        cli.execute_wave(
            plan.waves[0],
            execute_batch=execute_batch,
            stop_on_response_parse=True,
        )

    assert attempted == ["topics-v1"]


def test_legacy_adapter_maps_occurrence_results_and_preserves_empty_topics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = runner.build_semantic_batches(
        (_occurrence(11, brand="A"), _occurrence(12, brand="A")),
        topic_set_version="topics-v1",
        prompt_version="row_topic_v1",
        wave_no=1,
        batch_size=150,
    )[0]
    adapter = cli.LegacyGenosSemanticAdapter.from_test_rows(
        topic_set_version="topics-v1",
        rows=tuple(
            AssignmentInputRow(
                row_id=item.stage_row_id,
                scope_id=item.scope_id,
                brand=item.brand,
                keyword_text=f"keyword-{item.stage_row_id}",
            )
            for item in batch.occurrences
        ),
        rubrics={(batch.scope_id, batch.brand): ()},
        classify_legacy=lambda *_args, **_kwargs: {
            "assignments": [
                RowTopicAssignment(
                    row_id=batch.occurrences[0].stage_row_id,
                    scope_id=batch.scope_id,
                    brand=batch.brand,
                    topic_id="T1",
                    topic_set_version="topics-v1",
                    prompt_version="row_topic_v1",
                    batch_id=batch.batch_id,
                )
            ],
            "calls": 1,
            "missing_row_ids": [],
        },
    )

    classified = adapter.classify(batch)

    assert classified.calls_used == 1
    assert classified.results == (
        runner.OccurrenceResult(stage_row_id=11, topic_ids=("T1",)),
        runner.OccurrenceResult(stage_row_id=12, topic_ids=()),
    )


@pytest.mark.parametrize(
    ("message", "expected"),
    (
        ("serving call failed: TimeoutError", cli.SemanticLlmTimeoutError),
        ("serving call failed: HTTP 503", cli.SemanticLlmCallError),
        ("response JSON is malformed", cli.SemanticResponseParseError),
    ),
)
def test_legacy_adapter_preserves_failure_kind(
    message: str,
    expected: type[Exception],
) -> None:
    batch = runner.build_semantic_batches(
        (_occurrence(11, brand="A"),),
        topic_set_version="topics-v1",
        prompt_version="row_topic_v1",
        wave_no=1,
        batch_size=150,
    )[0]

    def fail(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssignmentParseError(message)

    adapter = cli.LegacyGenosSemanticAdapter.from_test_rows(
        topic_set_version="topics-v1",
        rows=(
            AssignmentInputRow(
                row_id=11,
                scope_id=batch.scope_id,
                brand=batch.brand,
                keyword_text="keyword-11",
            ),
        ),
        rubrics={(batch.scope_id, batch.brand): ()},
        classify_legacy=fail,
    )

    with pytest.raises(expected, match=message):
        adapter.classify(batch)


def test_recording_client_retains_each_raw_response() -> None:
    class Client:
        def classify(self, _messages: list[dict[str, str]]) -> tuple[str, dict[str, int], int]:
            return '{"assignments": []}', {"total_tokens": 7}, 11

    client = cli.RecordingAssignmentClient(Client())

    result = client.classify([])

    assert result == ('{"assignments": []}', {"total_tokens": 7}, 11)
    assert client.responses == ('{"assignments": []}',)


def test_failed_response_log_redacts_secrets_and_uses_private_permissions(tmp_path: Path) -> None:
    path = tmp_path / "failed.jsonl"

    cli.append_failed_response_log(
        path,
        run_id="run-1",
        batch_id="batch-1",
        error_code="ImmutableResultConflict",
        responses=(
            'Bearer top-secret user@example.com {"api_key":"hidden","topic_id":"T2"}',
        ),
        recorded_at_utc_naive="2026-08-12 04:00:00.000000",
    )

    payload = path.read_text(encoding="utf-8")
    assert "top-secret" not in payload
    assert "user@example.com" not in payload
    assert "hidden" not in payload
    assert 'topic_id\\\":\\\"T2' in payload
    assert path.stat().st_mode & 0o777 == 0o600
