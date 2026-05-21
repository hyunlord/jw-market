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


DICTIONARY_PATH = Path(__file__).resolve().with_name("data_dictionary.json")


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
	  .sample-toggle-bar {
	    display: flex;
	    flex-wrap: wrap;
	    gap: 8px;
	    align-items: center;
	    margin: 10px 0 0;
	    color: #6b7280;
	    font-size: 11px;
	  }
	  .sample-toggle-bar button {
	    border: 1px solid #d1d5db;
	    border-radius: 6px;
	    background: #fff;
	    color: #374151;
	    padding: 4px 10px;
	    font: inherit;
	    font-size: 11px;
	    cursor: pointer;
	  }
	  .sample-toggle-bar button:hover { background: #eef4fb; }
	  .sample-toggle-bar button.active {
	    border-color: #0b66c3;
	    background: #0b66c3;
	    color: #fff;
	    font-weight: 700;
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
	    min-width: 150px;
	    white-space: pre-wrap;
	  }
	  .json-cell-clickable {
	    cursor: pointer;
	    position: relative;
	    transition: background 0.15s ease, box-shadow 0.15s ease;
	  }
	  .json-cell-clickable:hover {
	    background: #e8f0fe !important;
	    box-shadow: inset 0 0 0 1px #93c5fd;
	  }
	  .json-cell-clickable::after {
	    content: " open";
	    display: inline-block;
	    margin-left: 6px;
	    color: #0b66c3;
	    font-family: -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Segoe UI", sans-serif;
	    font-size: 10px;
	    font-weight: 700;
	    opacity: 0.55;
	  }
	  .json-cell-clickable:hover::after { opacity: 1; }
	  .json-cell-trigger {
	    display: block;
	    width: 100%;
	    min-width: 130px;
	    border: 0;
	    background: transparent;
	    color: inherit;
	    padding: 0;
	    font: inherit;
	    text-align: left;
	    cursor: pointer;
	  }
	  .json-open-label {
	    display: inline-block;
	    margin-bottom: 6px;
	    border: 1px solid #93c5fd;
	    border-radius: 999px;
	    background: #eff6ff;
	    color: #0b66c3;
	    padding: 2px 7px;
	    font-family: -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Segoe UI", sans-serif;
	    font-size: 10px;
	    font-weight: 700;
	  }
	  .json-cell-preview {
	    margin: 0;
	    max-height: 110px;
	    overflow: hidden;
	    font: inherit;
	    white-space: pre-wrap;
	  }
	  .sample-table-wrapper.wide-json td.json-cell { max-width: 800px; }
	  .sample-table-wrapper.wide-all td { max-width: 800px; }
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
  .dict-overview {
    background: #fff;
    padding: 20px;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    margin-bottom: 16px;
  }
  .dict-section h3 {
    margin: 0 0 8px;
    color: #6b7280;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
  }
  .dict-section p {
    margin: 0;
    font-size: 14px;
    line-height: 1.6;
  }
  .dict-row {
    display: grid;
    grid-template-columns: repeat(3, minmax(160px, 1fr));
    gap: 14px;
    margin-top: 18px;
    padding-top: 16px;
    border-top: 1px solid #eef0f3;
  }
  .dict-stat label {
    display: block;
    margin-bottom: 5px;
    color: #6b7280;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
  }
  .dict-stat span {
    display: block;
    font-size: 13px;
    line-height: 1.45;
    overflow-wrap: anywhere;
  }
  .dict-table .dict-col-name {
    width: 210px;
    color: #0b66c3;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-weight: 700;
  }
  .dict-table .dict-col-type {
    width: 130px;
    color: #6b7280;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 11px;
  }
  .dict-table .dict-col-desc {
    min-width: 320px;
    line-height: 1.55;
  }
  details.dict-sample { border-left: 4px solid #22c55e; }
  .dict-sample-box {
    margin-top: 12px;
    padding: 14px;
    border: 1px solid #bbf7d0;
    border-radius: 8px;
    background: #f0fdf4;
  }
  .dict-sample-box strong {
    display: block;
    margin: 12px 0 6px;
    color: #15803d;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
  }
  .dict-sample-box strong:first-child { margin-top: 0; }
  .dict-sample-box pre {
    margin: 0;
    padding: 12px;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    background: #fff;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 12px;
    line-height: 1.5;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .dict-sample-box p {
    margin: 0;
    font-size: 13px;
    line-height: 1.65;
  }
  .dict-notes {
    margin: 10px 0 0 18px;
    padding: 0;
  }
  .dict-notes li {
    margin: 6px 0;
    font-size: 13px;
    line-height: 1.6;
  }
  .shape-doc-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 12px;
    margin-top: 12px;
  }
  .shape-doc-card {
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    background: #f8fafc;
    padding: 12px;
  }
  .shape-doc-card h4 {
    margin: 0 0 6px;
    color: #0b66c3;
    font-size: 13px;
  }
  .shape-doc-card p {
    margin: 0 0 10px;
    color: #4b5563;
    font-size: 12px;
    line-height: 1.55;
  }
  .shape-json {
    margin: 0;
    max-height: 360px;
    overflow: auto;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    background: #fff;
    padding: 10px;
    color: #111827;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 11px;
    line-height: 1.5;
    white-space: pre-wrap;
  }
	  .sample-link {
	    margin-top: 12px;
	    border: 1px solid #0b66c3;
    border-radius: 8px;
    background: #fff;
    color: #0b66c3;
    padding: 8px 12px;
    font: inherit;
    font-size: 13px;
    font-weight: 700;
    cursor: pointer;
	  }
	  .sample-link:hover { background: #eef4fb; }
	  .json-modal {
	    position: fixed;
	    inset: 0;
	    z-index: 9999;
	    align-items: center;
	    justify-content: center;
	  }
	  .json-modal-backdrop {
	    position: absolute;
	    inset: 0;
	    background: rgba(17, 24, 39, 0.56);
	    backdrop-filter: blur(4px);
	  }
	  .json-modal-container {
	    position: relative;
	    width: 80vw;
	    height: 80vh;
	    max-width: 1400px;
	    min-width: min(720px, calc(100vw - 24px));
	    background: #fff;
	    border-radius: 12px;
	    box-shadow: 0 24px 70px rgba(0, 0, 0, 0.32);
	    display: flex;
	    flex-direction: column;
	    overflow: hidden;
	  }
	  .json-modal-header {
	    display: flex;
	    align-items: center;
	    justify-content: space-between;
	    gap: 12px;
	    padding: 14px 18px;
	    border-bottom: 1px solid #e5e7eb;
	    background: #f3f4f6;
	  }
	  .json-modal-title {
	    min-width: 0;
	    overflow: hidden;
	    text-overflow: ellipsis;
	    white-space: nowrap;
	    color: #111827;
	    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
	    font-size: 13px;
	    font-weight: 700;
	  }
	  .json-modal-actions {
	    display: flex;
	    align-items: center;
	    gap: 8px;
	    flex-shrink: 0;
	  }
	  .json-modal-actions input {
	    width: 220px;
	    border: 1px solid #d1d5db;
	    border-radius: 6px;
	    padding: 6px 10px;
	    font-size: 12px;
	    outline: none;
	  }
	  .json-modal-actions input:focus { border-color: #0b66c3; box-shadow: 0 0 0 2px #dbeafe; }
	  .json-modal-actions button {
	    border: 1px solid #d1d5db;
	    border-radius: 6px;
	    background: #fff;
	    color: #374151;
	    padding: 6px 10px;
	    font: inherit;
	    font-size: 11px;
	    cursor: pointer;
	    white-space: nowrap;
	  }
	  .json-modal-actions button:hover { background: #eef4fb; }
	  .json-modal-close {
	    min-width: 32px;
	    padding: 4px 9px !important;
	    font-size: 16px !important;
	    font-weight: 700;
	  }
	  .json-modal-body {
	    flex: 1;
	    overflow: auto;
	    padding: 18px 20px;
	    background: #fff;
	    color: #111827;
	    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
	    font-size: 12px;
	    line-height: 1.6;
	  }
	  .json-copy-fallback {
	    display: block;
	    width: 100%;
	    min-height: 180px;
	    margin: 0 0 14px;
	    padding: 10px 12px;
	    border: 1px solid #bfdbfe;
	    border-radius: 6px;
	    background: #eff6ff;
	    color: #111827;
	    font: inherit;
	    line-height: 1.45;
	    resize: vertical;
	  }
	  .json-modal-footer {
	    min-height: 34px;
	    padding: 8px 18px;
	    border-top: 1px solid #e5e7eb;
	    background: #f3f4f6;
	    color: #6b7280;
	    font-size: 11px;
	  }
	  .j-line {
	    min-height: 18px;
	    white-space: nowrap;
	    padding-left: 16px;
	  }
	  .j-toggle {
	    display: inline-block;
	    width: 14px;
	    margin-left: -16px;
	    margin-right: 2px;
	    color: #6b7280;
	    cursor: pointer;
	    font-size: 10px;
	    text-align: center;
	    user-select: none;
	  }
	  .j-toggle:hover { color: #0b66c3; }
	  .j-key { color: #0b66c3; font-weight: 700; }
	  .j-str { color: #b42318; }
	  .j-num { color: #15803d; }
	  .j-bool { color: #7c3aed; font-weight: 700; }
	  .j-null { color: #6b7280; font-style: italic; }
	  .j-bracket { color: #111827; font-weight: 700; }
	  .j-children {
	    margin-left: 16px;
	    padding-left: 6px;
	    border-left: 1px dotted #d1d5db;
	  }
	  .j-children.collapsed { display: none; }
	  .j-summary {
	    margin-left: 6px;
	    color: #6b7280;
	    font-size: 10px;
	    font-style: italic;
	  }
	  .j-match {
	    border-radius: 3px;
	    background: #fef3c7;
	    box-shadow: 0 0 0 1px #fde68a;
	  }
	  .j-match-current {
	    background: #fde047;
	    box-shadow: 0 0 0 2px #f59e0b;
	  }
	  @media (max-width: 860px) {
	    .top-bar { align-items: flex-start; flex-direction: column; }
	    .meta { text-align: left; }
	    .container { flex-direction: column; }
	    .side-nav { width: 100%; flex-basis: auto; max-height: 260px; border-right: 0; border-bottom: 1px solid #dadce0; }
	    .overview-card { grid-template-columns: repeat(2, minmax(0, 1fr)); }
	    .dict-row { grid-template-columns: 1fr; }
	    .json-modal-container { width: calc(100vw - 20px); height: calc(100vh - 20px); min-width: 0; }
	    .json-modal-header { align-items: stretch; flex-direction: column; }
	    .json-modal-actions { flex-wrap: wrap; }
	    .json-modal-actions input { width: 100%; }
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
  <button class="tab" type="button" data-grouping="data-dictionary">Data Dictionary</button>
</nav>
	<div class="container">
	  <aside class="side-nav" id="sideNav"></aside>
	  <main class="main-panel" id="mainPanel"></main>
	</div>
	<div id="jsonModal" class="json-modal" style="display:none;" aria-hidden="true">
	  <div class="json-modal-backdrop" data-json-modal-close="1"></div>
	  <section class="json-modal-container" role="dialog" aria-modal="true" aria-labelledby="jsonModalPath">
	    <header class="json-modal-header">
	      <div class="json-modal-title" id="jsonModalPath">table › column</div>
	      <div class="json-modal-actions">
	        <input type="text" id="jsonModalSearch" placeholder="Search key/value..." autocomplete="off">
	        <button id="jsonModalExpand" type="button" title="Expand all">⊞ All</button>
	        <button id="jsonModalCollapse" type="button" title="Collapse to roots">⊟ Roots</button>
	        <button id="jsonModalCopy" type="button" title="Copy JSON">Copy</button>
	        <button id="jsonModalClose" class="json-modal-close" type="button" title="Close">×</button>
	      </div>
	    </header>
	    <div class="json-modal-body" id="jsonModalBody"></div>
	    <footer class="json-modal-footer"><span id="jsonModalStats">-</span></footer>
	  </section>
	</div>
	<script>
const DATA = __DATA_JSON__;
const DICTIONARY = __DICTIONARY_JSON__;
const GROUP_LABELS = {
  layer_1_raw: "Layer 1 raw",
  layer_2_enriched: "Layer 2 enriched",
  layer_3_mart: "Layer 3 6 mart",
  layer_4_cache: "LAYER 4 CACHE",
  catalog: "Catalog 7 table",
  ubist: "UBIST",
  iqvia: "IQVIA NSA",
  enriched: "Layer 2 enriched",
  mart: "Mart",
  cache: "Cache",
	};
	const LAYER_4_ENTRY_ORDER = [
	  "cache_brands",
	  "cache_market_status",
	  "cache_cause",
	  "cache_deep_analysis",
	];
	let currentMode = "by-layer";
	let sampleTableSequence = 0;
	let jsonCellSequence = 0;
	let currentJsonObject = null;
	let currentSearchTerm = "";
	let searchMatches = [];
	let searchCursor = -1;
	const JSON_CELL_PAYLOADS = {};

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

function orderedGroupItems(groupKey, items) {
  if (groupKey !== "layer_4_cache") return items;
  return items.slice().sort((left, right) => {
    const leftRank = LAYER_4_ENTRY_ORDER.indexOf(left.name);
    const rightRank = LAYER_4_ENTRY_ORDER.indexOf(right.name);
    const normalizedLeftRank = leftRank === -1 ? 999 : leftRank;
    const normalizedRightRank = rightRank === -1 ? 999 : rightRank;
    if (normalizedLeftRank !== normalizedRightRank) return normalizedLeftRank - normalizedRightRank;
    return left.name.localeCompare(right.name);
  });
}

function renderSideNav(grouping) {
  currentMode = grouping;
  const nav = document.getElementById("sideNav");
  nav.innerHTML = "";
  const groups = {};
  for (const [name, info] of Object.entries(DATA.tables || {})) {
    const key = grouping === "by-layer" ? info.layer : info.purpose;
    if (!groups[key]) groups[key] = [];
    groups[key].push({ name, info });
  }
  const order = grouping === "by-layer"
    ? ["layer_1_raw", "layer_2_enriched", "layer_3_mart", "layer_4_cache", "catalog"]
    : ["ubist", "iqvia", "enriched", "mart", "cache", "catalog"];
  for (const groupKey of order) {
    if (!groups[groupKey]) continue;
    const section = document.createElement("section");
    section.className = "layer-group";
    section.innerHTML = "<h3>" + escapeHtml(groupLabel(groupKey)) + "</h3>";
    const list = document.createElement("ul");
    for (const { name, info } of orderedGroupItems(groupKey, groups[groupKey])) {
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

function renderSideNavDictionary() {
  currentMode = "data-dictionary";
  const nav = document.getElementById("sideNav");
  nav.innerHTML = "";
  const groups = {
    layer_1_raw: [],
    layer_2_enriched: [],
    layer_3_mart: [],
    layer_4_cache: [],
    catalog: [],
  };
  for (const [name, info] of Object.entries(DATA.tables || {})) {
    const layer = info.layer || "catalog";
    if (!groups[layer]) groups[layer] = [];
    groups[layer].push({ name, info });
  }
  for (const groupKey of ["layer_1_raw", "layer_2_enriched", "layer_3_mart", "layer_4_cache", "catalog"]) {
    if (!groups[groupKey] || !groups[groupKey].length) continue;
    const section = document.createElement("section");
    section.className = "layer-group";
    section.innerHTML = "<h3>" + escapeHtml(groupLabel(groupKey)) + "</h3>";
    const list = document.createElement("ul");
    for (const { name } of orderedGroupItems(groupKey, groups[groupKey])) {
      const item = document.createElement("li");
      item.dataset.table = name;
      const status = DICTIONARY[name] ? "dict" : "no dict";
      item.innerHTML = "<span class=\"table-name\">" + escapeHtml(name) + "</span>"
        + "<span class=\"row-count\">" + escapeHtml(status) + "</span>";
      item.onclick = () => renderTableDictionary(name);
      list.appendChild(item);
    }
    section.appendChild(list);
    nav.appendChild(section);
  }
  const first = nav.querySelector("li");
  if (first) renderTableDictionary(first.dataset.table);
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
  const breakdownRows = table.cache_breakdown || table.endpoint_view_breakdown;
  if (breakdownRows && breakdownRows.length) {
    html += renderCacheBreakdown(breakdownRows, tableName);
  }
	  if (table.sample_rows && table.sample_rows.length) {
	    html += "<details open><summary>Sample (" + table.sample_rows.length + " rows)</summary>"
	      + renderRowsTable(table.sample_rows, tableName) + "</details>";
	  }
	  if (table.jw_deep_sample && table.jw_deep_sample.length) {
	    html += "<details open class=\"jw-deep\"><summary>JW Brand Deep Sample ("
	      + table.jw_deep_sample.length + " rows)</summary>"
	      + "<p class=\"note\">리바로, 가드메트, 페린젝트 등 JW 주력 brand 적재 상태</p>"
	      + renderRowsTable(table.jw_deep_sample, tableName + " (JW deep)") + "</details>";
	  }
  if (table.distribution && Object.keys(table.distribution).length) {
    html += renderDistribution(table.distribution);
	  }
  const dictionary = DICTIONARY[tableName] || {};
  const shapeDocs = table.response_shape_documentation || dictionary.sample_interpretation;
  if (shapeDocs && !isSimpleSampleInterpretation(shapeDocs)) {
    html += renderResponseShapeDocumentation(shapeDocs);
  }
	  panel.innerHTML = html;
	  attachSampleWidthHandlers(panel);
	}

function schemaTypeMap(tableName) {
  const table = DATA.tables && DATA.tables[tableName];
  const typeMap = {};
  if (!table || !table.schema) return typeMap;
  for (const column of table.schema) {
    typeMap[column.name] = column.type;
  }
  return typeMap;
}

function renderTableDictionary(tableName) {
  const dict = DICTIONARY[tableName];
  const panel = document.getElementById("mainPanel");
  document.querySelectorAll(".side-nav li").forEach(item => {
    item.classList.toggle("active", item.dataset.table === tableName);
  });
  let html = "<div class=\"title-line\"><h2>" + escapeHtml(tableName) + "</h2>"
    + "<span class=\"badge\">Data Dictionary</span></div>";
  if (!dict) {
    panel.innerHTML = html + "<div class=\"error\">Data dictionary 없음. data_dictionary.json에 항목 추가 필요.</div>";
    return;
  }

  html += "<section class=\"dict-overview\">"
    + "<div class=\"dict-section\"><h3>Purpose</h3><p>" + escapeHtml(dict.purpose || "-") + "</p></div>"
    + "<div class=\"dict-row\">"
    + "<div class=\"dict-stat\"><label>Row Grain</label><span>" + escapeHtml(dict.row_grain || "-") + "</span></div>"
    + "<div class=\"dict-stat\"><label>Row Count Approx</label><span>" + escapeHtml(dict.row_count_approx || "-") + "</span></div>";
  if (dict.etl_source) {
    html += "<div class=\"dict-stat\"><label>ETL Source</label><span>" + escapeHtml(dict.etl_source) + "</span></div>";
  }
  html += "</div></section>";

  const columns = dict.columns || {};
  const typeMap = schemaTypeMap(tableName);
  if (Object.keys(columns).length) {
    html += "<details open><summary>Column Dictionary (" + Object.keys(columns).length + " columns)</summary>"
      + "<div class=\"sample-table-wrapper\"><table class=\"dict-table\"><thead><tr>"
      + "<th>Column</th><th>Type</th><th>설명 / 의미</th>"
      + "</tr></thead><tbody>";
    for (const [columnName, description] of Object.entries(columns)) {
      html += "<tr><td class=\"dict-col-name\">" + escapeHtml(columnName) + "</td>"
        + "<td class=\"dict-col-type\">" + escapeHtml(typeMap[columnName] || "?") + "</td>"
        + "<td class=\"dict-col-desc\">" + escapeHtml(description) + "</td></tr>";
    }
    html += "</tbody></table></div></details>";
  }

  if (dict.sample_interpretation) {
    html += renderDictionarySampleInterpretation(dict.sample_interpretation);
  }

  if (dict.notes && dict.notes.length) {
    html += "<details open><summary>Notes</summary><ul class=\"dict-notes\">";
    for (const note of dict.notes) {
      html += "<li>" + escapeHtml(note) + "</li>";
    }
    html += "</ul></details>";
  }

  html += "<details open><summary>Sample Rows</summary>"
    + "<p class=\"note\">동일 table의 실제 sample row 화면으로 이동합니다.</p>"
    + "<button class=\"sample-link\" type=\"button\" data-table=\"" + escapeHtml(tableName) + "\">By Layer 탭에서 sample 보기</button>"
    + "</details>";
  panel.innerHTML = html;
  const sampleButton = panel.querySelector(".sample-link");
  if (sampleButton) {
    sampleButton.onclick = () => switchToLayerTabAndSelect(tableName);
  }
}

function switchToLayerTabAndSelect(tableName) {
  const layerTab = document.querySelector(".tab[data-grouping=\"by-layer\"]");
  if (layerTab) layerTab.click();
  window.setTimeout(() => {
    for (const item of document.querySelectorAll(".side-nav li")) {
      if (item.dataset.table === tableName) {
        item.click();
        break;
      }
    }
  }, 0);
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

function renderCacheBreakdown(rows, tableName) {
  const hasEndpoint = rows.some(row => Object.prototype.hasOwnProperty.call(row, "endpoint"));
  const title = hasEndpoint ? "Endpoint × view_type breakdown" : "View/source/measure breakdown";
  const note = hasEndpoint
    ? "Cache rows and payload size by API endpoint and view_type."
    : "Cache rows and payload size by view_type, source, and measure where available.";
  return "<details open><summary>" + escapeHtml(title) + " (" + rows.length + " groups)</summary>"
    + "<p class=\"note\">" + escapeHtml(note) + "</p>"
    + renderRowsTable(rows, tableName + " cache breakdown") + "</details>";
}

function isSimpleSampleInterpretation(sample) {
  return !!(sample && (Object.prototype.hasOwnProperty.call(sample, "row_example")
    || Object.prototype.hasOwnProperty.call(sample, "meaning")));
}

function renderDictionarySampleInterpretation(sample) {
  if (isSimpleSampleInterpretation(sample)) {
    return "<details open class=\"dict-sample\"><summary>Sample Interpretation</summary>"
      + "<div class=\"dict-sample-box\"><strong>예시 row</strong><pre>"
      + escapeHtml(sample.row_example || "-")
      + "</pre><strong>해석</strong><p>"
      + escapeHtml(sample.meaning || "-")
      + "</p></div></details>";
  }
  return renderResponseShapeDocumentation(sample);
}

function renderResponseShapeDocumentation(docs) {
  const entries = Object.entries(docs || {}).filter(([key]) => !key.startsWith("_"));
  if (!entries.length) return "";
  let html = "<details open><summary>Response Shape Documentation</summary><div class=\"shape-doc-grid\">";
  for (const [endpoint, doc] of entries) {
    const description = doc && typeof doc === "object" && doc.description ? doc.description : "";
    const shape = doc && typeof doc === "object" && doc.shape ? doc.shape : doc;
    html += "<section class=\"shape-doc-card\"><h4>" + escapeHtml(endpoint) + "</h4>";
    if (description) html += "<p>" + escapeHtml(description) + "</p>";
    html += "<pre class=\"shape-json\">" + escapeHtml(JSON.stringify(shape, null, 2)) + "</pre></section>";
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

function looksLikeJsonString(value) {
  return typeof value === "string" && /^[\[{]/.test(value.trim());
}

function jsonPreview(value) {
  let rendered;
  if (value !== null && typeof value === "object") {
    rendered = JSON.stringify(value, null, 1);
  } else if (looksLikeJsonString(value)) {
    try {
      rendered = JSON.stringify(JSON.parse(value), null, 1);
    } catch (_) {
      rendered = String(value);
    }
  } else {
    rendered = String(value);
  }
  return rendered.length > 800 ? rendered.slice(0, 800) + "\n..." : rendered;
}

function renderRowsTable(rows, tableName) {
	  if (!rows || !rows.length) return "<p>No data.</p>";
	  const columns = Object.keys(rows[0]);
	  const tableId = "sample_table_" + (++sampleTableSequence);
	  let html = "<div class=\"sample-toggle-bar\" data-sample-table-id=\"" + tableId + "\">"
	    + "<span>Column width:</span>"
	    + "<button type=\"button\" class=\"active\" data-width-mode=\"normal\">Normal</button>"
	    + "<button type=\"button\" data-width-mode=\"wide-json\">Wide JSON</button>"
	    + "<button type=\"button\" data-width-mode=\"wide-all\">Wide All</button>"
	    + "</div>";
	  html += "<div class=\"sample-table-wrapper\" id=\"" + tableId + "\"><table class=\"sample-table\"><thead><tr>";
	  for (const col of columns) html += "<th>" + escapeHtml(col) + "</th>";
	  html += "</tr></thead><tbody>";
	  for (const row of rows) {
	    html += "<tr>";
	    for (const col of columns) {
	      const value = row[col];
	      const isObject = value !== null && typeof value === "object";
	      const isJson = isObject || looksLikeJsonString(value);
	      if (value === null || value === undefined) {
	        html += "<td><span style=\"color:#6b7280\">null</span></td>";
	      } else if (isJson) {
	        const jsonId = "json_payload_" + (++jsonCellSequence);
	        const path = (tableName || "table") + " › " + col;
	        JSON_CELL_PAYLOADS[jsonId] = value;
	        html += "<td class=\"json-cell json-cell-clickable\" title=\"Open JSON viewer\">"
	          + "<button type=\"button\" class=\"json-cell-trigger\" onclick=\"openJsonModal(JSON_CELL_PAYLOADS[this.dataset.jsonId], this.dataset.jsonPath || '')\" data-json-id=\"" + escapeHtml(jsonId)
	          + "\" data-json-path=\"" + escapeHtml(path) + "\">"
	          + "<span class=\"json-open-label\">Open JSON</span>"
	          + "<pre class=\"json-cell-preview\">" + escapeHtml(jsonPreview(value)) + "</pre></button></td>";
	      } else {
	        const rendered = String(value);
	        html += "<td>" + escapeHtml(rendered.length > 800 ? rendered.slice(0, 800) + "..." : rendered) + "</td>";
	      }
	    }
	    html += "</tr>";
	  }
	  html += "</tbody></table></div>";
	  return html;
	}

function attachSampleWidthHandlers(root) {
  root.querySelectorAll(".sample-toggle-bar").forEach(bar => {
    const tableId = bar.dataset.sampleTableId;
    bar.querySelectorAll("button[data-width-mode]").forEach(button => {
      button.onclick = () => setSampleWidth(tableId, button.dataset.widthMode || "normal");
    });
  });
}

function setSampleWidth(tableId, mode) {
  const wrapper = document.getElementById(tableId);
  if (!wrapper) return;
  wrapper.classList.remove("wide-json", "wide-all");
  if (mode === "wide-json") wrapper.classList.add("wide-json");
  if (mode === "wide-all") wrapper.classList.add("wide-all");
  const bar = document.querySelector(".sample-toggle-bar[data-sample-table-id=\"" + tableId + "\"]");
  if (bar) {
    bar.querySelectorAll("button[data-width-mode]").forEach(button => {
      button.classList.toggle("active", button.dataset.widthMode === mode);
    });
  }
}

function escCloseHandler(event) {
  if (event.key === "Escape") closeJsonModal();
}

function openJsonModal(jsonPayload, path) {
  const parsed = parseJsonPayload(jsonPayload);
  currentJsonObject = parsed;
  currentSearchTerm = "";
  searchMatches = [];
  searchCursor = -1;

  const modal = document.getElementById("jsonModal");
  document.getElementById("jsonModalPath").textContent = path || "JSON";
  document.getElementById("jsonModalSearch").value = "";
  renderJsonTree(parsed);
  modal.style.display = "flex";
  modal.setAttribute("aria-hidden", "false");
  document.addEventListener("keydown", escCloseHandler);
  document.getElementById("jsonModalSearch").focus();
}

function parseJsonPayload(jsonPayload) {
  if (typeof jsonPayload !== "string") return jsonPayload;
  try {
    return JSON.parse(jsonPayload);
  } catch (error) {
    const repaired = repairTruncatedJsonPrefix(jsonPayload);
    if (repaired) {
      repaired._viewer_note = "Original sample string was truncated during collection; this tree shows the complete JSON prefix available in the viewer.";
      return repaired;
    }
    return {
      _raw_string: String(jsonPayload),
      _parse_error: error.message,
    };
  }
}

function stripCollectionTruncationSuffix(text) {
  return String(text).replace(/\.\.\. \(\+\d+ chars truncated\)\s*$/, "");
}

function repairTruncatedJsonPrefix(text) {
  const source = stripCollectionTruncationSuffix(text).trim();
  const opener = source[0];
  const closer = opener === "{" ? "}" : opener === "[" ? "]" : "";
  if (!closer) return null;
  let inString = false;
  let escaped = false;
  let depth = 0;
  let lastTopLevelComma = -1;
  for (let i = 0; i < source.length; i++) {
    const ch = source[i];
    if (escaped) {
      escaped = false;
      continue;
    }
    if (ch === "\\") {
      escaped = true;
      continue;
    }
    if (ch === "\"") {
      inString = !inString;
      continue;
    }
    if (inString) continue;
    if (ch === "{" || ch === "[") depth += 1;
    else if (ch === "}" || ch === "]") depth -= 1;
    else if (ch === "," && depth === 1) lastTopLevelComma = i;
  }
  if (lastTopLevelComma <= 1) return null;
  const candidate = source.slice(0, lastTopLevelComma) + closer;
  try {
    return JSON.parse(candidate);
  } catch (_) {
    return null;
  }
}

function closeJsonModal() {
  const modal = document.getElementById("jsonModal");
  modal.style.display = "none";
  modal.setAttribute("aria-hidden", "true");
  document.removeEventListener("keydown", escCloseHandler);
}

function renderJsonTree(obj) {
  const body = document.getElementById("jsonModalBody");
  const stats = getJsonStats(obj);
  document.getElementById("jsonModalStats").textContent =
    stats.keys + " keys · " + stats.arrays + " arrays · " + stats.values + " values · depth " + stats.depth;
  body.innerHTML = renderJsonValue(obj, "", 0);
  attachJsonToggleHandlers();
}

function renderJsonValue(value, keyName, depth) {
  if (value === null) {
    return wrapJsonLine(jsonKeyPrefix(keyName) + "<span class=\"j-null\">null</span>");
  }
  if (value === undefined) {
    return wrapJsonLine(jsonKeyPrefix(keyName) + "<span class=\"j-null\">undefined</span>");
  }
  if (typeof value === "boolean") {
    return wrapJsonLine(jsonKeyPrefix(keyName) + "<span class=\"j-bool\">" + value + "</span>");
  }
  if (typeof value === "number") {
    return wrapJsonLine(jsonKeyPrefix(keyName) + "<span class=\"j-num\">" + formatJsonNumber(value) + "</span>");
  }
  if (typeof value === "string") {
    return wrapJsonLine(jsonKeyPrefix(keyName) + "<span class=\"j-str\">\"" + escapeHtml(value) + "\"</span>");
  }
  if (Array.isArray(value)) {
    if (!value.length) {
      return wrapJsonLine(jsonKeyPrefix(keyName) + "<span class=\"j-bracket\">[]</span>");
    }
    const collapsed = depth >= 2;
    let html = wrapJsonLine(
      "<span class=\"j-toggle\">" + (collapsed ? "▶" : "▼") + "</span>"
      + jsonKeyPrefix(keyName)
      + "<span class=\"j-bracket\">[</span><span class=\"j-summary\">[" + value.length + " items]</span>"
    );
    html += "<div class=\"j-children" + (collapsed ? " collapsed" : "") + "\">";
    for (const item of value) {
      html += renderJsonValue(item, null, depth + 1);
    }
    html += "</div>" + wrapJsonLine("<span class=\"j-bracket\">]</span>");
    return html;
  }
  if (typeof value === "object") {
    const keys = Object.keys(value);
    if (!keys.length) {
      return wrapJsonLine(jsonKeyPrefix(keyName) + "<span class=\"j-bracket\">{}</span>");
    }
    const collapsed = depth >= 2;
    let html = wrapJsonLine(
      "<span class=\"j-toggle\">" + (collapsed ? "▶" : "▼") + "</span>"
      + jsonKeyPrefix(keyName)
      + "<span class=\"j-bracket\">{</span><span class=\"j-summary\">{" + keys.length + " keys}</span>"
    );
    html += "<div class=\"j-children" + (collapsed ? " collapsed" : "") + "\">";
    for (const key of keys) {
      html += renderJsonValue(value[key], key, depth + 1);
    }
    html += "</div>" + wrapJsonLine("<span class=\"j-bracket\">}</span>");
    return html;
  }
  return wrapJsonLine(jsonKeyPrefix(keyName) + escapeHtml(String(value)));
}

function jsonKeyPrefix(keyName) {
  if (keyName === null || keyName === undefined || keyName === "") return "";
  return "<span class=\"j-key\">\"" + escapeHtml(String(keyName)) + "\"</span>: ";
}

function wrapJsonLine(content) {
  return "<div class=\"j-line\">" + content + "</div>";
}

function formatJsonNumber(numberValue) {
  if (Number.isInteger(numberValue) && Math.abs(numberValue) >= 1000) {
    return numberValue.toLocaleString();
  }
  return String(numberValue);
}

function attachJsonToggleHandlers() {
  document.querySelectorAll(".json-modal-body .j-toggle").forEach(toggle => {
    toggle.onclick = event => {
      event.stopPropagation();
      const children = toggle.parentElement ? toggle.parentElement.nextElementSibling : null;
      if (!children || !children.classList.contains("j-children")) return;
      children.classList.toggle("collapsed");
      toggle.textContent = children.classList.contains("collapsed") ? "▶" : "▼";
    };
  });
}

function expandAllJson() {
  document.querySelectorAll(".json-modal-body .j-children").forEach(children => children.classList.remove("collapsed"));
  document.querySelectorAll(".json-modal-body .j-toggle").forEach(toggle => {
    toggle.textContent = "▼";
  });
}

function collapseAllJson() {
  const body = document.getElementById("jsonModalBody");
  body.querySelectorAll(".j-children").forEach((children, index) => {
    children.classList.toggle("collapsed", index !== 0);
  });
  body.querySelectorAll(".j-toggle").forEach((toggle, index) => {
    toggle.textContent = index === 0 ? "▼" : "▶";
  });
}

function fallbackCopyText(text) {
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.top = "0";
  textarea.style.left = "0";
  textarea.style.width = "2px";
  textarea.style.height = "2px";
  textarea.style.opacity = "0.01";
  textarea.style.zIndex = "10000";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("document.execCommand returned false");
}

function showCopyFallback(text) {
  const body = document.getElementById("jsonModalBody");
  let textarea = document.getElementById("jsonCopyFallback");
  if (!textarea) {
    textarea = document.createElement("textarea");
    textarea.id = "jsonCopyFallback";
    textarea.className = "json-copy-fallback";
    textarea.setAttribute("readonly", "");
    textarea.setAttribute("aria-label", "Selected JSON content for copy");
    body.insertBefore(textarea, body.firstChild);
  }
  textarea.value = text;
  textarea.focus();
  textarea.select();
}

async function copyJsonContent() {
  if (currentJsonObject === null || currentJsonObject === undefined) return;
  const button = document.getElementById("jsonModalCopy");
  const original = button.textContent;
  const text = JSON.stringify(currentJsonObject, null, 2);
  try {
    fallbackCopyText(text);
    button.textContent = "Copied";
  } catch (error) {
    try {
      if (!navigator.clipboard || !navigator.clipboard.writeText) throw error;
      await navigator.clipboard.writeText(text);
      button.textContent = "Copied";
    } catch (clipboardError) {
      showCopyFallback(text);
      button.textContent = "Selected";
      document.getElementById("jsonModalStats").textContent =
        "Clipboard permission denied; JSON text selected in the modal.";
    }
  }
  window.setTimeout(() => {
    button.textContent = original;
  }, 1400);
}

function getJsonStats(obj, stats, depth) {
  const currentStats = stats || { keys: 0, arrays: 0, values: 0, depth: 0 };
  const currentDepth = depth || 0;
  currentStats.depth = Math.max(currentStats.depth, currentDepth);
  if (obj === null || typeof obj !== "object") {
    currentStats.values += 1;
    return currentStats;
  }
  if (Array.isArray(obj)) {
    currentStats.arrays += 1;
    for (const item of obj) getJsonStats(item, currentStats, currentDepth + 1);
  } else {
    const keys = Object.keys(obj);
    currentStats.keys += keys.length;
    for (const key of keys) getJsonStats(obj[key], currentStats, currentDepth + 1);
  }
  return currentStats;
}

function applySearchHighlight() {
  if (currentJsonObject === null || currentJsonObject === undefined) return;
  currentSearchTerm = document.getElementById("jsonModalSearch").value.trim().toLowerCase();
  renderJsonTree(currentJsonObject);
  searchMatches = [];
  searchCursor = -1;
  if (!currentSearchTerm) return;

  const body = document.getElementById("jsonModalBody");
  const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
  const nodes = [];
  let node;
  while ((node = walker.nextNode())) nodes.push(node);

  for (const textNode of nodes) {
    const text = textNode.nodeValue || "";
    const lower = text.toLowerCase();
    let index = lower.indexOf(currentSearchTerm);
    if (index < 0) continue;
    const fragment = document.createDocumentFragment();
    let cursor = 0;
    while (index >= 0) {
      if (index > cursor) {
        fragment.appendChild(document.createTextNode(text.slice(cursor, index)));
      }
      const match = document.createElement("span");
      match.className = "j-match";
      match.textContent = text.slice(index, index + currentSearchTerm.length);
      fragment.appendChild(match);
      searchMatches.push(match);
      cursor = index + currentSearchTerm.length;
      index = lower.indexOf(currentSearchTerm, cursor);
    }
    if (cursor < text.length) fragment.appendChild(document.createTextNode(text.slice(cursor)));
    textNode.parentNode.replaceChild(fragment, textNode);
  }

  searchMatches.forEach(match => expandJsonAncestors(match));
  if (searchMatches.length) {
    nextSearchMatch();
  } else {
    document.getElementById("jsonModalStats").textContent = "No matches";
  }
}

function expandJsonAncestors(element) {
  let parent = element.parentElement;
  while (parent) {
    if (parent.classList && parent.classList.contains("j-children")) {
      parent.classList.remove("collapsed");
      const opener = parent.previousElementSibling;
      if (opener) {
        const toggle = opener.querySelector(".j-toggle");
        if (toggle) toggle.textContent = "▼";
      }
    }
    parent = parent.parentElement;
  }
}

function nextSearchMatch() {
  if (!searchMatches.length) return;
  if (searchCursor >= 0 && searchMatches[searchCursor]) {
    searchMatches[searchCursor].classList.remove("j-match-current");
  }
  searchCursor = (searchCursor + 1) % searchMatches.length;
  const match = searchMatches[searchCursor];
  match.classList.add("j-match-current");
  match.scrollIntoView({ behavior: "smooth", block: "center" });
  document.getElementById("jsonModalStats").textContent =
    (searchCursor + 1) + " / " + searchMatches.length + " matches";
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

document.getElementById("jsonModalSearch").addEventListener("input", applySearchHighlight);
document.getElementById("jsonModalSearch").addEventListener("keydown", event => {
  if (event.key === "Enter") {
    event.preventDefault();
    nextSearchMatch();
  }
});
document.getElementById("jsonModalExpand").onclick = expandAllJson;
document.getElementById("jsonModalCollapse").onclick = collapseAllJson;
document.getElementById("jsonModalCopy").onclick = copyJsonContent;
document.getElementById("jsonModalClose").onclick = closeJsonModal;
document.querySelector("[data-json-modal-close]").onclick = closeJsonModal;
document.getElementById("mainPanel").addEventListener("click", event => {
  const trigger = event.target && event.target.closest ? event.target.closest(".json-cell-trigger") : null;
  if (!trigger || !document.getElementById("mainPanel").contains(trigger)) return;
  const payload = JSON_CELL_PAYLOADS[trigger.dataset.jsonId];
  openJsonModal(payload, trigger.dataset.jsonPath || "");
});

document.querySelectorAll(".tab").forEach(tab => {
  tab.onclick = () => {
    document.querySelectorAll(".tab").forEach(item => item.classList.remove("active"));
    tab.classList.add("active");
    currentMode = tab.dataset.grouping;
    if (currentMode === "data-dictionary") {
      renderSideNavDictionary();
    } else {
      renderSideNav(currentMode);
    }
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


def load_dictionary(path: Path | str | None = None) -> dict[str, Any]:
    dictionary_path = Path(path) if path is not None else DICTIONARY_PATH
    if not dictionary_path.exists():
        return {}
    with dictionary_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def build_html(state: dict[str, Any], output_path: Path | str, *, dictionary_path: Path | str | None = None) -> Path:
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
        .replace("__DICTIONARY_JSON__", html_safe_json_dumps(load_dictionary(dictionary_path)))
    )
    output.write_text(html, encoding="utf-8")
    print(f"Generated: {output}")
    print(f"Size: {os.path.getsize(output) / 1024 / 1024:.2f} MB")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, help="Optional pre-collected JSON state path.")
    parser.add_argument("--dictionary", type=Path, help="Optional data dictionary JSON path.")
    parser.add_argument("--output", type=Path, help="HTML output path. Defaults to viewer/data_state_<timestamp>.html")
    args = parser.parse_args()

    if args.data:
        with args.data.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
    else:
        state = collect_all()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    output_path = args.output or (PROJECT_ROOT / "viewer" / f"data_state_{timestamp}.html")
    build_html(state, output_path, dictionary_path=args.dictionary)


if __name__ == "__main__":
    main()
