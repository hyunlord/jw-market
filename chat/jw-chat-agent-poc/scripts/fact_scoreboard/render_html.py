from __future__ import annotations

import html
from pathlib import Path


def render_scoreboard(report_markdown: str, output_path: Path) -> None:
    """Render a static, self-contained HTML report without client-side JSON parsing."""

    parts = [_head(), "<body><main>", _markdown(report_markdown), "</main></body></html>"]
    output_path.write_text("\n".join(parts), encoding="utf-8")


def render_markdown_fragment(markdown: str) -> str:
    """Render a markdown fragment to static HTML without client-side parsing."""

    return _markdown(markdown)


def _head() -> str:
    return """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chat Fact Scoreboard</title>
<style>
body{margin:0;background:#f6f7f9;color:#172033;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1280px;margin:0 auto;padding:28px 18px 48px}h1{margin:0 0 8px;font-size:30px}h2{font-size:21px;margin-top:28px}h3{font-size:17px}
p{line-height:1.55}.ok{color:#166534;font-weight:700}.bad{color:#991b1b;font-weight:700}.na{color:#854d0e;font-weight:700}
table{border-collapse:collapse;width:100%;font-size:13px;background:white;margin:12px 0}td,th{border:1px solid #e5e7eb;padding:7px;text-align:left;vertical-align:top}
pre{white-space:pre-wrap;background:#111827;color:#f9fafb;border-radius:8px;padding:12px;overflow:auto;font-size:12px;line-height:1.45}
code{background:#eef2f7;padding:1px 4px;border-radius:4px}.question{background:white;border:1px solid #e5e7eb;border-radius:8px;padding:14px;margin:16px 0}
</style></head>"""


def _markdown(markdown: str) -> str:
    lines = markdown.splitlines()
    out: list[str] = []
    in_table = False
    for line in lines:
        if line.startswith("# "):
            out.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.startswith("## "):
            out.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("### "):
            out.append(f"<h3>{html.escape(line[4:])}</h3>")
        elif line.startswith("|") and line.endswith("|"):
            if not in_table:
                out.append("<table>")
                in_table = True
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if all(set(cell) <= {"-", ":"} for cell in cells):
                continue
            tag = "th" if not any(part.startswith("<tr>") for part in out[-2:]) else "td"
            out.append("<tr>" + "".join(f"<{tag}>{_inline(cell)}</{tag}>" for cell in cells) + "</tr>")
        else:
            if in_table:
                out.append("</table>")
                in_table = False
            if line.strip():
                out.append(f"<p>{_inline(line)}</p>")
    if in_table:
        out.append("</table>")
    return "\n".join(out)


def _inline(text: str) -> str:
    escaped = html.escape(text)
    return escaped.replace("`", "")
