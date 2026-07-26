from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread
from typing import Iterator

from scripts.f21_probe.cli import main
from scripts.f21_probe.models import (
    load_question_set,
    load_question_sets,
    question_set_counts,
)
from scripts.f21_probe.schema import QUESTION_ANSWER_FIELDS

PROJECT_ROOT = Path(__file__).resolve().parents[1]
QUESTION_SET = PROJECT_ROOT / "eval" / "f21_probe_questions.v1.json"
F59_QUESTION_SET = PROJECT_ROOT / "eval" / "f59_probe_questions.v1.json"


class _SseHandler(BaseHTTPRequestHandler):
    payloads: list[dict[str, str]] = []
    fail_on_question: str | None = None

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API.
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.payloads.append(payload)
        if payload["question"] == self.fail_on_question:
            raw = b"upstream unavailable"
            self.send_response(503)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        turn = len(self.payloads)
        raw = (
            "event: delta\n"
            f'data: "mock answer {turn}"\n\n'
            "event: timing\n"
            f'data: {{"total_elapsed_ms": {turn * 10}}}\n\n'
            "event: trace\n"
            "data: "
            + json.dumps(
                {
                    "trace_id": f"trace-{turn}",
                    "qa_trace": {
                        "request": {
                            "pod": "mock-pod",
                            "request_id": f"request-{turn}",
                        },
                        "final": {"disposition": "typed_unavailable"},
                        "tools": [{"name": "mock_tool"}],
                    },
                }
            )
            + "\n\n"
            "event: done\n"
            "data: {}\n\n"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args: object) -> None:
        return


@contextmanager
def _mock_sse_server() -> Iterator[str]:
    _SseHandler.payloads = []
    _SseHandler.fail_on_question = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SseHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _write_minimal_question_set(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": "chat_f21_question_set_v1",
                "defaults": {"repetitions": 1},
                "stages": [
                    {
                        "id": "T",
                        "directory": "stage_test",
                        "scenarios": [
                            {
                                "id": "shared_context",
                                "turns": [
                                    {"case_id": "T1", "question": "첫 질문"},
                                    {"case_id": "T2", "question": "후속 질문"},
                                ],
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_mock_http_dry_run_emits_f21_compatible_artifacts(tmp_path: Path) -> None:
    questions = tmp_path / "questions.json"
    output = tmp_path / "capture"
    _write_minimal_question_set(questions)

    with _mock_sse_server() as base_url:
        result = main(
            [
                "--question-set",
                str(questions),
                "--base-url",
                base_url,
                "--stream-path",
                "/stream",
                "--output",
                str(output),
                "--target-commit",
                "mock-commit",
                "--target-generation",
                "mock-generation",
                "--target-digest",
                "sha256:mock",
                "--interval-seconds",
                "0",
                "--request-timeout-seconds",
                "5",
            ]
        )

    assert result == 0
    first = json.loads((output / "stage_test" / "T1.json").read_text())
    second = json.loads((output / "stage_test" / "T2.json").read_text())
    assert QUESTION_ANSWER_FIELDS == set(first)
    assert first["question"] == "첫 질문"
    assert first["pod"] == "mock-pod"
    assert first["trace_id"] == "request-1"
    assert first["disposition"] == "typed_unavailable"
    assert first["tools_called"] == ["mock_tool"]
    assert first["answer_full"] == "mock answer 1"
    assert first["total_elapsed_ms"] == 10
    assert first["sse_raw"] == (output / first["sse_file"]).read_text()
    assert first["conversation_id"] == second["conversation_id"]
    assert _SseHandler.payloads[0]["conversationId"] == _SseHandler.payloads[1]["conversationId"]

    summary = json.loads((output / "capture_summary.json").read_text())
    assert summary["expected_question_answer_pairs"] == 2
    assert summary["captured_question_answer_pairs"] == 2
    assert summary["disposition_counts"] == {"typed_unavailable": 2}
    serialized = json.dumps(summary)
    assert '"verdict"' not in serialized
    assert '"passed"' not in serialized
    assert '"expected_answer"' not in serialized

    metadata = json.loads((output / "run_metadata.json").read_text())
    assert metadata["target"]["commit"] == "mock-commit"
    assert metadata["target"]["generation"] == "mock-generation"
    assert metadata["target"]["digest"] == "sha256:mock"


def test_committed_question_set_preserves_f21_population() -> None:
    question_set = load_question_set(QUESTION_SET)
    counts = question_set_counts(question_set)

    assert counts.question_answer_pairs == 80
    assert counts.multiturn_sets == 27
    assert counts.skipped_multiturn_sets == 3
    assert counts.stage_question_counts == {"A": 14, "B": 9, "C": 3, "D": 54}


def test_f59_question_set_carries_expectations_as_metadata_only() -> None:
    question_set = load_question_set(F59_QUESTION_SET)
    turns = [
        turn
        for stage in question_set.stages
        for scenario in stage.scenarios
        for turn in scenario.turns
    ]

    assert turns
    assert all(turn.expectations for turn in turns)
    assert any(
        "selected_data_path" in expectation
        for turn in turns
        for expectation in turn.expectations
    )


def test_multiple_question_sets_combine_without_changing_source_files() -> None:
    baseline = load_question_set(QUESTION_SET)
    extension = load_question_set(F59_QUESTION_SET)
    combined = load_question_sets((QUESTION_SET, F59_QUESTION_SET))

    assert combined.stages == baseline.stages + extension.stages
    assert question_set_counts(combined).question_answer_pairs == (
        question_set_counts(baseline).question_answer_pairs
        + question_set_counts(extension).question_answer_pairs
    )


def test_cli_accepts_v1_new_and_both_question_set_modes(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    _write_minimal_question_set(first)
    payload = json.loads(first.read_text(encoding="utf-8"))
    payload["stages"][0]["id"] = "U"
    payload["stages"][0]["directory"] = "stage_second"
    second.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    modes = {
        "v1": [first],
        "new": [second],
        "both": [first, second],
    }
    for mode, paths in modes.items():
        output = tmp_path / mode
        argv = [
            "--base-url",
            "unused",
            "--output",
            str(output),
            "--target-commit",
            "mock-commit",
            "--target-generation",
            "mock-generation",
            "--target-digest",
            "sha256:mock",
        ]
        for path in paths:
            argv.extend(["--question-set", str(path)])

        with _mock_sse_server() as base_url:
            argv[1] = base_url
            assert main(argv + ["--interval-seconds", "0"]) == 0

        rows = list(output.glob("stage_*/*.json"))
        answer_rows = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in rows
            if path.name != "scenario.json"
        ]
        assert len(answer_rows) == 2 * len(paths)
        assert all(set(row) == QUESTION_ANSWER_FIELDS for row in answer_rows)


def test_question_population_changes_by_data_only(tmp_path: Path) -> None:
    original = json.loads(QUESTION_SET.read_text(encoding="utf-8"))
    baseline = question_set_counts(load_question_set(QUESTION_SET))
    original["stages"][1]["scenarios"].append(
        {
            "id": "10_data_only_extension",
            "turns": [{"case_id": "10", "question": "데이터 파일로 추가한 질문"}],
        }
    )
    extended_path = tmp_path / "extended.json"
    extended_path.write_text(
        json.dumps(original, ensure_ascii=False),
        encoding="utf-8",
    )

    extended = question_set_counts(load_question_set(extended_path))

    assert extended.question_answer_pairs == baseline.question_answer_pairs + 1


def test_full_question_set_records_file_upload_skips_as_data(tmp_path: Path) -> None:
    original = json.loads(QUESTION_SET.read_text(encoding="utf-8"))
    file_scenario = original["stages"][3]["scenarios"][5]

    assert file_scenario["id"] == "06_file_inheritance"
    assert file_scenario["repetitions"] == 3
    assert file_scenario["requires"] == "file_upload"
    assert file_scenario["turns"] == []
    assert "No upload file" in file_scenario["skip_reason"]


def test_transport_failure_preserves_completed_and_error_artifacts(tmp_path: Path) -> None:
    questions = tmp_path / "questions.json"
    output = tmp_path / "capture"
    _write_minimal_question_set(questions)

    with _mock_sse_server() as base_url:
        _SseHandler.fail_on_question = "후속 질문"
        result = main(
            [
                "--question-set",
                str(questions),
                "--base-url",
                base_url,
                "--stream-path",
                "/stream",
                "--output",
                str(output),
                "--target-commit",
                "mock-commit",
                "--target-generation",
                "mock-generation",
                "--target-digest",
                "sha256:mock",
                "--interval-seconds",
                "0",
                "--request-timeout-seconds",
                "5",
            ]
        )

    assert result == 0
    assert (output / "stage_test" / "T1.sse").is_file()
    assert (output / "stage_test" / "T1.json").is_file()
    failed = json.loads((output / "stage_test" / "T2.json").read_text())
    assert failed["http_status"] == 503
    assert failed["error"]["type"] == "HTTPError"
    progress = json.loads((output / "progress.json").read_text())
    assert progress["captured_rows"] == 2
    assert progress["http_error_rows"] == 1
