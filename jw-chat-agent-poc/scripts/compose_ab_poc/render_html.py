from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any


def render_report(results_path: Path, summary_path: Path, output_path: Path) -> None:
    """Render a self-contained static HTML comparison report."""

    payload = json.loads(results_path.read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    parts = [_head(), "<body><main>", "<h1>도구 조합 A/B PoC 비교</h1>"]
    parts.append(f"<p class='meta'>생성: {html.escape(payload['generated_utc'])} · 데이터: {html.escape(payload['data_sha256'])}</p>")
    parts.append(_summary_table(summary))
    for item in payload["questions"]:
        parts.append("<section class='question'>")
        parts.append(f"<h2>{html.escape(item['qid'])}. {html.escape(item['question'])}</h2>")
        parts.append("<div class='grid'>")
        for approach in ("primitive", "query_spec"):
            run = item[approach]
            parts.append(_run_card(approach, run))
        parts.append("</div></section>")
    parts.append("</main></body></html>")
    output_path.write_text("\n".join(parts), encoding="utf-8")


def _head() -> str:
    return """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Compose A/B PoC</title>
<style>
body{margin:0;background:#f6f7f9;color:#172033;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1280px;margin:0 auto;padding:28px 18px 48px}h1{margin:0 0 8px;font-size:30px}h2{font-size:20px}
.meta{color:#667085}.summary,.card{background:white;border:1px solid #e5e7eb;border-radius:8px}
.summary{padding:14px;margin:18px 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:14px}
.card{padding:14px}.badge{display:inline-block;border-radius:999px;padding:3px 9px;font-size:12px;font-weight:700}
.ok{background:#dcfce7;color:#166534}.warn{background:#fef9c3;color:#854d0e}.bad{background:#fee2e2;color:#991b1b}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{border:1px solid #e5e7eb;padding:7px;text-align:left;vertical-align:top}
pre{white-space:pre-wrap;background:#111827;color:#f9fafb;border-radius:8px;padding:12px;overflow:auto;font-size:12px;line-height:1.45}
details{margin-top:10px}.question{margin:22px 0}.small{color:#667085;font-size:13px}
</style></head>"""


def _summary_table(summary: dict[str, Any]) -> str:
    rows = []
    for approach, metrics in summary["approaches"].items():
        rows.append(
            "<tr>"
            f"<td>{html.escape(approach)}</td>"
            f"<td>{metrics['intent_ok']}/{metrics['total']}</td>"
            f"<td>{metrics['raw_schema_ok']}/{metrics['total']}</td>"
            f"<td>{metrics['schema_ok']}/{metrics['total']}</td>"
            f"<td>{metrics['fact_ok']}/{metrics['total']}</td>"
            f"<td>{metrics['avg_steps']:.2f}</td>"
            f"<td>{metrics['avg_elapsed_ms']:.1f} ms</td>"
            f"<td>{metrics['llm_error_count']}</td>"
            "</tr>"
        )
    return (
        "<div class='summary'><table><thead><tr><th>방식</th><th>intent 정확도</th><th>raw schema</th><th>grounded schema</th><th>fact OK</th>"
        "<th>평균 step</th><th>평균 지연</th><th>LLM/parse 오류</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _run_card(approach: str, run: dict[str, Any]) -> str:
    cls = "ok" if run["intent_ok"] and run["status"] == "ok" else "warn" if run["status"] == "unsupported" else "bad"
    body = [
        "<div class='card'>",
        f"<span class='badge {cls}'>{html.escape(approach)}</span>",
        f"<p class='small'>intent={html.escape(run['llm_intent'])} · expected={html.escape(run['expected_intent'])} · "
        f"raw_schema_ok={run.get('llm_raw_schema_ok')} · schema_ok={run['llm_schema_ok']} · "
        f"status={html.escape(run['status'])} · elapsed={run['elapsed_ms']:.1f}ms · steps={run['step_count']}</p>",
        f"<p class='small'>grounding changes: {html.escape(str(run.get('grounding_changes', [])))}</p>",
        "<h3>최종 답변</h3>",
        _markdown_static(run["answer_md"]),
        "<details><summary>LLM raw / parsed / grounded JSON</summary>",
        f"<pre>{html.escape(run['llm_raw'])}</pre><pre>{html.escape(json.dumps(run['llm_json'], ensure_ascii=False, indent=2))}</pre>"
        f"<pre>{html.escape(json.dumps(run.get('llm_grounded_json', {}), ensure_ascii=False, indent=2))}</pre>",
        "</details><details><summary>trace</summary>",
        f"<pre>{html.escape(json.dumps(run['trace'], ensure_ascii=False, indent=2))}</pre>",
        "</details></div>",
    ]
    return "\n".join(body)


def _markdown_static(markdown: str) -> str:
    escaped = html.escape(markdown)
    lines = escaped.splitlines()
    out: list[str] = []
    in_code = False
    for line in lines:
        if line.startswith("```"):
            out.append("<pre>" if not in_code else "</pre>")
            in_code = not in_code
            continue
        if in_code:
            out.append(line)
        elif line.startswith("### "):
            out.append(f"<h3>{line[4:]}</h3>")
        elif line.strip():
            out.append(f"<p>{line}</p>")
    if in_code:
        out.append("</pre>")
    return "\n".join(out)
