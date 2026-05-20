#!/usr/bin/env python3
"""Render the current JW market data state into one self-contained HTML file."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from pipeline.scripts.viewers.collect_data_state import PROJECT_ROOT, collect_all


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JW Market Data State Viewer</title>
<style>
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    background: #f6f7f9;
    color: #1d1d1f;
    font-family: -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Segoe UI", sans-serif;
    letter-spacing: 0;
  }
  .top-bar {
    min-height: 72px;
    background: #1d1d1f;
    color: #fff;
    padding: 14px 24px;
    display: flex;
    gap: 20px;
    align-items: center;
    justify-content: space-between;
  }
  .top-bar h1 { margin: 0; font-size: 18px; font-weight: 700; }
  .meta { font-size: 12px; line-height: 1.55; color: #d8d8dc; text-align: right; }
  .tab-bar {
    height: 48px;
    background: #fff;
    border-bottom: 1px solid #dadce0;
    padding: 0 24px;
    display: flex;
    align-items: stretch;
    gap: 4px;
  }
  .tab {
    border: 0;
    border-bottom: 3px solid transparent;
    background: transparent;
    color: #4d5156;
    padding: 0 18px;
    font: inherit;
    font-size: 14px;
    cursor: pointer;
  }
  .tab.active { border-bottom-color: #0b66c3; color: #0b66c3; font-weight: 700; }
  .container { display: flex; min-height: calc(100vh - 120px); }
  .side-nav {
    width: 300px;
    flex: 0 0 300px;
    background: #fff;
    border-right: 1px solid #dadce0;
    padding: 14px 12px;
    overflow-y: auto;
  }
  .layer-group { margin-bottom: 18px; }
  .layer-group h3 {
    margin: 12px 8px 8px;
    color: #6b7280;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
  }
  .side-nav ul { list-style: none; margin: 0; padding: 0; }
  .side-nav li {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 10px;
    align-items: center;
    min-height: 34px;
    padding: 7px 9px;
    margin: 2px 0;
    border-radius: 8px;
    font-size: 13px;
    cursor: pointer;
  }
  .side-nav li:hover { background: #eef4fb; }
  .side-nav li.active { background: #0b66c3; color: #fff; }
  .table-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .row-count { font-size: 11px; opacity: 0.75; font-variant-numeric: tabular-nums; }
  .main-panel {
    flex: 1;
    min-width: 0;
    padding: 24px;
    overflow: auto;
  }
  .title-line {
    display: flex;
    gap: 12px;
    align-items: baseline;
    flex-wrap: wrap;
    margin-bottom: 14px;
  }
  h2 { margin: 0; font-size: 24px; }
  .badge {
    display: inline-flex;
    align-items: center;
    min-height: 22px;
    padding: 2px 8px;
    border-radius: 999px;
    background: #e8f0fe;
    color: #0b57d0;
    font-size: 12px;
    font-weight: 700;
  }
  .overview-card {
    display: grid;
    grid-template-columns: repeat(4, minmax(140px, 1fr));
    gap: 14px;
    background: #fff;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    padding: 18px;
    margin-bottom: 16px;
  }
  .stat label {
    display: block;
    margin-bottom: 5px;
    color: #6b7280;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
  }
  .stat strong {
    display: block;
    color: #111827;
    font-size: 24px;
    line-height: 1.1;
    overflow-wrap: anywhere;
  }
  details {
    background: #fff;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    padding: 14px 16px 16px;
    margin-bottom: 14px;
  }
  details summary {
    cursor: pointer;
    font-weight: 700;
    outline: none;
    list-style: none;
  }
  details summary::marker { content: ""; }
  details summary::-webkit-details-marker { display: none; }
  details summary::before {
    content: "▶";
    display: inline-block;
    margin-right: 7px;
    font-size: 10px;
    transition: transform 0.16s ease;
  }
  details[open] summary::before { transform: rotate(90deg); }
  details.jw-deep { border-left: 4px solid #f4b400; }
  .note { color: #6b7280; font-size: 12px; margin: 10px 0 0; }
  .sample-table-wrapper {
    width: 100%;
    max-height: 460px;
    overflow: auto;
    margin-top: 12px;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
  }
  table { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 12px; }
  th, td {
    border-right: 1px solid #e5e7eb;
    border-bottom: 1px solid #e5e7eb;
    padding: 7px 9px;
    text-align: left;
    vertical-align: top;
    max-width: 420px;
    overflow-wrap: anywhere;
  }
  th {
    position: sticky;
    top: 0;
    z-index: 1;
    background: #f3f4f6;
    color: #374151;
    font-weight: 700;
  }
  td.json-cell {
    background: #fafafa;
    color: #374151;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 11px;
    white-space: pre-wrap;
  }
  .schema-table .null-high { background: #fff6d7; }
  .schema-table .col-name { font-weight: 700; }
  .bar-chart { margin-top: 12px; }
  .bar-row {
    display: grid;
    grid-template-columns: minmax(160px, 260px) minmax(160px, 1fr) 90px;
    gap: 10px;
    align-items: center;
    min-height: 24px;
    margin: 6px 0;
    font-size: 12px;
  }
  .bar-label { overflow-wrap: anywhere; }
  .bar {
    height: 16px;
    background: #edf0f4;
    border-radius: 4px;
    overflow: hidden;
  }
  .bar-fill { height: 100%; background: linear-gradient(90deg, #0b66c3, #14b8a6); }
  .bar-value { text-align: right; font-variant-numeric: tabular-nums; }
  .storage-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 8px;
    margin-top: 12px;
    font-size: 12px;
  }
  .storage-item {
    padding: 8px 10px;
    border-radius: 8px;
    background: #f8fafc;
    border: 1px solid #e5e7eb;
  }
  .storage-item b { display: block; color: #6b7280; margin-bottom: 4px; }
  .error {
    border-left: 4px solid #d93025;
    background: #fce8e6;
    color: #7a1d16;
    padding: 12px;
    border-radius: 8px;
    margin-bottom: 16px;
  }
  @media (max-width: 860px) {
    .top-bar { align-items: flex-start; flex-direction: column; }
    .meta { text-align: left; }
    .container { flex-direction: column; }
    .side-nav { width: 100%; flex-basis: auto; max-height: 260px; border-right: 0; border-bottom: 1px solid #dadce0; }
    .overview-card { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .bar-row { grid-template-columns: 1fr; gap: 4px; }
  }
</style>
</head>
<body>
<header class="top-bar">
  <h1>JW Market Data State Viewer</h1>
  <div class="meta">
    Generated: __GENERATED_AT__<br>
    Repo commit: __REPO_COMMIT__ (__REPO_TAG__) | Total rows across all tables: __TOTAL_ROWS__
  </div>
</header>
<nav class="tab-bar" aria-label="Grouping">
  <button class="tab active" type="button" data-grouping="by-layer">By Layer</button>
  <button class="tab" type="button" data-grouping="by-purpose">By Purpose</button>
</nav>
<div class="container">
  <aside class="side-nav" id="sideNav"></aside>
  <main class="main-panel" id="mainPanel"></main>
</div>
<script>
const DATA = __DATA_JSON__;
const GROUP_LABELS = {
  layer_1_raw: "Layer 1 raw",
  layer_2_enriched: "Layer 2 enriched",
  layer_3_mart: "Layer 3 6 mart",
  catalog: "Catalog 7 table",
  ubist: "UBIST",
  iqvia: "IQVIA NSA",
  enriched: "Layer 2 enriched",
  mart: "Mart",
};

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function fmtNumber(value) {
  const number = Number(value || 0);
  return Number.isFinite(number) ? number.toLocaleString() : "0";
}

function groupLabel(key) {
  return GROUP_LABELS[key] || String(key || "unknown").replace(/_/g, " ");
}

function renderSideNav(grouping) {
  const nav = document.getElementById("sideNav");
  nav.innerHTML = "";
  const groups = {};
  for (const [name, info] of Object.entries(DATA.tables || {})) {
    const key = grouping === "by-layer" ? info.layer : info.purpose;
    if (!groups[key]) groups[key] = [];
    groups[key].push({ name, info });
  }
  const order = grouping === "by-layer"
    ? ["layer_1_raw", "layer_2_enriched", "layer_3_mart", "catalog"]
    : ["ubist", "iqvia", "enriched", "mart", "catalog"];
  for (const groupKey of order) {
    if (!groups[groupKey]) continue;
    const section = document.createElement("section");
    section.className = "layer-group";
    section.innerHTML = "<h3>" + escapeHtml(groupLabel(groupKey)) + "</h3>";
    const list = document.createElement("ul");
    for (const { name, info } of groups[groupKey]) {
      const item = document.createElement("li");
      item.dataset.table = name;
      item.innerHTML = "<span class=\"table-name\">" + escapeHtml(name) + "</span>"
        + "<span class=\"row-count\">" + fmtNumber(info.total_rows) + "</span>";
      item.onclick = () => renderTable(name);
      list.appendChild(item);
    }
    section.appendChild(list);
    nav.appendChild(section);
  }
  const first = nav.querySelector("li");
  if (first) renderTable(first.dataset.table);
}

function renderTable(tableName) {
  const table = DATA.tables[tableName];
  const panel = document.getElementById("mainPanel");
  document.querySelectorAll(".side-nav li").forEach(item => {
    item.classList.toggle("active", item.dataset.table === tableName);
  });
  if (!table) {
    panel.innerHTML = "<div class=\"error\">Missing table metadata.</div>";
    return;
  }
  let html = "<div class=\"title-line\"><h2>" + escapeHtml(tableName) + "</h2>"
    + "<span class=\"badge\">" + escapeHtml(groupLabel(table.layer)) + "</span></div>";
  if (table.error) {
    html += "<div class=\"error\">" + escapeHtml(table.error) + "</div>";
  }
  html += renderOverview(table);
  html += renderStorage(table.storage_info || {});
  if (table.schema && table.schema.length) html += renderSchema(table.schema);
  if (table.sample_rows && table.sample_rows.length) {
    html += "<details open><summary>Sample (" + table.sample_rows.length + " rows)</summary>"
      + renderRowsTable(table.sample_rows) + "</details>";
  }
  if (table.jw_deep_sample && table.jw_deep_sample.length) {
    html += "<details open class=\"jw-deep\"><summary>JW Brand Deep Sample ("
      + table.jw_deep_sample.length + " rows)</summary>"
      + "<p class=\"note\">리바로, 가드메트, 페린젝트 등 JW 주력 brand 적재 상태</p>"
      + renderRowsTable(table.jw_deep_sample) + "</details>";
  }
  if (table.distribution && Object.keys(table.distribution).length) {
    html += renderDistribution(table.distribution);
  }
  panel.innerHTML = html;
}

function renderOverview(table) {
  const size = table.storage_info && (table.storage_info.total_size_mb ?? table.storage_info.size_mb);
  return "<section class=\"overview-card\">"
    + "<div class=\"stat\"><label>Total Rows</label><strong>" + fmtNumber(table.total_rows) + "</strong></div>"
    + "<div class=\"stat\"><label>Total Columns</label><strong>" + fmtNumber(table.total_columns) + "</strong></div>"
    + "<div class=\"stat\"><label>Purpose</label><strong>" + escapeHtml(groupLabel(table.purpose)) + "</strong></div>"
    + "<div class=\"stat\"><label>Storage Size</label><strong>" + escapeHtml(size === undefined ? "n/a" : size + " MB") + "</strong></div>"
    + "</section>";
}

function renderStorage(storage) {
  const entries = Object.entries(storage).filter(([_, value]) => value !== null && value !== undefined && value !== "");
  if (!entries.length) return "";
  let html = "<details><summary>Storage / Collection Info</summary><div class=\"storage-grid\">";
  for (const [key, value] of entries) {
    html += "<div class=\"storage-item\"><b>" + escapeHtml(key.replace(/_/g, " ")) + "</b>"
      + escapeHtml(typeof value === "object" ? JSON.stringify(value) : value) + "</div>";
  }
  html += "</div></details>";
  return html;
}

function renderSchema(schema) {
  let html = "<details open><summary>Schema + Stats (" + schema.length + " columns)</summary>"
    + "<div class=\"sample-table-wrapper\"><table class=\"schema-table\"><thead><tr>"
    + "<th>Column</th><th>Type</th><th>Null Rate</th><th>Unique Count</th><th>Sample Values</th><th>Stats Scope</th>"
    + "</tr></thead><tbody>";
  for (const col of schema) {
    const nullRate = Number(col.null_rate || 0);
    html += "<tr><td class=\"col-name\">" + escapeHtml(col.name) + "</td>"
      + "<td>" + escapeHtml(col.type) + "</td>"
      + "<td class=\"" + (nullRate > 50 ? "null-high" : "") + "\">" + escapeHtml(nullRate.toFixed(2)) + "%</td>"
      + "<td>" + fmtNumber(col.unique_count) + "</td>"
      + "<td>" + escapeHtml((col.sample_values || []).join(", ").slice(0, 220)) + "</td>"
      + "<td>" + escapeHtml(col.stats_scope || "") + "</td></tr>";
  }
  html += "</tbody></table></div></details>";
  return html;
}

function renderRowsTable(rows) {
  if (!rows || !rows.length) return "<p>No data.</p>";
  const columns = Object.keys(rows[0]);
  let html = "<div class=\"sample-table-wrapper\"><table class=\"sample-table\"><thead><tr>";
  for (const col of columns) html += "<th>" + escapeHtml(col) + "</th>";
  html += "</tr></thead><tbody>";
  for (const row of rows) {
    html += "<tr>";
    for (const col of columns) {
      const value = row[col];
      const isObject = value !== null && typeof value === "object";
      const rendered = value === null || value === undefined
        ? "null"
        : isObject
          ? JSON.stringify(value, null, 1).slice(0, 800)
          : String(value).slice(0, 800);
      const isJson = !isObject && typeof value === "string" && /^[\\[{]/.test(value.trim());
      html += "<td" + (isObject || isJson ? " class=\"json-cell\"" : "") + ">" + escapeHtml(rendered) + "</td>";
    }
    html += "</tr>";
  }
  html += "</tbody></table></div>";
  return html;
}

function renderDistribution(distribution) {
  let html = "<details><summary>Distribution Charts</summary>";
  for (const [key, items] of Object.entries(distribution)) {
    if (!Array.isArray(items) || !items.length) continue;
    html += "<h4>" + escapeHtml(key.replace(/_/g, " ")) + "</h4>" + renderBarChart(items);
  }
  html += "</details>";
  return html;
}

function renderBarChart(items) {
  const values = items.map(item => Number(item.count ?? item.rows ?? 0));
  const maxValue = Math.max(...values, 1);
  let html = "<div class=\"bar-chart\">";
  for (const item of items.slice(0, 30)) {
    const value = Number(item.count ?? item.rows ?? 0);
    const label = Object.entries(item)
      .filter(([key]) => !["count", "rows", "file_size_mb"].includes(key))
      .map(([_, val]) => val)
      .join(" · ");
    html += "<div class=\"bar-row\"><div class=\"bar-label\">" + escapeHtml(label || "(blank)") + "</div>"
      + "<div class=\"bar\"><div class=\"bar-fill\" style=\"width:" + Math.round(value / maxValue * 100) + "%\"></div></div>"
      + "<div class=\"bar-value\">" + fmtNumber(value) + "</div></div>";
  }
  html += "</div>";
  return html;
}

document.querySelectorAll(".tab").forEach(tab => {
  tab.onclick = () => {
    document.querySelectorAll(".tab").forEach(item => item.classList.remove("active"));
    tab.classList.add("active");
    renderSideNav(tab.dataset.grouping);
  };
});

renderSideNav("by-layer");
</script>
</body>
</html>
"""


def html_safe_json_dumps(value: Any) -> str:
    dumped = json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    return (
        dumped.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def build_html(state: dict[str, Any], output_path: Path | str) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    repo_tag = state.get("repo_tag") or "no tag"
    repo_commit = str(state.get("repo_commit") or "unknown")[:8]
    html = (
        HTML_TEMPLATE.replace("__GENERATED_AT__", str(state.get("generated_at", "")))
        .replace("__REPO_COMMIT__", repo_commit)
        .replace("__REPO_TAG__", str(repo_tag))
        .replace("__TOTAL_ROWS__", f"{int(state.get('total_rows') or 0):,}")
        .replace("__DATA_JSON__", html_safe_json_dumps(state))
    )
    output.write_text(html, encoding="utf-8")
    print(f"Generated: {output}")
    print(f"Size: {os.path.getsize(output) / 1024 / 1024:.2f} MB")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="HTML output path. Defaults to viewer/data_state_<timestamp>.html")
    args = parser.parse_args()

    state = collect_all()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    output_path = args.output or (PROJECT_ROOT / "viewer" / f"data_state_{timestamp}.html")
    build_html(state, output_path)


if __name__ == "__main__":
    main()
