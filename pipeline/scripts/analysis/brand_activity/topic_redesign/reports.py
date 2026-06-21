"""Markdown and tabular report rendering for the redesign deliverables."""

from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path

from .models import CoverageRow, JsonValue, LabelCandidate, MethodScore


def pct(value: float) -> str:
    """Format a ratio as a one-decimal percentage."""
    return f"{value * 100:.1f}%"


def markdown_table(headers: list[str], rows: list[list[str | int | float]]) -> str:
    """Render a GitHub-flavored Markdown table."""
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(_cell(value) for value in row) + " |")
    return "\n".join(lines)


def render_method_comparison(generated_at: str, scores: list[MethodScore]) -> str:
    """Render REDESIGN_01 method comparison and recommendation."""
    averages = _average_scores(scores)
    rows = [
        [method, len(items), pct(avg["coverage"]), pct(avg["noise"]), pct(avg["redundancy"]), f"{avg['score']:.3f}"]
        for method, items, avg in averages
    ]
    sections = [
        "# REDESIGN_01_METHOD_COMPARISON",
        "",
        f"- Generated: {generated_at}",
        "- Scope: local MariaDB read-only Keyword rows; no external LLM/API calls.",
        "- Scoring is a discovery heuristic, not manual-label F1.",
        "",
        "## Method Summary",
        "",
        markdown_table(["method", "market tests", "candidate coverage", "noise", "redundancy", "heuristic score"], rows),
        "",
        "## Recommendation",
        "",
        "**권고: `n-gram 빈출 + PMI 연어 + TF-IDF/SVD 군집` 조합을 시장별 규칙 사전으로 앵커링한다.** n-gram/PMI가 사람이 이해할 수 있는 라벨 후보를 만들고, 군집은 남는 미분류를 월간 discovery로 회수하는 보조 역할이 가장 적합했다.",
        "",
        "## Per-Market Evidence",
    ]
    for score in scores:
        sections.extend(
            [
                "",
                f"### {score.market} / {score.method}",
                "",
                markdown_table(
                    ["candidates", "coverage", "noise", "redundancy", "score", "top candidates", "note"],
                    [[score.candidate_count, pct(score.coverage_rate), pct(score.noise_rate), pct(score.redundancy_rate), f"{score.score:.3f}", ", ".join(score.top_candidates), score.note]],
                ),
            ]
        )
    return "\n".join(sections) + "\n"


def render_label_candidates(generated_at: str, candidates: list[LabelCandidate], market_counts: dict[str, int]) -> str:
    """Render REDESIGN_02 all-market candidate labels with snippets."""
    sections = [
        "# REDESIGN_02_LABEL_CANDIDATES",
        "",
        f"- Generated: {generated_at}",
        "- Sensitivity: representative sentences are limited PL-review snippets; do not redistribute as raw audit data.",
        "- Label names are provisional. PL/marketing must merge, rename, or delete candidates before operation.",
    ]
    by_market: defaultdict[str, list[LabelCandidate]] = defaultdict(list)
    for candidate in candidates:
        by_market[candidate.market].append(candidate)
    for market, count in sorted(market_counts.items(), key=lambda item: (-item[1], item[0])):
        sections.extend(["", f"## {market} ({count:,} Keyword rows)", ""])
        if count < 80:
            sections.append("> 소규모 시장: 통계적 발굴은 빈약하므로 후보를 키워드 사전 초안 중심으로만 해석해야 한다.")
            sections.append("")
        rows: list[list[str | int | float]] = []
        for candidate in by_market[market]:
            rows.append(
                [
                    candidate.label,
                    candidate.hit_count,
                    pct(candidate.coverage_rate),
                    candidate.source,
                    ", ".join(candidate.evidence_terms[:8]),
                    ", ".join(candidate.keywords[:10]),
                    "<br>".join(candidate.snippets) or "(대표 문장 부족)",
                ]
            )
        sections.append(markdown_table(["candidate label", "hits", "coverage", "source", "evidence", "draft keywords", "representative sentences"], rows))
    return "\n".join(sections) + "\n"


def render_dictionary_draft(generated_at: str, candidates: list[LabelCandidate], json_path: str) -> str:
    """Render REDESIGN_03 dictionary draft handoff."""
    rows = [
        [candidate.market, candidate.label, ", ".join(candidate.keywords), candidate.source, candidate.note]
        for candidate in sorted(candidates, key=lambda item: (item.market, item.label))
    ]
    sections = [
        "# REDESIGN_03_DICTIONARY_DRAFT",
        "",
        f"- Generated: {generated_at}",
        f"- Machine-readable JSON: `{json_path}`",
        "- Dictionary semantics: multi-label; a message can match every label whose keyword/phrase appears.",
        "",
        markdown_table(["ATC4", "provisional label", "keywords/regex draft", "source", "review note"], rows),
    ]
    return "\n".join(sections) + "\n"


def render_coverage_test(
    generated_at: str,
    coverage: list[CoverageRow],
    residual_terms: dict[str, list[tuple[str, int]]],
    first_poc_baseline: dict[str, float],
) -> str:
    """Render REDESIGN_04 coverage test and residual analysis."""
    baseline_avg = sum(first_poc_baseline.values()) / len(first_poc_baseline)
    avg_unmatched = sum(row.unmatched_rate for row in coverage) / len(coverage)
    weighted_unmatched = sum(row.unmatched_rows for row in coverage) / sum(row.rows for row in coverage)
    rows = [
        [row.market, row.rows, row.matched_rows, pct(row.unmatched_rate), pct(row.multilabel_rate), _baseline_delta(row, first_poc_baseline)]
        for row in coverage
    ]
    sections = [
        "# REDESIGN_04_COVERAGE_TEST",
        "",
        f"- Generated: {generated_at}",
        f"- Draft average unmatched rate: {pct(avg_unmatched)} simple mean, {pct(weighted_unmatched)} row-weighted.",
        f"- First PoC five-market baseline unmatched mean: {pct(baseline_avg)}; simple-mean improvement where comparable: {pct(max(0.0, baseline_avg - avg_unmatched))}.",
        "",
        "## Reclassification Coverage",
        "",
        markdown_table(["ATC4", "rows", "matched rows", "unmatched rate", "multilabel rate", "vs first PoC"], rows),
        "",
        "## Residual Unmatched Signals",
    ]
    for market, terms in residual_terms.items():
        sections.extend(["", f"### {market}", "", ", ".join(f"{term}({count})" for term, count in terms[:15]) or "none"])
    return "\n".join(sections) + "\n"


def render_eval_handoff(
    generated_at: str,
    coverage: list[CoverageRow],
    candidate_count: int,
    small_markets: tuple[str, ...],
) -> str:
    """Render REDESIGN_05 human confirmation and operational evaluation design."""
    unmatched_total = sum(row.unmatched_rows for row in coverage)
    total = sum(row.rows for row in coverage)
    sections = [
        "# REDESIGN_05_EVAL_AND_HANDOFF",
        "",
        f"- Generated: {generated_at}",
        "- External LLM/API calls in this PoC: none.",
        "",
        "## Human Confirmation Workflow",
        "",
        "1. PL reviews provisional labels per ATC4 and marks merge/rename/delete/keep.",
        "2. Marketing owner checks boundary examples for each kept label.",
        "3. Freeze label names and dictionary version, then rerun the coverage script.",
        "4. Use `기타/신규 후보` only for meaningful messages outside the approved label set; use `판단불가` only for vague copy.",
        "",
        "## Evaluation Set Design",
        "",
        "- Large markets: 80-100 stratified Keyword messages per ATC4.",
        "- Mid markets: 40-60 messages or all unique texts if fewer.",
        "- Small markets (`" + ", ".join(small_markets) + "`): full review.",
        "- Metrics: multi-label micro/macro precision, recall, F1, unmatched rate, 기타 rate, and 20% double-label inter-reviewer agreement.",
        "",
        "## Operational Classifier Recommendation",
        "",
        f"- Rule dictionary first pass covers the measured draft match set; unmatched estimate for optional LLM assist is {unmatched_total:,}/{total:,} rows ({pct(unmatched_total / total if total else 0.0)}).",
        "- GenOS Lite/Flash should be called only for unmatched or low-confidence messages after PL-approved label enums exist.",
        "- Prompt must use temperature 0, schema-constrained multi-label JSON, input hash cache, and quarantine on invalid JSON.",
        "",
        "## Handoff State",
        "",
        f"- Provisional label candidates: {candidate_count}",
        "- Ready for PL/marketing review: yes, with the caveat that labels are candidates, not final truth.",
    ]
    return "\n".join(sections) + "\n"


def dictionary_json(candidates: list[LabelCandidate]) -> dict[str, JsonValue]:
    """Serialize the draft dictionary without raw message text."""
    payload: dict[str, JsonValue] = {}
    for candidate in sorted(candidates, key=lambda item: (item.market, item.label)):
        market_payload = payload.setdefault(candidate.market, {})
        if isinstance(market_payload, dict):
            market_payload[candidate.label] = {
                "keywords": list(candidate.keywords),
                "source": candidate.source,
                "evidence_terms": list(candidate.evidence_terms),
                "hit_count": candidate.hit_count,
                "coverage_rate": round(candidate.coverage_rate, 4),
                "note": candidate.note,
            }
    return payload


def write_json(path: Path, value: JsonValue) -> None:
    """Write UTF-8 JSON with stable formatting."""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _cell(value: str | int | float) -> str:
    """Escape Markdown table cell text."""
    return str(value).replace("\n", "<br>").replace("|", "\\|")


def _average_scores(scores: list[MethodScore]) -> list[tuple[str, list[MethodScore], dict[str, float]]]:
    """Average method scores across sample markets."""
    by_method: defaultdict[str, list[MethodScore]] = defaultdict(list)
    for score in scores:
        by_method[score.method].append(score)
    averages: list[tuple[str, list[MethodScore], dict[str, float]]] = []
    for method, items in by_method.items():
        divisor = len(items) or 1
        averages.append(
            (
                method,
                items,
                {
                    "coverage": sum(item.coverage_rate for item in items) / divisor,
                    "noise": sum(item.noise_rate for item in items) / divisor,
                    "redundancy": sum(item.redundancy_rate for item in items) / divisor,
                    "score": sum(item.score for item in items) / divisor,
                },
            )
        )
    return sorted(averages, key=lambda item: -item[2]["score"])


def _baseline_delta(row: CoverageRow, baseline: dict[str, float]) -> str:
    """Render comparable first-PoC delta when that market existed in Track A."""
    previous = baseline.get(row.market)
    if previous is None:
        return "new/no first-PoC baseline"
    return f"{pct(previous)} -> {pct(row.unmatched_rate)} ({(row.unmatched_rate - previous) * 100:+.1f}pp)"

