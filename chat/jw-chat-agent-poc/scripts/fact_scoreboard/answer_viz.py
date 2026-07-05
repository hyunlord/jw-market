from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any

from scripts.fact_scoreboard.render_html import render_markdown_fragment


def main() -> None:
    """Render question-by-question answers, query facts, and scoring details as static HTML."""

    args = _parse_args()
    details = _load_details(args.details)
    summary = _load_json(args.summary) if args.summary else {}
    args.output.write_text(_page(details, summary), encoding="utf-8")


def _page(details: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    return "\n".join(
        (
            _head(),
            "<body><main>",
            "<h1>Chat Answer Safety Visualization</h1>",
            _summary(summary),
            _version_meta(summary, details),
            *(_question_block(detail) for detail in details),
            "</main></body></html>",
        )
    )


def _summary(summary: dict[str, Any]) -> str:
    if not summary:
        return ""
    rows = (
        ("생성 UTC", summary.get("generated_utc", "")),
        ("질문 수", summary.get("total", "")),
        ("schema_ok", f"{summary.get('schema_ok', '')}/{summary.get('total', '')}"),
        ("degraded fallback", f"{summary.get('degraded', '')}/{summary.get('total', '')}"),
        (
            "multistep_comparison_ok",
            f"{summary.get('multistep_comparison_ok', '')}/{summary.get('multistep_comparison_applicable', '')}",
        ),
        ("query_fact_ok", f"{summary.get('query_fact_ok', '')}/{summary.get('query_fact_applicable', '')}"),
        ("answer_fact_ok", f"{summary.get('answer_fact_ok', '')}/{summary.get('answer_fact_applicable', '')}"),
    )
    return _table(("항목", "값"), rows)


def _version_meta(summary: dict[str, Any], details: list[dict[str, Any]]) -> str:
    version = _version_from_summary(summary) or _first_detail_version(details)
    if not version:
        return ""
    policy_versions = version.get("policy_versions") if isinstance(version.get("policy_versions"), dict) else {}
    rows: list[tuple[Any, ...]] = [
        ("release_id", version.get("release_id", "")),
        ("git_sha", version.get("git_sha", "")),
        ("image_digest", version.get("image_digest", "")),
        ("built_at", version.get("built_at", "")),
        ("model_family", version.get("model_family", "")),
        ("serving_common_router", version.get("serving_common_router", "")),
        ("serving_final", version.get("serving_final", "")),
        ("serving_planner", version.get("serving_planner", "")),
    ]
    rows.extend((f"policy.{key}", value) for key, value in sorted(policy_versions.items()))
    return "\n".join(("<section class=\"meta-card\"><h2>Runtime provenance</h2>", _table(("항목", "값"), rows), "</section>"))


def _version_from_summary(summary: dict[str, Any]) -> dict[str, Any]:
    for key in ("version", "runtime_version", "provenance"):
        value = summary.get(key)
        if isinstance(value, dict):
            version = value.get("version") if isinstance(value.get("version"), dict) else value
            if isinstance(version, dict):
                return version
    return {}


def _first_detail_version(details: list[dict[str, Any]]) -> dict[str, Any]:
    for detail in details:
        version = _detail_version(detail)
        if version:
            return version
    return {}


def _detail_version(detail: dict[str, Any]) -> dict[str, Any]:
    for key in ("trace", "trace_envelope", "runtime_trace"):
        trace = detail.get(key)
        if isinstance(trace, dict) and isinstance(trace.get("version"), dict):
            return trace["version"]
    return {}


def _question_provenance(detail: dict[str, Any]) -> str:
    trace = next((detail.get(key) for key in ("trace", "trace_envelope", "runtime_trace") if isinstance(detail.get(key), dict)), {})
    if not isinstance(trace, dict):
        return ""
    version = trace.get("version") if isinstance(trace.get("version"), dict) else {}
    stages = trace.get("model_stages") if isinstance(trace.get("model_stages"), dict) else {}
    route = trace.get("route") if isinstance(trace.get("route"), dict) else {}
    rows = (
        ("trace_id", trace.get("trace_id", "")),
        ("git_sha", version.get("git_sha", "")),
        ("image_digest", version.get("image_digest", "")),
        ("route", route.get("service_path", "") or route.get("mode", "")),
        ("planner", stages.get("planner_serving_id", "")),
        ("final", stages.get("final_serving_id", "")),
        ("router", stages.get("router_serving_id", "")),
    )
    return "<details class=\"provenance\"><summary>Runtime provenance</summary>" + _table(("항목", "값"), rows) + "</details>"


def _question_block(detail: dict[str, Any]) -> str:
    row = detail.get("row") if isinstance(detail.get("row"), dict) else {}
    answer = str(detail.get("answer_markdown") or "")
    question = str(row.get("question") or "")
    qid = str(row.get("qid") or "")
    badges = " ".join(
        _badge(label, str(row.get(key) or ""))
        for label, key in (
            ("schema", "schema_ok"),
            ("degraded", "degraded"),
            ("multistep", "multistep_comparison_ok"),
            ("query", "query_fact_ok"),
            ("answer", "answer_fact_ok"),
            ("residual", "residual_class"),
        )
    )
    return "\n".join(
        (
            '<section class="question">',
            f"<h2>{html.escape(qid)} {html.escape(question)}</h2>",
            f'<div class="badges">{badges}</div>',
            _question_provenance(detail),
            "<h3>최종 답변</h3>",
            f'<div class="answer">{render_markdown_fragment(answer)}</div>',
            "<details><summary>답변 markdown 원문</summary>",
            f"<pre>{html.escape(answer)}</pre>",
            "</details>",
            '<details class="facts"><summary>Query fact — 채점 근거(출처 아님)</summary>',
            _facts_table(detail.get("trace_facts"), "trace/query fact가 없습니다."),
            "</details>",
            "<h3>소요시간</h3>",
            _timing_table(detail.get("timing")),
            "<h3>채점 상세</h3>",
            _score_detail(detail),
            "</section>",
        )
    )


def _badge(label: str, value: str) -> str:
    if label == "degraded":
        klass = "bad" if value == "Y" else "ok" if value == "N" else "na"
        return f'<span class="badge {klass}">{html.escape(label)}: {html.escape(value)}</span>'
    klass = "ok" if value == "Y" or value == "pass" else "bad" if value == "N" else "na"
    return f'<span class="badge {klass}">{html.escape(label)}: {html.escape(value)}</span>'


def _facts_table(raw_facts: Any, empty: str) -> str:
    facts = raw_facts if isinstance(raw_facts, list) else []
    if not facts:
        return f"<p>{html.escape(empty)}</p>"
    rows: list[tuple[Any, ...]] = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        rows.append(
            (
                fact.get("fact_id", ""),
                fact.get("label", ""),
                _display_value(fact.get("value", "")),
                fact.get("unit", ""),
                "required" if fact.get("required") else "",
            )
        )
    return _table(("fact_id", "label", "value", "unit", "required"), rows)


def _display_value(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        rounded = round(value, 2)
        if rounded.is_integer():
            return f"{int(rounded):,}"
        return f"{rounded:,.2f}"
    return value


def _timing_table(raw_timing: Any) -> str:
    timing = raw_timing if isinstance(raw_timing, dict) else {}
    stages = timing.get("stages") if isinstance(timing.get("stages"), list) else []
    total = timing.get("total_elapsed_ms", "")
    rows: list[tuple[Any, ...]] = [("총 소요", total, "")]
    for item in stages:
        if not isinstance(item, dict):
            continue
        rows.append((item.get("name", ""), item.get("elapsed_ms", ""), item.get("detail", "")))
    return _table(("단계", "ms", "비고"), rows)


def _score_detail(detail: dict[str, Any]) -> str:
    row = detail.get("row") if isinstance(detail.get("row"), dict) else {}
    answer_match = detail.get("answer_match") if isinstance(detail.get("answer_match"), dict) else {}
    unmatched = answer_match.get("unmatched_mentions") if isinstance(answer_match.get("unmatched_mentions"), list) else []
    missing = answer_match.get("missing_required") if isinstance(answer_match.get("missing_required"), list) else []
    parts = [
        _table(
            ("항목", "값"),
            (
                ("schema_execution_ok", row.get("schema_execution_ok", "")),
                ("schema_intent_ok", row.get("schema_intent_ok", "")),
                ("degraded", row.get("degraded", "")),
                ("multistep_comparison_ok", row.get("multistep_comparison_ok", "")),
                ("query_fact_ok", row.get("query_fact_ok", "")),
                ("answer_fact_ok", row.get("answer_fact_ok", "")),
                ("answer_number_match_rate", row.get("answer_number_match_rate", "")),
                ("notes", row.get("notes", "")),
            ),
        )
    ]
    if unmatched:
        parts.append("<h4>불일치 답변 숫자</h4>")
        parts.append(
            _table(
                ("raw", "value", "unit", "context"),
                tuple((item.get("raw", ""), item.get("value", ""), item.get("unit", ""), item.get("context", "")) for item in unmatched if isinstance(item, dict)),
            )
        )
    if missing:
        parts.append("<h4>누락 필수 fact</h4>")
        parts.append(
            _table(
                ("fact_id", "label", "value", "unit"),
                tuple((item.get("fact_id", ""), item.get("label", ""), item.get("value", ""), item.get("unit", "")) for item in missing if isinstance(item, dict)),
            )
        )
    return "\n".join(parts)


def _table(headers: tuple[str, ...], rows: tuple[tuple[Any, ...], ...] | list[tuple[Any, ...]]) -> str:
    head = "<tr>" + "".join(f"<th>{html.escape(header)}</th>" for header in headers) + "</tr>"
    body = [
        "<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in row) + "</tr>"
        for row in rows
    ]
    return "<table>" + head + "".join(body) + "</table>"


def _head() -> str:
    return """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chat Answer Safety Visualization</title>
<style>
body{margin:0;background:#f6f7f9;color:#172033;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1320px;margin:0 auto;padding:28px 18px 48px}h1{margin:0 0 8px;font-size:30px}h2{font-size:21px;margin-top:0}h3{font-size:17px;margin-top:18px}h4{font-size:14px;margin:14px 0 4px}
p{line-height:1.55}.question{background:white;border:1px solid #e5e7eb;border-radius:8px;padding:16px;margin:18px 0}.answer{border:1px solid #e5e7eb;border-radius:8px;padding:12px;background:#fbfdff}
.meta-card{background:#eef6ff;border:1px solid #bfdbfe;border-radius:8px;padding:16px;margin:14px 0 20px}
table{border-collapse:collapse;width:100%;font-size:13px;background:white;margin:10px 0}td,th{border:1px solid #e5e7eb;padding:7px;text-align:left;vertical-align:top}
pre{white-space:pre-wrap;background:#111827;color:#f9fafb;border-radius:8px;padding:12px;overflow:auto;font-size:12px;line-height:1.45}
.badge{display:inline-block;border-radius:999px;padding:3px 8px;font-size:12px;margin:0 5px 5px 0;background:#e5e7eb}.ok{background:#dcfce7;color:#166534}.bad{background:#fee2e2;color:#991b1b}.na{background:#fef3c7;color:#854d0e}
</style></head>"""


def _load_details(path: Path) -> list[dict[str, Any]]:
    payload = _load_json(path)
    return payload if isinstance(payload, list) else []


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--details", required=True, type=Path)
    parser.add_argument("--summary", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


if __name__ == "__main__":
    main()
