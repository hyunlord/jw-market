from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service import sse_presenter
from jw_chat_agent_poc.service.app import FinalAnswer
from jw_chat_agent_poc.service.sse_presenter import iter_legacy_final_answer_events


def _answer(
    *,
    text: str = "본문",
    charts: list[dict] | None = None,
    tables: list[dict] | None = None,
    file_sources: tuple[dict, ...] = (),
) -> FinalAnswer:
    return FinalAnswer(
        text=text,
        charts=list(charts or ()),
        tables=list(tables or ()),
        timing={"stages": [{"name": "answer", "elapsed_ms": 1.0}]},
        trace={"qa_trace": {"status": "ok"}},
        sources=("cache", "document"),
        conversation_id="conv-phase4a",
        file_sources=file_sources,
    )


@pytest.mark.parametrize(
    "answer",
    (
        _answer(text="리바로 매출은 80.39억원입니다."),
        _answer(text="요청한 비교는 시장 정의가 달라 제한됩니다."),
        _answer(
            text="요약\n\n| 브랜드 | 매출 |\n|---|---:|\n| 리바로 | 80.39억원 |",
            charts=[{"type": "bar", "datasets": [{"label": "리바로", "data": [80.39]}]}],
            file_sources=({"file_name": "sales.csv", "document_id": 17},),
        ),
    ),
)
def test_presenter_sequence_is_byte_identical_to_legacy(monkeypatch, answer: FinalAnswer) -> None:
    monkeypatch.setenv(service_app.SSE_PRESENTER_ENV, "1")

    presented = list(service_app._sse_events_from_final_answer(answer))
    legacy = list(
        iter_legacy_final_answer_events(
            conversation_id=answer.conversation_id,
            source_labels=service_app.source_labels(answer.sources),
            file_sources=service_app._project_public_file_sources(answer.file_sources),
            text=answer.text,
            charts=answer.charts,
            tables=answer.tables,
            timing=answer.timing,
            trace=answer.trace,
        )
    )

    assert presented == legacy


def test_json_event_serializes_datetime_and_decimal_without_changing_plain_bytes() -> None:
    plain = {"status": "ok", "count": 3}
    expected_plain = 'event: trace\ndata: {"status":"ok","count":3}\n\n'

    assert sse_presenter.sse_json_event("trace", plain) == expected_plain
    encoded = sse_presenter.sse_json_event(
        "trace",
        {
            "retrieved_at": datetime(2026, 8, 11, 1, 2, 3, tzinfo=UTC),
            "exact_value": Decimal("85.8700"),
        },
    )

    assert '"retrieved_at":"2026-08-11T01:02:03+00:00"' in encoded
    assert '"exact_value":"85.8700"' in encoded


def test_markdown_table_is_one_atomic_block(monkeypatch) -> None:
    monkeypatch.setenv(service_app.SSE_PRESENTER_ENV, "1")
    events = list(
        service_app._sse_events_from_final_answer(
            _answer(text="| 브랜드 | 매출 |\n|---|---:|\n| 리바로 | 80.39억원 |")
        )
    )

    table_events = [event for event in events if event.startswith("event: markdown_block\n")]
    assert len(table_events) == 1
    payload = json.loads(table_events[0].split("data: ", 1)[1])
    assert payload == {
        "kind": "table",
        "markdown": "\n\n| 브랜드 | 매출 |\n|---|---:|\n| 리바로 | 80.39억원 |\n\n",
    }
    assert not any(event.startswith("event: delta\n") for event in events)


def test_grounded_tables_are_emitted_as_an_additive_sse_event() -> None:
    table = {
        "table_id": "v4-table-1",
        "title": "임상시험 상세",
        "source_label": "ClinicalTrials.gov",
        "columns": [
            {
                "key": "column_1",
                "label": "NCT ID",
                "type": "string",
                "unit": None,
                "align": "left",
            }
        ],
        "rows": [{"cells": {"column_1": "NCT00000001"}, "record_id": "clinical:NCT00000001"}],
        "row_count": 1,
        "omitted_columns": [],
    }

    events = list(service_app._sse_events_from_final_answer(_answer(tables=[table])))

    payload = next(event for event in events if event.startswith("event: tables\n"))
    assert json.loads(payload.split("data: ", 1)[1]) == [table]


def test_step_initial_and_busy_frames_match_legacy(monkeypatch) -> None:
    step_payload = {"index": 1, "name": "질문 접수", "status": "done"}

    monkeypatch.setenv(service_app.SSE_PRESENTER_ENV, "1")
    presented = {
        "step": service_app._sse_json_event("step", step_payload),
        "initial": list(
            service_app._sse_initial_text_events(
                conversation_id="conv-phase4a",
                sources=("cache",),
                text="본문",
            )
        ),
        "busy": list(service_app._sse_busy_events()),
    }

    monkeypatch.setenv(service_app.SSE_PRESENTER_ENV, "0")
    legacy = {
        "step": service_app._sse_json_event("step", step_payload),
        "initial": list(
            service_app._sse_initial_text_events(
                conversation_id="conv-phase4a",
                sources=("cache",),
                text="본문",
            )
        ),
        "busy": list(service_app._sse_busy_events()),
    }

    assert presented == legacy


def test_flag_off_uses_legacy_path(monkeypatch) -> None:
    monkeypatch.setenv(service_app.SSE_PRESENTER_ENV, "0")

    class UnexpectedPresenter:
        def final_answer_events(self, **_kwargs):
            raise AssertionError("presenter path must remain disabled")

    monkeypatch.setattr(sse_presenter, "_EXTRACTED_PRESENTER", UnexpectedPresenter())

    answer = _answer()
    assert list(service_app._sse_events_from_final_answer(answer)) == list(
        iter_legacy_final_answer_events(
            conversation_id=answer.conversation_id,
            source_labels=service_app.source_labels(answer.sources),
            file_sources=service_app._project_public_file_sources(answer.file_sources),
            text=answer.text,
            charts=answer.charts,
            timing=answer.timing,
            trace=answer.trace,
        )
    )


def test_presenter_failure_keeps_legacy_propagation_semantics(monkeypatch) -> None:
    monkeypatch.setenv(service_app.SSE_PRESENTER_ENV, "1")

    class BrokenPresenter:
        def final_answer_events(self, **_kwargs):
            raise RuntimeError("presenter failed")

    monkeypatch.setattr(sse_presenter, "_EXTRACTED_PRESENTER", BrokenPresenter())

    with pytest.raises(RuntimeError, match="presenter failed"):
        list(service_app._sse_events_from_final_answer(_answer()))
