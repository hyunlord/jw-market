from __future__ import annotations

import argparse
import difflib
import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import requests

from scripts.fact_scoreboard.sse import parse_sse_file
from scripts.fact_scoreboard.text_numbers import extract_numeric_mentions

from jw_chat_agent_poc.service.app import SessionStore, _answer_question, _default_agent_factory, _sse_events
from jw_chat_agent_poc.tools.metrics.market_scope import MarketScopeResolver


QUESTIONS: tuple[tuple[str, str], ...] = (
    ("Q01", "리바로 관련 최근 이슈 뭐 있어"),
    ("Q02", "리바로하이 질병 환자수랑 최근 매출 한번에"),
    ("Q04", "리바로 최근 매출 추이 어때"),
    ("Q05", "리바로 시장 경쟁 구도 변화는 어때"),
    ("Q06", "페린젝트 매출 추이 어때"),
    ("Q07", "리바로 어느 채널에서 잘 팔려"),
    ("Q08", "리바로젯 시장 규모 얼마나 돼"),
    ("Q09", "리바로 점유율 몇 위야"),
    ("Q10", "가드렛 매출 추이"),
    ("Q11", "베노훼럼 매출 추이"),
)

VOLATILE_KEYS = {
    "answer_cleanup",
    "answer_generation_total",
    "chart_generation",
    "conversation_id",
    "elapsed_ms",
    "generated_at",
    "generation_time_ms",
    "session_id",
    "started_at",
    "started_at_monotonic",
    "timing",
    "timing_markdown",
}
TIMING_BLOCK = re.compile(r"\n*## 처리 시간\n\n(?:.*?)(?=\n## 출처|\Z)", re.DOTALL)


def capture(out_dir: Path, external_mode: str, base_url: str | None = None) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("sse", "markdown", "traces"):
        (out_dir / name).mkdir(parents=True, exist_ok=True)
    summary: list[dict[str, Any]] = []
    store = SessionStore()
    resolver = MarketScopeResolver()
    for qid, question in QUESTIONS:
        started = time.perf_counter()
        status = "ok"
        error = ""
        result: dict[str, Any] = {}
        events: list[str] = []
        try:
            if base_url:
                events = [_http_sse(base_url, question, external_mode)]
                result = {"capture_mode": "http", "trace_available": False}
            else:
                item = _answer_question(store, resolver, _default_agent_factory, question, external_mode, None)
                result = item["result"]
                events = list(_sse_events(question, result, item.get("conversation_id")))
        except Exception as exc:  # noqa: BLE001 - parity capture must record all question failures.
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
        raw_sse = "".join(events)
        (out_dir / "sse" / f"{qid}.sse").write_text(raw_sse, encoding="utf-8")
        parsed = parse_sse_file(out_dir / "sse" / f"{qid}.sse")
        (out_dir / "markdown" / f"{qid}.md").write_text(parsed.answer_markdown, encoding="utf-8")
        (out_dir / "traces" / f"{qid}.json").write_text(
            json.dumps(_trace_payload(qid, question, result, parsed), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        row = {
            "qid": qid,
            "question": question,
            "status": status,
            "error": error,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "delta_count": parsed.delta_count,
            "done_count": parsed.done_count,
            "error_count": parsed.error_count,
            "answer_chars": parsed.answer_chars,
        }
        summary.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "questions.json").write_text(json.dumps(dict(QUESTIONS), ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(row["status"] == "ok" and row["done_count"] == 1 and row["error_count"] == 0 for row in summary) else 1


def diff_captures(before: Path, after: Path, report_dir: Path) -> int:
    report_dir.mkdir(parents=True, exist_ok=True)
    results = [_diff_question(qid, before, after) for qid, _ in QUESTIONS]
    report = {"status": "pass" if all(item["pass"] for item in results) else "fail", "questions": results}
    (report_dir / "parity_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (report_dir / "parity_report.md").write_text(_markdown_report(report), encoding="utf-8")
    print(json.dumps({"status": report["status"], "failed": [item["qid"] for item in results if not item["pass"]]}, ensure_ascii=False))
    return 0 if report["status"] == "pass" else 1


def self_test(capture_dir: Path, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    same_dir = out_dir / "same"
    mutated_dir = out_dir / "mutated"
    same_status = diff_captures(capture_dir, capture_dir, same_dir)
    if mutated_dir.exists():
        shutil.rmtree(mutated_dir)
    shutil.copytree(capture_dir, mutated_dir)
    target = next((mutated_dir / "sse").glob("*.sse"))
    target.write_text(
        target.read_text(encoding="utf-8") + "event: delta\ndata: MUTATION_SENTINEL 999억원\n\n",
        encoding="utf-8",
    )
    mutated_status = diff_captures(capture_dir, mutated_dir, out_dir / "mutated_diff")
    summary = {"same_passed": same_status == 0, "mutation_detected": mutated_status != 0}
    (out_dir / "self_test_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["same_passed"] and summary["mutation_detected"] else 1


def _trace_payload(qid: str, question: str, result: dict[str, Any], parsed: Any) -> dict[str, Any]:
    return {
        "qid": qid,
        "question": question,
        "result": result,
        "sse": {
            "delta_count": parsed.delta_count,
            "done_count": parsed.done_count,
            "error_count": parsed.error_count,
            "sources": parsed.sources,
            "charts": parsed.charts,
        },
    }


def _http_sse(base_url: str, question: str, external_mode: str) -> str:
    url = base_url.rstrip("/") + "/chat/stream"
    response = requests.get(url, params={"question": question, "external_mode": external_mode}, timeout=180)
    response.raise_for_status()
    return response.text


def _diff_question(qid: str, before: Path, after: Path) -> dict[str, Any]:
    before_sse = parse_sse_file(before / "sse" / f"{qid}.sse")
    after_sse = parse_sse_file(after / "sse" / f"{qid}.sse")
    before_trace = _read_json(before / "traces" / f"{qid}.json")
    after_trace = _read_json(after / "traces" / f"{qid}.json")
    fact_check = _fact_equivalent(before_trace, before_sse, after_trace, after_sse)
    answer_number_check = _answer_number_check(before_trace, before_sse, after_trace, after_sse)
    checks = {
        "L0_sse": before_sse.error_count == after_sse.error_count == 0 and before_sse.done_count == after_sse.done_count == 1,
        "L1_route": _route_payload(before_trace) == _route_payload(after_trace),
        "L2_fact": fact_check["pass"],
        "L3_answer": answer_number_check["pass"],
        "L4_numbers": answer_number_check["pass"],
    }
    detail: dict[str, Any] = {}
    if not checks["L1_route"]:
        detail["route_diff"] = _json_diff(_route_payload(before_trace), _route_payload(after_trace))
    if not checks["L2_fact"]:
        detail.update(fact_check["detail"])
    if _normalize_markdown(before_sse.answer_markdown) != _normalize_markdown(after_sse.answer_markdown):
        detail["answer_text_changed"] = True
    if not answer_number_check["pass"]:
        detail.update(answer_number_check["detail"])
    return {"qid": qid, "pass": all(checks.values()), "checks": checks, "detail": detail}


def _route_payload(trace: dict[str, Any]) -> dict[str, Any]:
    result = trace.get("result") if isinstance(trace.get("result"), dict) else {}
    calls = result.get("tool_calls") if isinstance(result.get("tool_calls"), list) else []
    return _normalize_json(
        {
            "decomposition": _route_decomposition(result.get("decomposition")),
            "router_diagnostics": _route_diagnostics(result.get("router_diagnostics")),
            "tool_names": [call.get("tool") for call in calls if isinstance(call, dict)],
        }
    )


def _route_decomposition(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    items: list[dict[str, Any]] = []
    for route in value:
        if not isinstance(route, dict):
            continue
        items.append(
            {
                key: route.get(key)
                for key in ("intent", "bq", "question", "sources", "filters", "brands", "status", "max_steps")
                if key in route
            }
        )
    return items


def _route_diagnostics(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    return {key: value.get(key) for key in ("mode", "deterministic_execution", "fallback_used") if key in value}


def _fact_payload(trace: dict[str, Any], parsed: Any) -> dict[str, Any]:
    result = trace.get("result") if isinstance(trace.get("result"), dict) else {}
    markdown = result.get("markdown_response") if isinstance(result.get("markdown_response"), dict) else {}
    return _normalize_json(
        {
            "fact_md": _normalize_markdown(str(markdown.get("fact_md") or "")),
            "sources_md": _normalize_markdown(str(markdown.get("sources_md") or "")),
            "notice_md": _normalize_markdown(str(markdown.get("notice_md") or "")),
        }
    )


def _fact_equivalent(before_trace: dict[str, Any], before_sse: Any, after_trace: dict[str, Any], after_sse: Any) -> dict[str, Any]:
    before_core = _fact_payload(before_trace, before_sse)
    after_core = _fact_payload(after_trace, after_sse)
    detail: dict[str, Any] = {}
    if before_core != after_core:
        detail["fact_diff"] = _json_diff(before_core, after_core)
    chart_result = _chart_equivalence(before_sse.charts, after_sse.charts)
    if chart_result["common_changed"]:
        detail["chart_diff"] = chart_result
    if chart_result["presence_changed"]:
        detail["chart_presence_changed"] = chart_result["presence_changed"]
    return {"pass": before_core == after_core and not chart_result["common_changed"], "detail": detail}


def _chart_equivalence(before_charts: tuple[dict[str, object], ...], after_charts: tuple[dict[str, object], ...]) -> dict[str, object]:
    before = _chart_index(before_charts)
    after = _chart_index(after_charts)
    before_keys = set(before)
    after_keys = set(after)
    common_changed = {
        key: {"before": before[key], "after": after[key]}
        for key in sorted(before_keys & after_keys)
        if before[key] != after[key]
    }
    presence_changed = {
        "before_only": sorted(before_keys - after_keys),
        "after_only": sorted(after_keys - before_keys),
    }
    presence_changed = {key: value for key, value in presence_changed.items() if value}
    return {"common_changed": common_changed, "presence_changed": presence_changed}


def _chart_index(charts: tuple[dict[str, object], ...]) -> dict[str, Any]:
    indexed: dict[str, Any] = {}
    for chart in charts:
        normalized = _normalize_json(chart)
        if not isinstance(normalized, dict):
            continue
        key = "|".join(
            str(normalized.get(part) or "")
            for part in ("title", "source", "type", "unit")
        )
        indexed[key] = normalized
    return indexed


def _normalize_markdown(text: str) -> str:
    clean = TIMING_BLOCK.sub("", text.replace("\r\n", "\n")).strip()
    clean = re.sub(r"(?m)^event: timing.*$", "", clean)
    clean = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "<uuid>", clean)
    return re.sub(r"\n{3,}", "\n\n", clean).strip()


def _normalize_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize_json(val) for key, val in sorted(value.items()) if key not in VOLATILE_KEYS}
    if isinstance(value, list | tuple):
        return [_normalize_json(item) for item in value]
    if isinstance(value, str):
        return _normalize_markdown(value)
    return value


def _answer_number_check(before_trace: dict[str, Any], before_parsed: Any, after_trace: dict[str, Any], after_parsed: Any) -> dict[str, Any]:
    if _has_fact_support(after_trace, after_parsed):
        ungrounded = _ungrounded_answer_numbers(after_trace, after_parsed)
        return {"pass": ungrounded == [], "detail": {"ungrounded_numbers": ungrounded} if ungrounded else {}}
    new_numbers = _new_numbers(before_parsed.answer_markdown, after_parsed.answer_markdown)
    return {"pass": new_numbers == [], "detail": {"new_numbers_without_trace": new_numbers} if new_numbers else {}}


def _has_fact_support(trace: dict[str, Any], parsed: Any) -> bool:
    return bool(extract_numeric_mentions(_support_text(trace, parsed)))


def _ungrounded_answer_numbers(trace: dict[str, Any], parsed: Any) -> list[str]:
    answer_tokens = {_number_token(item) for item in extract_numeric_mentions(_normalize_markdown(parsed.answer_markdown))}
    support_tokens = {_number_token(item) for item in extract_numeric_mentions(_support_text(trace, parsed))}
    return sorted(answer_tokens - support_tokens)


def _new_numbers(before: str, after: str) -> list[str]:
    before_tokens = {_number_token(item) for item in extract_numeric_mentions(_normalize_markdown(before))}
    after_tokens = {_number_token(item) for item in extract_numeric_mentions(_normalize_markdown(after))}
    return sorted(after_tokens - before_tokens)


def _support_text(trace: dict[str, Any], parsed: Any) -> str:
    result = trace.get("result") if isinstance(trace.get("result"), dict) else {}
    markdown = result.get("markdown_response") if isinstance(result.get("markdown_response"), dict) else {}
    return "\n".join(
        (
            _normalize_markdown(str(markdown.get("fact_md") or "")),
            _normalize_markdown(str(markdown.get("sources_md") or "")),
            _normalize_markdown(str(markdown.get("notice_md") or "")),
            "\n".join(str(value) for value in markdown.get("allowed_numbers", ()) if value is not None),
            _normalize_markdown(str(parsed.sources or "")),
            json.dumps(parsed.charts, ensure_ascii=False, sort_keys=True, default=str),
        )
    )


def _number_token(item: Any) -> str:
    return f"{item.raw}|{item.unit}|{item.value:g}"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _json_diff(before: Any, after: Any) -> list[str]:
    return _text_diff(json.dumps(before, ensure_ascii=False, indent=2), json.dumps(after, ensure_ascii=False, indent=2))


def _text_diff(before: str, after: str) -> list[str]:
    return list(difflib.unified_diff(before.splitlines(), after.splitlines(), fromfile="before", tofile="after", lineterm=""))[:200]


def _markdown_report(report: dict[str, Any]) -> str:
    lines = [f"# Parity Report", "", f"status: {report['status']}", "", "| ID | L0 | L1 | L2 | L3 | L4 |", "| --- | --- | --- | --- | --- | --- |"]
    for item in report["questions"]:
        checks = item["checks"]
        lines.append(
            f"| {item['qid']} | {_mark(checks['L0_sse'])} | {_mark(checks['L1_route'])} | {_mark(checks['L2_fact'])} | {_mark(checks['L3_answer'])} | {_mark(checks['L4_numbers'])} |"
        )
    return "\n".join(lines) + "\n"


def _mark(value: bool) -> str:
    return "PASS" if value else "FAIL"


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture and diff jw-chat parity baselines.")
    sub = parser.add_subparsers(dest="command", required=True)
    capture_parser = sub.add_parser("capture")
    capture_parser.add_argument("--out-dir", type=Path, required=True)
    capture_parser.add_argument("--external-mode", default="live")
    capture_parser.add_argument("--base-url", help="Capture deployed /chat/stream SSE instead of local service trace.")
    diff_parser = sub.add_parser("diff")
    diff_parser.add_argument("--before", type=Path, required=True)
    diff_parser.add_argument("--after", type=Path, required=True)
    diff_parser.add_argument("--report-dir", type=Path, required=True)
    self_parser = sub.add_parser("self-test")
    self_parser.add_argument("--capture-dir", type=Path, required=True)
    self_parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "capture":
        return capture(args.out_dir, args.external_mode, args.base_url)
    if args.command == "diff":
        return diff_captures(args.before, args.after, args.report_dir)
    if args.command == "self-test":
        return self_test(args.capture_dir, args.out_dir)
    raise AssertionError(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
