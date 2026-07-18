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
    _p0g_market_tool_stage_failures,
    _p0g_route_contamination_failures,
    _p0g_source_evidence_failures,
    _source_section_forbidden_labels,
    _source_section_has_canonical_provenance_header,
    _source_section_has_complete_provenance_row,
    _source_section_has_labels_in_row,
    capture,
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

    def get(url, *, params, headers, timeout):
        captured.update(url=url, params=params, headers=headers, timeout=timeout)
        return Response()

    monkeypatch.setattr("scripts.parity_harness.requests.get", get)

    payload = _http_sse(
        "http://chat.example",
        "2025년 2분기 매출 얼마야",
        "live",
        conversation_id="dirty-session",
        portal_user_id="85",
    )

    assert payload == "event: done\ndata: ok\n\n"
    assert captured["params"] == {
        "question": "2025년 2분기 매출 얼마야",
        "external_mode": "live",
        "conversation_id": "dirty-session",
    }
    assert captured["headers"] == {"X-Portal-User-Id": "85"}


def test_capture_rejects_render_integrity_failures(monkeypatch, tmp_path: Path) -> None:
    raw_sse = (
        "event: markdown_block\n"
        'data: {"kind":"table","markdown":"2025-Q2 리바로 매출은 242.72억원입니다.\\n\\n| 항목 | 값 |\\n| --- | --- |\\n| 매출 | 242.72억원 | 깨짐 |\\n"}\n\n'
        "event: done\n"
        "data: ok\n\n"
    )
    monkeypatch.setattr("scripts.parity_harness._http_sse", lambda *_args, **_kwargs: raw_sse)

    assert capture(
        tmp_path,
        "live",
        "http://chat.example",
        (("F01", "2025년 2분기 매출 얼마야"),),
    ) == 1
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert any(issue.startswith("table_cell_count:") for issue in summary[0]["render_issues"])


def test_history_golden_acceptance_requires_live_values() -> None:
    top5_answer = (
        "상위 5개 합계 시장점유율은 29.52%입니다.\n\n"
        "| 순위 | 브랜드 | 점유율 | 매출 |\n"
        "| --- | --- | --- | --- |\n"
        "| 1위 | 로수젯 | 9.13% | 195.24억원 |\n"
        "| 2위 | 리피토 | 6.13% | 131.09억원 |\n"
        "| 3위 | 리바로젯 | 5.12% | 109.46억원 |\n"
        "| 4위 | 아토젯 | 4.95% | 105.87억원 |\n"
        "| 5위 | 로수바미브 | 4.20% | 89.76억원 |"
    )
    assert _history_golden_acceptance("H01", "도구 조회가 끝났습니다.") == (True, "")
    assert _history_golden_acceptance("H02", "2025-Q2 리바로 매출은 242.72억원입니다.") == (True, "")
    assert _history_golden_acceptance("H03", top5_answer) == (True, "")

    assert _history_golden_acceptance("H02", "데이터 존재 여부를 확인하지 못했습니다.") == (
        False,
        "fail-closed answer: 데이터 존재 여부를 확인하지 못했습니다",
    )
    assert _history_golden_acceptance("H03", "지원되지 않는 시장입니다.") == (
        False,
        "fail-closed answer: 지원되지 않는 시장",
    )
    assert _history_golden_acceptance("M02", "2025-Q2 리바로 매출은 242.72억원입니다.") == (True, "")
    assert _history_golden_acceptance("M03", top5_answer) == (True, "")
    assert _history_golden_acceptance("F01", "2025-Q2 리바로 매출은 242.72억원입니다.") == (True, "")
    assert _history_golden_acceptance("F02", top5_answer) == (True, "")


def test_history_golden_acceptance_rejects_top5_aggregate_without_ranked_rows() -> None:
    assert _history_golden_acceptance(
        "F02",
        "상위 5개 합계 시장점유율은 29.52%입니다.",
    ) == (False, "missing top 5 ranked rows")


def test_history_golden_acceptance_rejects_incorrect_top5_ranked_values() -> None:
    incorrect_answer = (
        "상위 5개 합계 시장점유율은 29.52%입니다.\n\n"
        "| 순위 | 브랜드 | 점유율 | 매출 |\n"
        "| --- | --- | --- | --- |\n"
        "| 1위 | 로수젯 | 9.13% | 195.24억원 |\n"
        "| 2위 | 리피토 | 6.13% | 131.09억원 |\n"
        "| 3위 | 잘못된브랜드 | 5.12% | 109.46억원 |\n"
        "| 4위 | 아토젯 | 4.95% | 105.87억원 |\n"
        "| 5위 | 로수바미브 | 4.20% | 0.00억원 |"
    )

    assert _history_golden_acceptance("M03", incorrect_answer) == (
        False,
        "incorrect top 5 ranked values",
    )


def test_history_golden_acceptance_rejects_sales_value_in_wrong_context() -> None:
    assert _history_golden_acceptance(
        "H02",
        "2026-Q2 로수젯 매출은 242.72억원입니다.",
    ) == (False, "incorrect 2025-Q2 리바로 sales context")


def test_history_golden_acceptance_rejects_conflicting_sales_claims() -> None:
    answer = (
        "2025-Q2 리바로 매출은 242.72억원입니다.\n"
        "다만 2025-Q2 리바로 매출은 0.00억원으로도 표시됩니다."
    )

    assert _history_golden_acceptance("F01", answer) == (
        False,
        "conflicting 2025-Q2 리바로 sales values",
    )


def test_history_golden_acceptance_rejects_duplicate_top5_ranks() -> None:
    answer = (
        "상위 5개 합계 시장점유율은 29.52%입니다.\n\n"
        "| 순위 | 브랜드 | 점유율 | 매출 |\n"
        "| --- | --- | --- | --- |\n"
        "| 1위 | 로수젯 | 9.13% | 195.24억원 |\n"
        "| 1위 | 잘못된브랜드 | 0.00% | 0.00억원 |\n"
        "| 2위 | 리피토 | 6.13% | 131.09억원 |\n"
        "| 3위 | 리바로젯 | 5.12% | 109.46억원 |\n"
        "| 4위 | 아토젯 | 4.95% | 105.87억원 |\n"
        "| 5위 | 로수바미브 | 4.20% | 89.76억원 |"
    )

    assert _history_golden_acceptance("F02", answer) == (
        False,
        "duplicate top 5 ranked rows",
    )


def test_history_golden_acceptance_ignores_correct_rows_after_sources() -> None:
    answer = (
        "상위 5개 합계 시장점유율은 29.52%입니다.\n\n"
        "| 순위 | 브랜드 | 점유율 | 매출 |\n"
        "| --- | --- | --- | --- |\n"
        "| 1위 | 잘못된1 | 0.00% | 0.00억원 |\n"
        "| 2위 | 잘못된2 | 0.00% | 0.00억원 |\n"
        "| 3위 | 잘못된3 | 0.00% | 0.00억원 |\n"
        "| 4위 | 잘못된4 | 0.00% | 0.00억원 |\n"
        "| 5위 | 잘못된5 | 0.00% | 0.00억원 |\n\n"
        "## 출처\n"
        "| 1위 | 로수젯 | 9.13% | 195.24억원 |\n"
        "| 2위 | 리피토 | 6.13% | 131.09억원 |\n"
        "| 3위 | 리바로젯 | 5.12% | 109.46억원 |\n"
        "| 4위 | 아토젯 | 4.95% | 105.87억원 |\n"
        "| 5위 | 로수바미브 | 4.20% | 89.76억원 |"
    )

    assert _history_golden_acceptance("F02", answer) == (
        False,
        "incorrect top 5 ranked values",
    )


def test_history_golden_acceptance_rejects_fail_closed_text_even_with_value() -> None:
    assert _history_golden_acceptance(
        "H02",
        "데이터 존재 여부를 확인하지 못했습니다. 참고값은 242.72억원입니다.",
    ) == (False, "fail-closed answer: 데이터 존재 여부를 확인하지 못했습니다")
    assert _history_golden_acceptance(
        "M03",
        "현재 지원되지 않는 시장 매핑입니다. 이전 기준은 29.52%입니다.",
    ) == (False, "fail-closed answer: 지원되지 않는 시장")
    assert _history_golden_acceptance(
        "F01",
        "조회 오류입니다. 캐시된 값은 242.72억원입니다.",
    ) == (False, "fail-closed answer: 조회 오류")


def test_p0g_suite_runs_all_portal_equivalent_scenarios(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[Path, str | None, str | None, tuple[tuple[str, str], ...], str | None]] = []

    def fake_capture(out_dir, external_mode, base_url, questions, conversation_id, *, portal_user_id=None):
        calls.append((out_dir, external_mode, base_url, questions, conversation_id))
        out_dir.mkdir(parents=True)
        (out_dir / "summary.json").write_text(
            json.dumps(
                [
                    {
                        "qid": qid,
                        "elapsed_ms": 100.0,
                        "sources": "UBIST" if qid in {"F01", "F02", "H02", "H03", "M02", "M03"} else "",
                        "source_section_has_ubist": qid in {"F01", "F02", "H02", "H03", "M02", "M03"},
                        "source_section_has_period": qid in {"F01", "F02", "H02", "H03", "M02", "M03"},
                        "source_section_has_ubist_period_row": qid in {"F01", "F02", "H02", "H03", "M02", "M03"},
                        "source_section_has_canonical_provenance_header": qid
                        in {"F01", "F02", "H02", "H03", "M02", "M03"},
                        "source_section_has_complete_provenance_row": qid
                        in {"F01", "F02", "H02", "H03", "M02", "M03"},
                        "steps": (
                            [
                                {"name": "질문 접수", "status": "done"},
                                {"name": "임상 데이터 조회", "status": "done"},
                            ]
                            if qid == "H01"
                            else [
                                {"name": "질문 접수", "status": "done"},
                                {"name": "딥리서치 조사 설계", "status": "done"},
                            ]
                            if qid == "M01"
                            else [
                                {"name": "질문 접수", "status": "done"},
                                {"name": "조회 계획 확정", "status": "done"},
                                {
                                    "name": (
                                        "브랜드 매출 조회"
                                        if qid in {"F01", "H02", "M02"}
                                        else "상위 브랜드 확인"
                                    ),
                                    "status": "done",
                                },
                            ]
                        ),
                        "conversation_ids": [conversation_id] if conversation_id else [],
                    }
                    for qid, _ in questions
                ],
                ensure_ascii=False,
            ),
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
        portal_user_id="85",
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
    def fake_capture(out_dir, external_mode, base_url, questions, conversation_id, *, portal_user_id=None):
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
        portal_user_id="85",
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
    def fake_capture(out_dir, external_mode, base_url, questions, conversation_id, *, portal_user_id=None):
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
        "portal_user_id_supplied": False,
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
    def fake_capture(out_dir, external_mode, base_url, questions, conversation_id, *, portal_user_id=None):
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
        portal_user_id="85",
    ) == 1
    summary = json.loads((tmp_path / "p0g_summary.json").read_text(encoding="utf-8"))
    assert summary["qualification_failures"] == [
        "portal-equivalent evidence requires a deployed base URL",
        "file bridge base URL was not supplied",
    ]


def test_p0g_suite_requires_portal_user_header_for_release_evidence(monkeypatch, tmp_path: Path) -> None:
    def fake_capture(out_dir, external_mode, base_url, questions, conversation_id, *, portal_user_id=None):
        out_dir.mkdir(parents=True)
        rows = [
            {
                "qid": qid,
                "elapsed_ms": 100.0,
                "steps": [{"name": "질문 접수"}],
                "conversation_ids": [conversation_id] if conversation_id else [],
            }
            for qid, _ in questions
        ]
        (out_dir / "summary.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return 0

    monkeypatch.setattr("scripts.parity_harness.capture", fake_capture)
    monkeypatch.setattr(
        "scripts.parity_harness._probe_uploaded_file_session",
        lambda base_url, conversation_id, workflow_id: (True, 1, ""),
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
    assert summary["qualification_failures"] == [
        "portal-equivalent evidence requires X-Portal-User-Id",
    ]


def test_p0g_suite_fails_when_any_scenario_fails(monkeypatch, tmp_path: Path) -> None:
    statuses = iter((0, 1, 0))

    def fake_capture(out_dir, external_mode, base_url, questions, conversation_id, *, portal_user_id=None):
        out_dir.mkdir(parents=True)
        (out_dir / "summary.json").write_text(
            json.dumps([{"qid": qid, "elapsed_ms": 100.0} for qid, _ in questions]),
            encoding="utf-8",
        )
        return next(statuses)

    monkeypatch.setattr("scripts.parity_harness.capture", fake_capture)

    assert capture_p0g_suite(tmp_path, "live", None) == 1


def test_p0g_suite_fails_when_general_golden_exceeds_fast_path_budget(monkeypatch, tmp_path: Path) -> None:
    def fake_capture(out_dir, external_mode, base_url, questions, conversation_id, *, portal_user_id=None):
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
    def fake_capture(out_dir, external_mode, base_url, questions, conversation_id, *, portal_user_id=None):
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
        "H02": ["첨부 파일 확인", "첨부 문서 조회", "임상 데이터 조회"],
        "H03": ["첨부 파일 확인"],
    }


def test_p0g_suite_fails_when_general_step_text_leaks_prior_turn_or_internal_metadata(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def fake_capture(out_dir, external_mode, base_url, questions, conversation_id, *, portal_user_id=None):
        out_dir.mkdir(parents=True)
        rows = [
            {
                "qid": qid,
                "elapsed_ms": 100.0,
                "steps": (
                    [
                        {"name": "질문 분해", "detail": "뇌경색 질환 임상·허가", "status": "done"},
                        {"name": "관련 데이터 조회", "detail": "1; mode=parallel단계", "status": "done"},
                    ]
                    if qid == "H02"
                    else []
                ),
                "answer_forbidden_tokens": ["첨부 파일", "뇌경색"] if qid == "H02" else [],
            }
            for qid, _ in questions
        ]
        (out_dir / "summary.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return 0

    monkeypatch.setattr("scripts.parity_harness.capture", fake_capture)

    assert capture_p0g_suite(tmp_path, "live", None) == 1
    summary = json.loads((tmp_path / "p0g_summary.json").read_text(encoding="utf-8"))
    assert summary["scenarios"][1]["route_contamination_failures"] == {
        "H02": [
            "질문 분해:뇌경색",
            "질문 분해:임상·허가",
            "관련 데이터 조회:mode=parallel",
            "답변:첨부 파일",
            "답변:뇌경색",
        ],
    }


def test_p0g_suite_rejects_portal_evidence_without_progress_steps(monkeypatch, tmp_path: Path) -> None:
    def fake_capture(out_dir, external_mode, base_url, questions, conversation_id, *, portal_user_id=None):
        out_dir.mkdir(parents=True)
        (out_dir / "summary.json").write_text(
            json.dumps(
                [
                    {
                        "qid": qid,
                        "elapsed_ms": 100.0,
                        "steps": [],
                        "conversation_ids": [conversation_id] if conversation_id else [],
                    }
                    for qid, _ in questions
                ]
            ),
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr("scripts.parity_harness.capture", fake_capture)
    monkeypatch.setattr(
        "scripts.parity_harness._probe_uploaded_file_session",
        lambda base_url, conversation_id, workflow_id: (True, 1, ""),
    )

    assert capture_p0g_suite(
        tmp_path,
        "live",
        "http://portal-equivalent",
        history_conversation_id="uploaded-file-session",
        portal_equivalent=True,
        portal_user_id="85",
        file_base_url="http://code-serving-235",
    ) == 1
    summary = json.loads((tmp_path / "p0g_summary.json").read_text(encoding="utf-8"))
    assert summary["scenarios"] == [
        {
            "scenario": "fresh",
            "status": 0,
            "latency_failures": [],
            "route_contamination_failures": {},
            "step_evidence_failures": ["F01", "F02"],
            "fast_path_stage_failures": ["F01", "F02"],
            "market_tool_stage_failures": {"F01": "브랜드 매출 조회", "F02": "상위 브랜드 확인"},
            "source_evidence_failures": {
                "F01": {"event_sources": "", "answer_source_section_has_ubist": False},
                "F02": {"event_sources": "", "answer_source_section_has_ubist": False},
            },
            "session_continuity_failures": {},
            "seed_execution_failures": [],
        },
        {
            "scenario": "history",
            "status": 0,
            "latency_failures": [],
            "route_contamination_failures": {},
            "step_evidence_failures": ["H02", "H03"],
            "fast_path_stage_failures": ["H02", "H03"],
            "market_tool_stage_failures": {"H02": "브랜드 매출 조회", "H03": "상위 브랜드 확인"},
            "source_evidence_failures": {
                "H02": {"event_sources": "", "answer_source_section_has_ubist": False},
                "H03": {"event_sources": "", "answer_source_section_has_ubist": False},
            },
            "session_continuity_failures": {},
            "seed_execution_failures": ["H01"],
        },
        {
            "scenario": "mode-transition",
            "status": 0,
            "latency_failures": [],
            "route_contamination_failures": {},
            "step_evidence_failures": ["M02", "M03"],
            "fast_path_stage_failures": ["M02", "M03"],
            "market_tool_stage_failures": {"M02": "브랜드 매출 조회", "M03": "상위 브랜드 확인"},
            "source_evidence_failures": {
                "M02": {"event_sources": "", "answer_source_section_has_ubist": False},
                "M03": {"event_sources": "", "answer_source_section_has_ubist": False},
            },
            "session_continuity_failures": {},
            "seed_execution_failures": ["M01"],
        },
    ]


def test_p0g_suite_rejects_history_response_from_a_different_session(monkeypatch, tmp_path: Path) -> None:
    def fake_capture(out_dir, external_mode, base_url, questions, conversation_id, *, portal_user_id=None):
        out_dir.mkdir(parents=True)
        rows = []
        for qid, _ in questions:
            returned = "wrong-session" if qid == "H02" else conversation_id
            rows.append(
                {
                    "qid": qid,
                    "elapsed_ms": 100.0,
                    "steps": [{"name": "질문 접수"}],
                    "conversation_ids": [returned] if returned else [],
                }
            )
        (out_dir / "summary.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return 0

    monkeypatch.setattr("scripts.parity_harness.capture", fake_capture)
    monkeypatch.setattr(
        "scripts.parity_harness._probe_uploaded_file_session",
        lambda base_url, conversation_id, workflow_id: (True, 1, ""),
    )

    assert capture_p0g_suite(
        tmp_path,
        "live",
        "http://portal-equivalent",
        history_conversation_id="uploaded-file-session",
        portal_equivalent=True,
        portal_user_id="85",
        file_base_url="http://code-serving-235",
    ) == 1
    summary = json.loads((tmp_path / "p0g_summary.json").read_text(encoding="utf-8"))
    assert summary["scenarios"][1]["session_continuity_failures"] == {
        "H02": ["wrong-session"],
    }


def test_p0g_suite_requires_history_and_deep_seed_execution(monkeypatch, tmp_path: Path) -> None:
    def fake_capture(out_dir, external_mode, base_url, questions, conversation_id, *, portal_user_id=None):
        out_dir.mkdir(parents=True)
        rows = [
            {
                "qid": qid,
                "elapsed_ms": 100.0,
                "steps": (
                    [{"name": "임상 데이터 조회", "status": "started"}]
                    if qid == "H01"
                    else [{"name": "딥리서치 조사 설계", "status": "started"}]
                    if qid == "M01"
                    else [{"name": "질문 접수"}]
                ),
                "conversation_ids": [conversation_id] if conversation_id else [],
            }
            for qid, _ in questions
        ]
        (out_dir / "summary.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return 0

    monkeypatch.setattr("scripts.parity_harness.capture", fake_capture)
    monkeypatch.setattr(
        "scripts.parity_harness._probe_uploaded_file_session",
        lambda base_url, conversation_id, workflow_id: (True, 1, ""),
    )

    assert capture_p0g_suite(
        tmp_path,
        "live",
        "http://portal-equivalent",
        history_conversation_id="uploaded-file-session",
        portal_equivalent=True,
        portal_user_id="85",
        file_base_url="http://code-serving-235",
    ) == 1
    summary = json.loads((tmp_path / "p0g_summary.json").read_text(encoding="utf-8"))
    assert summary["scenarios"][1]["seed_execution_failures"] == ["H01"]
    assert summary["scenarios"][2]["seed_execution_failures"] == ["M01"]


def test_p0g_suite_requires_completed_fast_path_stage_for_general_goldens(monkeypatch, tmp_path: Path) -> None:
    def fake_capture(out_dir, external_mode, base_url, questions, conversation_id, *, portal_user_id=None):
        out_dir.mkdir(parents=True)
        rows = [
            {
                "qid": qid,
                "elapsed_ms": 100.0,
                "steps": (
                    [{"name": "임상 데이터 조회", "status": "done"}]
                    if qid == "H01"
                    else [{"name": "딥리서치 조사 설계", "status": "done"}]
                    if qid == "M01"
                    else [
                        {"name": "질문 접수", "status": "done"},
                        {"name": "조회 계획 확정", "status": "started"},
                    ]
                ),
                "conversation_ids": [conversation_id] if conversation_id else [],
            }
            for qid, _ in questions
        ]
        (out_dir / "summary.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return 0

    monkeypatch.setattr("scripts.parity_harness.capture", fake_capture)
    monkeypatch.setattr(
        "scripts.parity_harness._probe_uploaded_file_session",
        lambda base_url, conversation_id, workflow_id: (True, 1, ""),
    )

    assert capture_p0g_suite(
        tmp_path,
        "live",
        "http://portal-equivalent",
        history_conversation_id="uploaded-file-session",
        portal_equivalent=True,
        portal_user_id="85",
        file_base_url="http://code-serving-235",
    ) == 1
    summary = json.loads((tmp_path / "p0g_summary.json").read_text(encoding="utf-8"))
    assert [item["fast_path_stage_failures"] for item in summary["scenarios"]] == [
        ["F01", "F02"],
        ["H02", "H03"],
        ["M02", "M03"],
    ]


def test_p0g_suite_requires_completed_market_tool_stage_for_general_goldens(monkeypatch, tmp_path: Path) -> None:
    def fake_capture(out_dir, external_mode, base_url, questions, conversation_id, *, portal_user_id=None):
        out_dir.mkdir(parents=True)
        rows = [
            {
                "qid": qid,
                "elapsed_ms": 100.0,
                "steps": (
                    [{"name": "임상 데이터 조회", "status": "done"}]
                    if qid == "H01"
                    else [{"name": "딥리서치 조사 설계", "status": "done"}]
                    if qid == "M01"
                    else [
                        {"name": "질문 접수", "status": "done"},
                        {"name": "조회 계획 확정", "status": "done"},
                        {
                            "name": (
                                "브랜드 매출 조회"
                                if qid in {"F01", "H02", "M02"}
                                else "상위 브랜드 확인"
                            ),
                            "status": "started",
                        },
                    ]
                ),
                "conversation_ids": [conversation_id] if conversation_id else [],
            }
            for qid, _ in questions
        ]
        (out_dir / "summary.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return 0

    monkeypatch.setattr("scripts.parity_harness.capture", fake_capture)
    monkeypatch.setattr(
        "scripts.parity_harness._probe_uploaded_file_session",
        lambda base_url, conversation_id, workflow_id: (True, 1, ""),
    )

    assert capture_p0g_suite(
        tmp_path,
        "live",
        "http://portal-equivalent",
        history_conversation_id="uploaded-file-session",
        portal_equivalent=True,
        portal_user_id="85",
        file_base_url="http://code-serving-235",
    ) == 1
    summary = json.loads((tmp_path / "p0g_summary.json").read_text(encoding="utf-8"))
    assert [item["market_tool_stage_failures"] for item in summary["scenarios"]] == [
        {"F01": "브랜드 매출 조회", "F02": "상위 브랜드 확인"},
        {"H02": "브랜드 매출 조회", "H03": "상위 브랜드 확인"},
        {"M02": "브랜드 매출 조회", "M03": "상위 브랜드 확인"},
    ]


def test_p0g_general_steps_reject_stale_or_skipped_public_details() -> None:
    assert _p0g_route_contamination_failures(
        [
            {
                "qid": "H02",
                "steps": [
                    {
                        "name": "조회 계획 확정",
                        "status": "done",
                        "detail": "이전 턴 계획을 사용해 새 분류는 건너뜀",
                    },
                    {
                        "name": "브랜드 매출 조회",
                        "status": "done",
                        "summary": "도구가 실행되지 않음",
                    },
                ],
            }
        ]
    ) == {
        "H02": [
            "조회 계획 확정:이전 턴",
            "조회 계획 확정:건너뜀",
            "브랜드 매출 조회:실행되지",
        ]
    }


def test_p0g_market_tool_stage_requires_plan_before_tool_completion() -> None:
    assert _p0g_market_tool_stage_failures(
        [
            {
                "qid": "F01",
                "steps": [
                    {"name": "브랜드 매출 조회", "status": "done"},
                    {"name": "조회 계획 확정", "status": "done"},
                ],
            }
        ]
    ) == {"F01": "브랜드 매출 조회"}


def test_p0g_suite_requires_ubist_source_evidence_for_general_goldens(monkeypatch, tmp_path: Path) -> None:
    def fake_capture(out_dir, external_mode, base_url, questions, conversation_id, *, portal_user_id=None):
        out_dir.mkdir(parents=True)
        rows = [
            {
                "qid": qid,
                "elapsed_ms": 100.0,
                "sources": "—" if qid in {"F01", "F02", "H02", "H03", "M02", "M03"} else "ClinicalTrials.gov",
                "source_section_has_ubist": False,
                "steps": (
                    [{"name": "임상 데이터 조회", "status": "done"}]
                    if qid == "H01"
                    else [{"name": "딥리서치 조사 설계", "status": "done"}]
                    if qid == "M01"
                    else [
                        {"name": "조회 계획 확정", "status": "done"},
                        {
                            "name": (
                                "브랜드 매출 조회"
                                if qid in {"F01", "H02", "M02"}
                                else "상위 브랜드 확인"
                            ),
                            "status": "done",
                        },
                    ]
                ),
                "conversation_ids": [conversation_id] if conversation_id else [],
            }
            for qid, _ in questions
        ]
        (out_dir / "summary.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return 0

    monkeypatch.setattr("scripts.parity_harness.capture", fake_capture)
    monkeypatch.setattr(
        "scripts.parity_harness._probe_uploaded_file_session",
        lambda base_url, conversation_id, workflow_id: (True, 1, ""),
    )

    assert capture_p0g_suite(
        tmp_path,
        "live",
        "http://portal-equivalent",
        history_conversation_id="uploaded-file-session",
        portal_equivalent=True,
        portal_user_id="85",
        file_base_url="http://code-serving-235",
    ) == 1
    summary = json.loads((tmp_path / "p0g_summary.json").read_text(encoding="utf-8"))
    assert [item["source_evidence_failures"] for item in summary["scenarios"]] == [
        {
            "F01": {"event_sources": "—", "answer_source_section_has_ubist": False},
            "F02": {"event_sources": "—", "answer_source_section_has_ubist": False},
        },
        {
            "H02": {"event_sources": "—", "answer_source_section_has_ubist": False},
            "H03": {"event_sources": "—", "answer_source_section_has_ubist": False},
        },
        {
            "M02": {"event_sources": "—", "answer_source_section_has_ubist": False},
            "M03": {"event_sources": "—", "answer_source_section_has_ubist": False},
        },
    ]


def test_p0g_suite_rejects_ubist_event_when_rendered_source_section_is_empty(monkeypatch, tmp_path: Path) -> None:
    def fake_capture(out_dir, external_mode, base_url, questions, conversation_id, *, portal_user_id=None):
        out_dir.mkdir(parents=True)
        rows = [
            {
                "qid": qid,
                "elapsed_ms": 100.0,
                "sources": "UBIST",
                "source_section_has_ubist": False,
                "steps": (
                    [{"name": "임상 데이터 조회", "status": "done"}]
                    if qid == "H01"
                    else [{"name": "딥리서치 조사 설계", "status": "done"}]
                    if qid == "M01"
                    else [
                        {"name": "조회 계획 확정", "status": "done"},
                        {
                            "name": "브랜드 매출 조회" if qid in {"F01", "H02", "M02"} else "상위 브랜드 확인",
                            "status": "done",
                        },
                    ]
                ),
                "conversation_ids": [conversation_id] if conversation_id else [],
            }
            for qid, _ in questions
        ]
        (out_dir / "summary.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return 0

    monkeypatch.setattr("scripts.parity_harness.capture", fake_capture)
    monkeypatch.setattr(
        "scripts.parity_harness._probe_uploaded_file_session",
        lambda base_url, conversation_id, workflow_id: (True, 1, ""),
    )

    assert capture_p0g_suite(
        tmp_path,
        "live",
        "http://portal-equivalent",
        history_conversation_id="uploaded-file-session",
        portal_equivalent=True,
        portal_user_id="85",
        file_base_url="http://code-serving-235",
    ) == 1
    summary = json.loads((tmp_path / "p0g_summary.json").read_text(encoding="utf-8"))
    assert summary["scenarios"][0]["source_evidence_failures"] == {
        "F01": {"event_sources": "UBIST", "answer_source_section_has_ubist": False},
        "F02": {"event_sources": "UBIST", "answer_source_section_has_ubist": False},
    }


def test_p0g_source_evidence_requires_the_golden_period_in_rendered_sources() -> None:
    assert _p0g_source_evidence_failures(
        [
            {
                "qid": "F01",
                "sources": "UBIST",
                "source_section_has_ubist": True,
                "source_section_has_canonical_provenance_header": True,
                "source_section_has_period": False,
            },
            {
                "qid": "F02",
                "sources": "UBIST",
                "source_section_has_ubist": True,
                "source_section_has_canonical_provenance_header": True,
                "source_section_has_period": False,
            },
        ]
    ) == {
        "F01": {
            "event_sources": "UBIST",
            "answer_source_section_has_ubist": True,
            "expected_period": "2025-Q2",
            "answer_source_section_has_period": False,
        },
        "F02": {
            "event_sources": "UBIST",
            "answer_source_section_has_ubist": True,
            "expected_period": "2026-05",
            "answer_source_section_has_period": False,
        },
    }


def test_p0g_source_evidence_requires_ubist_and_period_in_the_same_row() -> None:
    assert _p0g_source_evidence_failures(
        [
            {
                "qid": "F01",
                "sources": "UBIST",
                "source_section_has_ubist": True,
                "source_section_has_canonical_provenance_header": True,
                "source_section_has_period": True,
                "source_section_has_ubist_period_row": False,
            }
        ]
    ) == {
        "F01": {
            "event_sources": "UBIST",
            "expected_period": "2025-Q2",
            "answer_source_section_has_ubist_period_row": False,
        }
    }


def test_p0g_source_evidence_rejects_stale_external_and_file_sources() -> None:
    assert _p0g_source_evidence_failures(
        [
            {
                "qid": "H02",
                "sources": "UBIST, ClinicalTrials.gov, 업로드 파일",
                "source_section_has_ubist": True,
                "source_section_has_period": True,
                "source_section_has_ubist_period_row": True,
                "source_section_forbidden_labels": ["ClinicalTrials", "업로드"],
            }
        ]
    ) == {
        "H02": {
            "event_sources": "UBIST, ClinicalTrials.gov, 업로드 파일",
            "forbidden_event_sources": ["ClinicalTrials", "업로드"],
            "forbidden_answer_sources": ["ClinicalTrials", "업로드"],
        }
    }


def test_source_section_forbidden_labels_reads_rendered_provenance() -> None:
    answer = (
        "정상 답변\n\n## 출처\n"
        "| 출처 | 기준기간 |\n| --- | --- |\n"
        "| UBIST | 2025-Q2 |\n"
        "| ClinicalTrials.gov | — |\n"
        "| 업로드 문서 | — |"
    )

    assert _source_section_forbidden_labels(answer) == ["ClinicalTrials", "업로드"]


def test_p0g_source_evidence_requires_canonical_provenance_header() -> None:
    assert _p0g_source_evidence_failures(
        [
            {
                "qid": "F01",
                "sources": "UBIST",
                "source_section_has_ubist": True,
                "source_section_has_period": True,
                "source_section_has_ubist_period_row": True,
                "source_section_has_canonical_provenance_header": False,
                "source_section_forbidden_labels": [],
            }
        ]
    ) == {
        "F01": {
            "event_sources": "UBIST",
            "answer_source_section_has_canonical_provenance_header": False,
        }
    }


def test_canonical_provenance_header_rejects_abbreviated_source_table() -> None:
    abbreviated = (
        "답변\n\n## 출처\n"
        "| 출처 | 기준기간 |\n| --- | --- |\n| UBIST | 2025-Q2 |"
    )
    canonical = (
        "답변\n\n## 출처\n"
        "| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| UBIST | 2025-Q2 | 일반뷰 | 고지혈증 | 전체 | 전체 | 억원 |"
    )

    assert _source_section_has_canonical_provenance_header(abbreviated) is False
    assert _source_section_has_canonical_provenance_header(canonical) is True


def test_p0g_source_evidence_rejects_incomplete_canonical_row() -> None:
    assert _p0g_source_evidence_failures(
        [
            {
                "qid": "F01",
                "sources": "UBIST",
                "source_section_has_ubist": True,
                "source_section_has_period": True,
                "source_section_has_ubist_period_row": True,
                "source_section_has_canonical_provenance_header": True,
                "source_section_has_complete_provenance_row": False,
                "source_section_forbidden_labels": [],
            }
        ]
    ) == {
        "F01": {
            "event_sources": "UBIST",
            "expected_period": "2025-Q2",
            "answer_source_section_has_complete_provenance_row": False,
        }
    }


def test_complete_provenance_row_rejects_missing_context_cells() -> None:
    incomplete = (
        "답변\n\n## 출처\n"
        "| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| UBIST | 2025-Q2 | — | — | — | — | — |"
    )
    complete = (
        "답변\n\n## 출처\n"
        "| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| UBIST | 2025-Q2 | 전략뷰 | 요청 브랜드의 전략 시장 | 555 | 전체 | 억원 |"
    )

    assert _source_section_has_complete_provenance_row(incomplete, "UBIST", "2025-Q2") is False
    assert _source_section_has_complete_provenance_row(complete, "UBIST", "2025-Q2") is True


def test_source_section_row_match_does_not_combine_different_rows() -> None:
    same_row = (
        "답변\n\n## 출처\n"
        "| 출처 | 기준기간 |\n| --- | --- |\n| UBIST | 2025-Q2 |"
    )
    split_rows = (
        "답변\n\n## 출처\n"
        "| 출처 | 기준기간 |\n| --- | --- |\n| UBIST | — |\n| 외부 API | 2025-Q2 |"
    )

    assert _source_section_has_labels_in_row(same_row, ("UBIST", "2025-Q2")) is True
    assert _source_section_has_labels_in_row(split_rows, ("UBIST", "2025-Q2")) is False


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
        "event: conversation\n"
        "data: dirty-session\n\n"
        "event: done\n"
        "data: ok\n\n",
        encoding="utf-8",
    )

    parsed = parse_sse_file(path)

    assert parsed.steps == (
        {"index": 1, "name": "질문 접수", "detail": "요청 처리 시작", "status": "started"},
        {"index": 2, "name": "임상 데이터 조회", "detail": "성분 기준 임상시험 확인", "status": "done"},
    )
    assert parsed.conversation_ids == ("dirty-session",)


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
