from __future__ import annotations

import json
from pathlib import Path

from scripts.parity_harness import CHANNEL_PARAPHRASE_QUESTIONS, _capture_questions, diff_captures


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
