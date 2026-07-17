from __future__ import annotations

import json
from pathlib import Path

from scripts.fact_scoreboard.sse import parse_sse_file
from scripts.parity_harness import (
    CHANNEL_PARAPHRASE_QUESTIONS,
    FRESH_GOLDEN_QUESTIONS,
    HISTORY_GOLDEN_QUESTIONS,
    MODE_TRANSITION_GOLDEN_QUESTIONS,
    _history_golden_acceptance,
    _capture_questions,
    _http_sse,
    capture_p0g_suite,
    diff_captures,
)
from scripts.runtime_model_compare_runner import _parse_events


def _write_capture(root: Path, answer: str, *, fact_value: str = "1억원", chart_value: int = 1) -> None:
    for child in ("sse", "markdown", "traces"):
        (root / child).mkdir(parents=True)
    (root / "sse" / "Q01.sse").write_text(
        f"event: delta\ndata: {answer}\n\nevent: done\ndata: ok\n\n",
        encoding="utf-8",
    )
    (root / "markdown" / "Q01.md").write_text(answer, encoding="utf-8")
    trace = {
        "result": {
            "decomposition": [{"intent": "issue_context"}],
            "router_diagnostics": {"mode": "agent_loop"},
            "tool_calls": [{"tool": "search_news"}],
            "markdown_response": {
                "fact_md": f"| 항목 | 값 |\n| --- | --- |\n| 매출 | {fact_value} |",
                "sources_md": "## 출처",
                "notice_md": "",
            },
        }
    }
    (root / "traces" / "Q01.json").write_text(json.dumps(trace, ensure_ascii=False), encoding="utf-8")
    (root / "sse" / "Q01.sse").write_text(
        f"event: charts\ndata: [{{\"title\":\"매출 추이\",\"labels\":[\"2026-04\"],\"datasets\":[{{\"label\":\"매출\",\"data\":[{chart_value}]}}]}}]\n\n"
        f"event: delta\ndata: {answer}\n\nevent: done\ndata: ok\n\n",
        encoding="utf-8",
    )
    for qid in ("Q02", "Q04", "Q05", "Q06", "Q07", "Q08", "Q09", "Q10", "Q11"):
        (root / "sse" / f"{qid}.sse").write_text("event: done\ndata: ok\n\n", encoding="utf-8")
        (root / "markdown" / f"{qid}.md").write_text("", encoding="utf-8")
        (root / "traces" / f"{qid}.json").write_text(json.dumps({"result": {}}, ensure_ascii=False), encoding="utf-8")


def test_parity_harness_passes_identical_capture(tmp_path: Path) -> None:
    capture = tmp_path / "capture"
    _write_capture(capture, "리바로 매출은 1억원입니다.")

    assert diff_captures(capture, capture, tmp_path / "report") == 0


def test_parity_harness_registers_channel_paraphrases() -> None:
    questions = {question for _, question in CHANNEL_PARAPHRASE_QUESTIONS}

    assert "리바로 채널별로 보여줘" in questions
    assert "리바로 채널" in questions
    assert "리바로 의원/병원별 실적" in questions
    assert _capture_questions("channel") == CHANNEL_PARAPHRASE_QUESTIONS


def test_parity_harness_registers_fresh_goldens() -> None:
    assert _capture_questions("fresh") == FRESH_GOLDEN_QUESTIONS
    assert FRESH_GOLDEN_QUESTIONS == (
        ("F01", "2025년 2분기 매출 얼마야"),
        ("F02", "고지혈증 시장 상위 5개 브랜드 알려줘"),
        ("F03", "리바로 최근 매출 추이 어때"),
    )


def test_parity_harness_registers_history_goldens() -> None:
    assert _capture_questions("history") == HISTORY_GOLDEN_QUESTIONS
    assert HISTORY_GOLDEN_QUESTIONS[-2:] == (
        ("H02", "2025년 2분기 매출 얼마야"),
        ("H03", "고지혈증 시장 상위 5개 브랜드 알려줘"),
    )


def test_parity_harness_registers_mode_transition_goldens() -> None:
    assert _capture_questions("mode-transition") == MODE_TRANSITION_GOLDEN_QUESTIONS
    assert MODE_TRANSITION_GOLDEN_QUESTIONS == (
        ("M01", "/deep 리바로 경쟁구도"),
        ("M02", "2025년 2분기 매출 얼마야"),
        ("M03", "고지혈증 시장 상위 5개 브랜드 알려줘"),
    )


def test_http_sse_forwards_shared_conversation_id(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        text = "event: done\ndata: ok\n\n"

        @staticmethod
        def raise_for_status() -> None:
            return None

    def get(url, *, params, timeout):
        captured.update(url=url, params=params, timeout=timeout)
        return Response()

    monkeypatch.setattr("scripts.parity_harness.requests.get", get)

    payload = _http_sse(
        "http://chat.example",
        "2025년 2분기 매출 얼마야",
        "live",
        conversation_id="dirty-session",
    )

    assert payload == "event: done\ndata: ok\n\n"
    assert captured["params"] == {
        "question": "2025년 2분기 매출 얼마야",
        "external_mode": "live",
        "conversation_id": "dirty-session",
    }


def test_history_golden_acceptance_requires_live_values() -> None:
    assert _history_golden_acceptance("H01", "도구 조회가 끝났습니다.") == (True, "")
    assert _history_golden_acceptance("H02", "2025-Q2 리바로 매출은 242.72억원입니다.") == (True, "")
    assert _history_golden_acceptance("H03", "상위 5개 합계 시장점유율은 29.52%입니다.") == (True, "")

    assert _history_golden_acceptance("H02", "데이터 존재 여부를 확인하지 못했습니다.") == (
        False,
        "missing 242.72억원",
    )
    assert _history_golden_acceptance("H03", "지원되지 않는 시장입니다.") == (
        False,
        "missing 29.52%",
    )
    assert _history_golden_acceptance("M02", "2025-Q2 리바로 매출은 242.72억원입니다.") == (True, "")
    assert _history_golden_acceptance("M03", "상위 5개 합계 시장점유율은 29.52%입니다.") == (True, "")
    assert _history_golden_acceptance("F01", "2025-Q2 리바로 매출은 242.72억원입니다.") == (True, "")
    assert _history_golden_acceptance("F02", "상위 5개 합계 시장점유율은 29.52%입니다.") == (True, "")


def test_p0g_suite_runs_all_portal_equivalent_scenarios(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[Path, str | None, str | None, tuple[tuple[str, str], ...], str | None]] = []

    def fake_capture(out_dir, external_mode, base_url, questions, conversation_id):
        calls.append((out_dir, external_mode, base_url, questions, conversation_id))
        out_dir.mkdir(parents=True)
        (out_dir / "summary.json").write_text(
            json.dumps([{"qid": qid, "elapsed_ms": 100.0} for qid, _ in questions]),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr("scripts.parity_harness.capture", fake_capture)
    monkeypatch.setattr(
        "scripts.parity_harness._probe_uploaded_file_session",
        lambda base_url, conversation_id, workflow_id: (True, 2, ""),
    )

    status = capture_p0g_suite(
        tmp_path,
        "live",
        "http://portal-equivalent",
        history_conversation_id="uploaded-file-session",
        portal_equivalent=True,
        file_base_url="http://code-serving-235",
    )

    assert status == 0
    assert [call[0].name for call in calls] == ["fresh", "history", "mode-transition"]
    assert [call[3] for call in calls] == [
        FRESH_GOLDEN_QUESTIONS,
        HISTORY_GOLDEN_QUESTIONS,
        MODE_TRANSITION_GOLDEN_QUESTIONS,
    ]
    assert calls[0][4] is None
    assert calls[1][4] == "uploaded-file-session"
    assert calls[2][4] is not None


def test_p0g_suite_requires_nonempty_file_bridge_documents(monkeypatch, tmp_path: Path) -> None:
    def fake_capture(out_dir, external_mode, base_url, questions, conversation_id):
        out_dir.mkdir(parents=True)
        (out_dir / "summary.json").write_text(
            json.dumps([{"qid": qid, "elapsed_ms": 100.0} for qid, _ in questions]),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr("scripts.parity_harness.capture", fake_capture)
    monkeypatch.setattr(
        "scripts.parity_harness._probe_uploaded_file_session",
        lambda base_url, conversation_id, workflow_id: (False, 0, "no documents"),
    )

    assert capture_p0g_suite(
        tmp_path,
        "live",
        "http://portal-equivalent",
        history_conversation_id="uploaded-file-session",
        portal_equivalent=True,
        file_base_url="http://code-serving-235",
    ) == 1
    summary = json.loads((tmp_path / "p0g_summary.json").read_text(encoding="utf-8"))
    assert summary["evidence_context"]["file_probe"] == {
        "attempted": True,
        "document_count": 0,
        "passed": False,
        "error": "no documents",
    }
    assert summary["qualification_failures"] == [
        "uploaded-file history session probe failed: no documents",
    ]


def test_probe_uploaded_file_session_uses_235_documents_contract(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict[str, object]:
            return {"documents": [{"file_name": "a.xlsx"}, {"file_name": "b.pdf"}]}

    def get(url, *, params, timeout):
        captured.update(url=url, params=params, timeout=timeout)
        return Response()

    monkeypatch.setattr("scripts.parity_harness.requests.get", get)

    from scripts.parity_harness import _probe_uploaded_file_session

    assert _probe_uploaded_file_session("http://code-serving-235/", "conv-file", 301) == (True, 2, "")
    assert captured == {
        "url": "http://code-serving-235/documents",
        "params": {"workflow_id": 301, "app_session_id": "conv-file", "chat_id": "conv-file"},
        "timeout": 10,
    }


def test_p0g_suite_rejects_diagnostic_only_capture_as_release_evidence(monkeypatch, tmp_path: Path) -> None:
    def fake_capture(out_dir, external_mode, base_url, questions, conversation_id):
        out_dir.mkdir(parents=True)
        (out_dir / "summary.json").write_text(
            json.dumps([{"qid": qid, "elapsed_ms": 100.0} for qid, _ in questions]),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr("scripts.parity_harness.capture", fake_capture)

    assert capture_p0g_suite(tmp_path, "live", "http://direct-chat") == 1
    summary = json.loads((tmp_path / "p0g_summary.json").read_text(encoding="utf-8"))
    assert summary["evidence_context"] == {
        "transport": "direct-chat-sse",
        "portal_equivalent_declared": False,
        "history_conversation_id_supplied": False,
        "file_probe": {
            "attempted": False,
            "document_count": 0,
            "passed": False,
            "error": "",
        },
    }
    assert summary["qualification_failures"] == [
        "portal-equivalent entry path was not declared",
        "uploaded-file history conversation ID was not supplied",
    ]


def test_p0g_suite_rejects_local_capture_declared_as_portal_equivalent(monkeypatch, tmp_path: Path) -> None:
    def fake_capture(out_dir, external_mode, base_url, questions, conversation_id):
        out_dir.mkdir(parents=True)
        (out_dir / "summary.json").write_text(
            json.dumps([{"qid": qid, "elapsed_ms": 100.0} for qid, _ in questions]),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr("scripts.parity_harness.capture", fake_capture)

    assert capture_p0g_suite(
        tmp_path,
        "live",
        None,
        history_conversation_id="uploaded-file-session",
        portal_equivalent=True,
    ) == 1
    summary = json.loads((tmp_path / "p0g_summary.json").read_text(encoding="utf-8"))
    assert summary["qualification_failures"] == [
        "portal-equivalent evidence requires a deployed base URL",
        "file bridge base URL was not supplied",
    ]


def test_p0g_suite_fails_when_any_scenario_fails(monkeypatch, tmp_path: Path) -> None:
    statuses = iter((0, 1, 0))

    def fake_capture(out_dir, external_mode, base_url, questions, conversation_id):
        out_dir.mkdir(parents=True)
        (out_dir / "summary.json").write_text(
            json.dumps([{"qid": qid, "elapsed_ms": 100.0} for qid, _ in questions]),
            encoding="utf-8",
        )
        return next(statuses)

    monkeypatch.setattr("scripts.parity_harness.capture", fake_capture)

    assert capture_p0g_suite(tmp_path, "live", None) == 1


def test_p0g_suite_fails_when_general_golden_exceeds_fast_path_budget(monkeypatch, tmp_path: Path) -> None:
    def fake_capture(out_dir, external_mode, base_url, questions, conversation_id):
        out_dir.mkdir(parents=True)
        rows = [
            {"qid": qid, "elapsed_ms": 10_001.0 if qid == "H02" else 100.0}
            for qid, _ in questions
        ]
        (out_dir / "summary.json").write_text(json.dumps(rows), encoding="utf-8")
        return 0

    monkeypatch.setattr("scripts.parity_harness.capture", fake_capture)

    assert capture_p0g_suite(tmp_path, "live", None, max_general_elapsed_ms=10_000.0) == 1
    summary = json.loads((tmp_path / "p0g_summary.json").read_text(encoding="utf-8"))
    assert summary["scenarios"][1]["latency_failures"] == ["H02"]


def test_p0g_suite_fails_when_general_golden_runs_contaminated_routes(monkeypatch, tmp_path: Path) -> None:
    def fake_capture(out_dir, external_mode, base_url, questions, conversation_id):
        out_dir.mkdir(parents=True)
        rows = []
        for qid, _ in questions:
            steps = (
                [{"name": "질문 접수"}, {"name": "첨부 파일 확인"}, {"name": "시장 데이터 조회"}]
                if qid != "H02"
                else [
                    {"name": "질문 접수"},
                    {"name": "첨부 파일 확인"},
                    {"name": "첨부 문서 조회"},
                    {"name": "임상 데이터 조회"},
                ]
            )
            rows.append({"qid": qid, "elapsed_ms": 100.0, "steps": steps})
        (out_dir / "summary.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return 0

    monkeypatch.setattr("scripts.parity_harness.capture", fake_capture)

    assert capture_p0g_suite(tmp_path, "live", None) == 1
    summary = json.loads((tmp_path / "p0g_summary.json").read_text(encoding="utf-8"))
    assert summary["scenarios"][1]["route_contamination_failures"] == {
        "H02": ["첨부 문서 조회", "임상 데이터 조회"],
    }


def test_parity_harness_allows_text_variation_when_numbers_are_grounded(tmp_path: Path) -> None:
    before = tmp_path / "before"
    after = tmp_path / "after"
    after_changed = tmp_path / "after_changed"
    _write_capture(before, "리바로 매출은 1억원입니다.")
    _write_capture(after, "리바로는 1억원의 매출을 기록했습니다.")

    assert diff_captures(before, after, tmp_path / "report") == 0
    report = json.loads((tmp_path / "report" / "parity_report.json").read_text(encoding="utf-8"))
    q01 = next(item for item in report["questions"] if item["qid"] == "Q01")
    assert q01["checks"]["L3_answer"] is True
    assert q01["checks"]["L4_numbers"] is True


def test_parity_harness_detects_ungrounded_answer_number(tmp_path: Path) -> None:
    before = tmp_path / "before"
    after = tmp_path / "after"
    _write_capture(before, "리바로 매출은 1억원입니다.")
    _write_capture(after, "리바로 매출은 2억원입니다.")

    assert diff_captures(before, after, tmp_path / "report") == 1
    report = json.loads((tmp_path / "report" / "parity_report.json").read_text(encoding="utf-8"))
    q01 = next(item for item in report["questions"] if item["qid"] == "Q01")
    assert q01["checks"]["L3_answer"] is False
    assert q01["checks"]["L4_numbers"] is False


def test_parity_harness_allows_extra_chart_presence_but_checks_common_values(tmp_path: Path) -> None:
    before = tmp_path / "before"
    after = tmp_path / "after"
    after_changed = tmp_path / "after_changed"
    _write_capture(before, "리바로 매출은 1억원입니다.", chart_value=1)
    _write_capture(after, "리바로는 1억원의 매출을 기록했습니다.", chart_value=1)
    (after / "sse" / "Q01.sse").write_text(
        (after / "sse" / "Q01.sse").read_text(encoding="utf-8")
        + "event: charts\ndata: [{\"title\":\"보조 차트\",\"labels\":[\"2026-04\"],\"datasets\":[{\"label\":\"매출\",\"data\":[1]}]}]\n\n",
        encoding="utf-8",
    )

    assert diff_captures(before, after, tmp_path / "report") == 0

    _write_capture(after_changed, "리바로는 1억원의 매출을 기록했습니다.", chart_value=2)

    assert diff_captures(before, after_changed, tmp_path / "report_changed") == 1
    report = json.loads((tmp_path / "report_changed" / "parity_report.json").read_text(encoding="utf-8"))
    q01 = next(item for item in report["questions"] if item["qid"] == "Q01")
    assert q01["checks"]["L2_fact"] is False


def test_sse_parser_appends_markdown_block_events(tmp_path: Path) -> None:
    raw = (
        "event: delta\n"
        "data: 채널 표입니다.\n\n"
        "event: markdown_block\n"
        "data: {\"kind\":\"table\",\"markdown\":\"\\n\\n| 채널 | 매출 |\\n| --- | --- |\\n| 의원 | 41.93억원 |\\n\\n\"}\n\n"
        "event: done\n"
        "data: ok\n\n"
    )
    path = tmp_path / "block.sse"
    path.write_text(raw, encoding="utf-8")

    parsed = parse_sse_file(path)

    assert "| 의원 | 41.93억원 |" in parsed.answer_markdown
    assert parsed.render_issues == ()


def test_sse_parser_preserves_public_step_events(tmp_path: Path) -> None:
    path = tmp_path / "steps.sse"
    path.write_text(
        "event: step\n"
        'data: {"index":1,"name":"질문 접수","detail":"요청 처리 시작","status":"started"}\n\n'
        "event: step\n"
        'data: {"index":2,"name":"임상 데이터 조회","detail":"성분 기준 임상시험 확인","status":"done"}\n\n'
        "event: step\n"
        "data: not-json\n\n"
        "event: done\n"
        "data: ok\n\n",
        encoding="utf-8",
    )

    parsed = parse_sse_file(path)

    assert parsed.steps == (
        {"index": 1, "name": "질문 접수", "detail": "요청 처리 시작", "status": "started"},
        {"index": 2, "name": "임상 데이터 조회", "detail": "성분 기준 임상시험 확인", "status": "done"},
    )


def test_sse_parser_flags_naive_table_join_breakage(tmp_path: Path) -> None:
    raw = (
        "event: delta\n"
        "data: | 채널 | 시장점유율 | 매출 |\n"
        "data: | --- | --- | --- |\n"
        "data: | 의원 | 3.37% | 41.93억원 |\n\n"
        "event: delta\n"
        "data: ## 처리 시간\n\n"
        "event: done\n"
        "data: ok\n\n"
    )
    path = tmp_path / "broken.sse"
    path.write_text(raw, encoding="utf-8")

    parsed = parse_sse_file(path)

    assert any(issue.startswith("naive_sse_table_join:") for issue in parsed.render_issues)


def test_sse_parser_flags_table_cell_count_mismatch(tmp_path: Path) -> None:
    raw = (
        "event: markdown_block\n"
        "data: {\"kind\":\"table\",\"markdown\":\"| 항목 | 값 |\\n| --- | --- |\\n| 매출 | 1억원 | 정상 |\\n\"}\n\n"
        "event: done\n"
        "data: ok\n\n"
    )
    path = tmp_path / "mismatch.sse"
    path.write_text(raw, encoding="utf-8")

    parsed = parse_sse_file(path)

    assert any(issue.startswith("table_cell_count:") for issue in parsed.render_issues)


def test_sse_parser_flags_raw_markdown_block_json_exposure(tmp_path: Path) -> None:
    raw = (
        "event: delta\n"
        "data: {\"kind\":\"table\",\"markdown\":\"| 기간 | 매출 |\\n| --- | --- |\\n| 2025-Q4 | 35.16억원 |\"}\n\n"
        "event: done\n"
        "data: ok\n\n"
    )
    path = tmp_path / "raw_json.sse"
    path.write_text(raw, encoding="utf-8")

    parsed = parse_sse_file(path)

    assert any(issue == 'answer_table_join:{"kind":"table"' for issue in parsed.render_issues)
    assert any(issue == 'answer_table_join:"markdown":"' for issue in parsed.render_issues)


def test_runtime_model_compare_runner_decodes_markdown_block_events() -> None:
    answer, counts = _parse_events(
        [
            "event: delta\ndata: 페린젝트 표입니다.\n\n",
            (
                "event: markdown_block\n"
                "data: {\"kind\":\"table\",\"markdown\":\"\\n\\n| 기간 | 매출 | MS |\\n| --- | --- | --- |\\n| 2025-Q4 | 35.16억원 | 25.36% |\\n\\n\"}\n\n"
            ),
            "event: done\ndata: ok\n\n",
        ]
    )

    assert "| 2025-Q4 | 35.16억원 | 25.36% |" in answer
    assert '{"kind":"table"' not in answer
    assert counts["done_count"] == 1
