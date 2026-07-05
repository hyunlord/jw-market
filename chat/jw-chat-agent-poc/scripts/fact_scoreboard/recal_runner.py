from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.compose_ab_poc.questions import QUESTIONS, EvalQuestion
from scripts.fact_scoreboard.gold import GoldStore
from scripts.fact_scoreboard.recalibration import CalibratedQuestion, calibrate_question
from scripts.fact_scoreboard.render_html import render_scoreboard
from scripts.fact_scoreboard.scoring import GoldFact, MatchResult, match_mentions
from scripts.fact_scoreboard.sse import parse_sse_file
from scripts.fact_scoreboard.text_numbers import NumericMention, extract_numeric_mentions

# noqa: SIZE_OK — audit runner keeps score rows, details, and static report together for reproducible handoff.


@dataclass(frozen=True, slots=True)
class FactMismatch:
    """Observed fact that did not match the independently recalculated value."""

    fact_id: str
    label: str
    observed_value: float
    gold_value: float | None
    unit: str
    delta: float | None
    reason: str


@dataclass(frozen=True, slots=True)
class FactComparison:
    """Exact-id comparison between trace facts and calibrated gold facts."""

    ok: bool
    match_rate: float
    matched: int
    total: int
    mismatches: tuple[FactMismatch, ...]


@dataclass(frozen=True, slots=True)
class RecalRow:
    """One calibrated scoreboard row."""

    qid: str
    question: str
    old_schema_ok: str
    old_query_fact_ok: str
    old_answer_fact_ok: str
    schema_execution_ok: str
    schema_intent_ok: str
    schema_ok: str
    degraded: str
    multistep_comparison_ok: str
    query_fact_ok: str
    answer_fact_ok: str
    query_fact_match_rate: float
    answer_number_match_rate: float
    answer_numbers: int
    unmatched_answer: int
    query_facts: int
    query_mismatches: int
    residual_class: str
    notes: str


@dataclass(frozen=True, slots=True)
class RecalInputs:
    """Filesystem inputs for recalibrating an existing operating capture."""

    mart_jsonl: Path
    sse_dir: Path
    trace_dir: Path
    previous_scoreboard: Path
    out_dir: Path


def main() -> None:
    """Re-score prior operating captures against calibrated independent mart facts."""

    inputs = _parse_args()
    inputs.out_dir.mkdir(parents=True, exist_ok=True)
    store = GoldStore.from_jsonl(inputs.mart_jsonl)
    previous_rows = _load_previous(inputs.previous_scoreboard)
    rows: list[RecalRow] = []
    details: list[dict[str, Any]] = []
    for question in QUESTIONS:
        calibrated = calibrate_question(store, question, inputs.trace_dir / f"{question.qid}.json")
        sse = parse_sse_file(inputs.sse_dir / f"{question.qid}.sse")
        answer_mentions = _material_answer_mentions(sse.answer_markdown)
        answer_match = match_mentions(_answer_facts(calibrated.facts), answer_mentions)
        query_match = compare_observed_facts(calibrated.facts, calibrated.trace_facts)
        previous = previous_rows.get(question.qid, {})
        row = _row(question, previous, calibrated, sse.answer_markdown, answer_mentions, answer_match, query_match)
        rows.append(row)
        details.append(_detail(row, calibrated, answer_mentions, answer_match, query_match, sse.answer_markdown, sse.timing))
    _write_csv(inputs.out_dir / "scoreboard_recal.csv", rows)
    _write_json(inputs.out_dir / "scoreboard_recal_summary.json", _summary(rows))
    _write_json(inputs.out_dir / "scoreboard_recal_details.json", details)
    markdown = _markdown_report(rows, details, inputs.mart_jsonl)
    (inputs.out_dir / "scoreboard_recal_report.md").write_text(markdown, encoding="utf-8")
    render_scoreboard(markdown, inputs.out_dir / "scoreboard_recal_report.html")


def compare_observed_facts(gold_facts: tuple[GoldFact, ...], observed_facts: tuple[GoldFact, ...]) -> FactComparison:
    """Compare trace facts to calibrated facts by exact fact id and unit."""

    by_id = {fact.fact_id: fact for fact in gold_facts}
    mismatches: list[FactMismatch] = []
    matched = 0
    for observed in observed_facts:
        gold = by_id.get(observed.fact_id)
        if gold is None:
            mismatches.append(FactMismatch(observed.fact_id, observed.label, observed.value, None, observed.unit, None, "missing calibrated fact"))
            continue
        if gold.unit != observed.unit:
            mismatches.append(FactMismatch(observed.fact_id, observed.label, observed.value, gold.value, observed.unit, None, "unit mismatch"))
            continue
        delta = observed.value - gold.value
        if abs(delta) > _tolerance(gold):
            mismatches.append(FactMismatch(observed.fact_id, observed.label, observed.value, gold.value, observed.unit, delta, "value mismatch"))
            continue
        matched += 1
    total = len(observed_facts)
    return FactComparison(
        ok=total > 0 and not mismatches,
        match_rate=matched / total if total else 0.0,
        matched=matched,
        total=total,
        mismatches=tuple(mismatches),
    )


def _row(
    question: EvalQuestion,
    previous: dict[str, str],
    calibrated: CalibratedQuestion,
    answer: str,
    mentions: tuple[NumericMention, ...],
    answer_match: MatchResult,
    query_match: FactComparison,
) -> RecalRow:
    schema_execution_ok = _yn(calibrated.schema_execution_ok)
    schema_intent_ok = _yn(calibrated.schema_intent_ok)
    schema_ok = _yn(calibrated.schema_execution_ok and calibrated.schema_intent_ok)
    degraded = _yn(_is_degraded_answer(answer))
    multistep_comparison_ok = _multistep_comparison_ok(question.intent_id, calibrated)
    query_fact_ok = _query_ok(query_match)
    answer_fact_ok = _answer_ok(answer_match, mentions)
    return RecalRow(
        qid=question.qid,
        question=question.question,
        old_schema_ok=previous.get("schema_ok", ""),
        old_query_fact_ok=previous.get("query_fact_ok", ""),
        old_answer_fact_ok=previous.get("answer_fact_ok", ""),
        schema_execution_ok=schema_execution_ok,
        schema_intent_ok=schema_intent_ok,
        schema_ok=schema_ok,
        degraded=degraded,
        multistep_comparison_ok=multistep_comparison_ok,
        query_fact_ok=query_fact_ok,
        answer_fact_ok=answer_fact_ok,
        query_fact_match_rate=query_match.match_rate,
        answer_number_match_rate=_answer_match_rate(answer_match, mentions),
        answer_numbers=len(mentions),
        unmatched_answer=len(answer_match.unmatched_mentions),
        query_facts=query_match.total,
        query_mismatches=len(query_match.mismatches),
        residual_class=_residual(schema_intent_ok, degraded, multistep_comparison_ok, query_fact_ok, answer_fact_ok),
        notes="; ".join(calibrated.population_notes),
    )


def _material_answer_mentions(answer: str) -> tuple[NumericMention, ...]:
    return tuple(mention for mention in extract_numeric_mentions(answer) if _is_material_answer_number(mention))


def _is_material_answer_number(mention: NumericMention) -> bool:
    if mention.unit != "count":
        return True
    context = mention.context
    return not any(token in context for token in ("상위", "최근", "개월", "표", "질문"))


def _answer_facts(facts: tuple[GoldFact, ...]) -> tuple[GoldFact, ...]:
    extras = [
        GoldFact(f"{fact.fact_id}:abs", f"{fact.label} magnitude", abs(fact.value), fact.unit, fact.question_id, False)
        for fact in facts
        if "delta" in fact.fact_id and fact.value < 0
    ]
    return facts + tuple(extras)


def _answer_ok(match: MatchResult, mentions: tuple[NumericMention, ...]) -> str:
    if not mentions:
        return "NA"
    return "Y" if match.answer_fact_ok else "N"


def _is_degraded_answer(answer: str) -> bool:
    markers = ("답변 생성이 지연", "검증된 fact만 최소", "최소 정리합니다")
    return any(marker in answer for marker in markers)


def _multistep_comparison_ok(intent_id: str, calibrated: CalibratedQuestion) -> str:
    match intent_id:
        case "market_vs_brand_feb":
            return _yn(_has_trace_fact(calibrated, "Jan-Feb"))
        case "atozet_threat" | "atozet_livaro_cross_trend":
            return _yn(_has_trace_fact(calibrated, "trend_compare"))
        case _:
            return "NA"


def _has_trace_fact(calibrated: CalibratedQuestion, token: str) -> bool:
    return any(token in fact.fact_id or token in fact.label for fact in calibrated.trace_facts)


def _query_ok(match: FactComparison) -> str:
    if match.total == 0:
        return "NA"
    return "Y" if match.ok else "N"


def _answer_match_rate(match: MatchResult, mentions: tuple[NumericMention, ...]) -> float:
    return len(match.matched) / len(mentions) if mentions else 0.0


def _residual(
    schema_intent_ok: str,
    degraded: str,
    multistep_comparison_ok: str,
    query_fact_ok: str,
    answer_fact_ok: str,
) -> str:
    if degraded == "Y":
        return "degraded_fallback"
    if multistep_comparison_ok == "N":
        return "multistep_comparison_missing"
    if schema_intent_ok == "N":
        return "schema_or_population_gap"
    if query_fact_ok == "N":
        return "query_logic_error"
    if answer_fact_ok == "N":
        return "llm_expression_error"
    if query_fact_ok == "NA" and answer_fact_ok == "NA":
        return "no_numeric_evidence"
    return "pass"


def _detail(
    row: RecalRow,
    calibrated: CalibratedQuestion,
    mentions: tuple[NumericMention, ...],
    answer_match: MatchResult,
    query_match: FactComparison,
    answer: str,
    timing: dict[str, object],
) -> dict[str, Any]:
    return {
        "row": asdict(row),
        "calibrated_facts": [asdict(fact) for fact in calibrated.facts],
        "trace_facts": [asdict(fact) for fact in calibrated.trace_facts],
        "answer_mentions": [asdict(mention) for mention in mentions],
        "answer_match": _match_dict(answer_match),
        "query_match": asdict(query_match),
        "answer_markdown": answer,
        "timing": timing,
    }


def _match_dict(result: MatchResult) -> dict[str, Any]:
    return {
        "ok": result.answer_fact_ok,
        "required_coverage": result.required_coverage,
        "matched": [asdict(item) for item in result.matched],
        "missing_required": [asdict(item) for item in result.missing_required],
        "unmatched_mentions": [asdict(item) for item in result.unmatched_mentions],
    }


def _summary(rows: list[RecalRow]) -> dict[str, Any]:
    total = len(rows)
    return {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total": total,
        "schema_execution_ok": sum(row.schema_execution_ok == "Y" for row in rows),
        "schema_intent_ok": sum(row.schema_intent_ok == "Y" for row in rows),
        "schema_ok": sum(row.schema_ok == "Y" for row in rows),
        "degraded": sum(row.degraded == "Y" for row in rows),
        "multistep_comparison_ok": sum(row.multistep_comparison_ok == "Y" for row in rows),
        "multistep_comparison_applicable": sum(row.multistep_comparison_ok != "NA" for row in rows),
        "query_fact_ok": sum(row.query_fact_ok == "Y" for row in rows),
        "query_fact_applicable": sum(row.query_fact_ok != "NA" for row in rows),
        "answer_fact_ok": sum(row.answer_fact_ok == "Y" for row in rows),
        "answer_fact_applicable": sum(row.answer_fact_ok != "NA" for row in rows),
        "avg_answer_match_rate": sum(row.answer_number_match_rate for row in rows) / total if total else 0.0,
        "residual_classes": _class_counts(rows),
    }


def _class_counts(rows: list[RecalRow]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.residual_class] = counts.get(row.residual_class, 0) + 1
    return counts


def _markdown_report(rows: list[RecalRow], details: list[dict[str, Any]], mart_jsonl: Path) -> str:
    summary = _summary(rows)
    lines = [
        "# Calibrated Chat Fact Scoreboard",
        f"- 생성: {summary['generated_utc']}",
        f"- mart snapshot: `{mart_jsonl}`",
        f"- schema_ok: {summary['schema_ok']}/{summary['total']} (execution {summary['schema_execution_ok']}, intent {summary['schema_intent_ok']})",
        f"- degraded fallback: {summary['degraded']}/{summary['total']}",
        f"- multistep_comparison_ok: {summary['multistep_comparison_ok']}/{summary['multistep_comparison_applicable']} applicable",
        f"- query_fact_ok: {summary['query_fact_ok']}/{summary['query_fact_applicable']} applicable",
        f"- answer_fact_ok: {summary['answer_fact_ok']}/{summary['answer_fact_applicable']} applicable",
        f"- 평균 answer number match rate: {summary['avg_answer_match_rate']:.3f}",
        f"- residual classes: `{json.dumps(summary['residual_classes'], ensure_ascii=False)}`",
        "",
        "## Question Summary",
        "| qid | old schema | schema | exec | intent | degraded | multistep | query | answer | ans match | residual | notes |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | ---: | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row.qid} | {row.old_schema_ok} | {row.schema_ok} | {row.schema_execution_ok} | {row.schema_intent_ok} | "
            f"{row.degraded} | {row.multistep_comparison_ok} | {row.query_fact_ok} | {row.answer_fact_ok} | "
            f"{row.answer_number_match_rate:.2f} | {row.residual_class} | {_cell(row.notes)} |"
        )
    lines.extend(["", "## Residual Details"])
    for detail in details:
        row = detail["row"]
        lines.extend(
            [
                f"### {row['qid']} {row['question']}",
                f"- schema: execution={row['schema_execution_ok']}, intent={row['schema_intent_ok']}, final={row['schema_ok']}",
                f"- degraded={row['degraded']}, multistep_comparison_ok={row['multistep_comparison_ok']}",
                f"- query_fact_ok={row['query_fact_ok']} ({row['query_mismatches']} mismatches / {row['query_facts']} trace facts)",
                f"- answer_fact_ok={row['answer_fact_ok']} ({row['unmatched_answer']} unmatched / {row['answer_numbers']} answer numbers)",
                f"- residual={row['residual_class']}",
                f"- notes: {_cell(row['notes']) or '-'}",
            ]
        )
        mismatches = detail["query_match"]["mismatches"][:5]
        if mismatches:
            lines.append("| fact_id | observed | gold | delta | reason |")
            lines.append("| --- | ---: | ---: | ---: | --- |")
            for item in mismatches:
                lines.append(
                    f"| {_cell(item['fact_id'])} | {item['observed_value']} | {item['gold_value']} | {item['delta']} | {_cell(item['reason'])} |"
                )
        unmatched = detail["answer_match"]["unmatched_mentions"][:8]
        if unmatched:
            lines.append("| unmatched answer raw | value | unit | context |")
            lines.append("| --- | ---: | --- | --- |")
            for item in unmatched:
                lines.append(f"| {_cell(item['raw'])} | {item['value']} | {item['unit']} | {_cell(item['context'])} |")
    return "\n".join(lines)


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _tolerance(fact: GoldFact) -> float:
    match fact.unit:
        case "percent":
            return 0.08
        case "eok":
            return 0.08
        case "rank":
            return 0.01
        case "count":
            return 1.0
        case "plain":
            return max(0.05, abs(fact.value) * 0.001)
        case unreachable:
            raise AssertionError(f"unreachable fact unit: {unreachable}")


def _yn(value: bool) -> str:
    return "Y" if value else "N"


def _load_previous(path: Path) -> dict[str, dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return {str(row["qid"]): dict(row) for row in csv.DictReader(handle)}


def _write_csv(path: Path, rows: list[RecalRow]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=tuple(asdict(rows[0]).keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def _write_json(path: Path, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_args() -> RecalInputs:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mart-jsonl", required=True, type=Path)
    parser.add_argument("--sse-dir", required=True, type=Path)
    parser.add_argument("--trace-dir", required=True, type=Path)
    parser.add_argument("--previous-scoreboard", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    return RecalInputs(args.mart_jsonl, args.sse_dir, args.trace_dir, args.previous_scoreboard, args.out_dir)


if __name__ == "__main__":
    main()
