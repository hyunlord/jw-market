from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.fact_scoreboard.gold import GoldSet, load_gold_sets
from scripts.fact_scoreboard.render_html import render_scoreboard
from scripts.fact_scoreboard.scoring import MatchResult, match_mentions
from scripts.fact_scoreboard.sse import parse_sse_file
from scripts.fact_scoreboard.text_numbers import NumericMention, NumericUnit, extract_numeric_mentions


@dataclass(frozen=True, slots=True)
class ScoreRow:
    """Question-level fact scoreboard row."""

    qid: str
    question: str
    status: str
    schema_ok: str
    query_fact_ok: str
    answer_fact_ok: str
    required_coverage: float
    answer_numbers: int
    query_numbers: int
    missing_required: int
    unmatched_answer: int
    unmatched_query: int
    notes: str


def main() -> None:
    """Build the fact scoreboard from mart JSONL, SSE captures, and optional traces."""

    args = _parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    gold_sets = load_gold_sets(args.mart_jsonl)
    rows: list[ScoreRow] = []
    details: list[dict[str, Any]] = []
    for gold_set in gold_sets:
        qid = gold_set.question.qid
        sse_path = args.sse_dir / f"{qid}.sse"
        trace_path = args.trace_dir / f"{qid}.json" if args.trace_dir else None
        sse = parse_sse_file(sse_path)
        answer_mentions = extract_numeric_mentions(sse.answer_markdown)
        answer_match = match_mentions(gold_set.facts, answer_mentions)
        query_mentions = _query_mentions(trace_path, sse.charts)
        query_match = match_mentions(gold_set.facts, query_mentions)
        schema_ok = _schema_ok(trace_path, gold_set)
        row = ScoreRow(
            qid=qid,
            question=gold_set.question.question,
            status=gold_set.status,
            schema_ok=schema_ok,
            query_fact_ok=_query_ok_value(query_match, bool(query_mentions), gold_set.status),
            answer_fact_ok="Y" if answer_match.answer_fact_ok else "N",
            required_coverage=answer_match.required_coverage,
            answer_numbers=len(answer_mentions),
            query_numbers=len(query_mentions),
            missing_required=len(answer_match.missing_required),
            unmatched_answer=len(answer_match.unmatched_mentions),
            unmatched_query=len(query_match.unmatched_mentions),
            notes=gold_set.notes,
        )
        rows.append(row)
        details.append(_detail(gold_set, row, answer_match, query_match, answer_mentions, query_mentions, sse.answer_markdown, sse.timing))
    _write_json(args.out_dir / "scoreboard_details.json", details)
    _write_json(args.out_dir / "scoreboard_summary.json", _summary(rows))
    _write_csv(args.out_dir / "scoreboard.csv", rows)
    markdown = _markdown_report(rows, details, args.mart_jsonl)
    (args.out_dir / "scoreboard_report.md").write_text(markdown, encoding="utf-8")
    render_scoreboard(markdown, args.out_dir / "scoreboard_report.html")


def _query_mentions(trace_path: Path | None, charts: tuple[dict[str, object], ...]) -> tuple[NumericMention, ...]:
    mentions: list[NumericMention] = []
    if trace_path is not None and trace_path.is_file():
        mentions.extend(_trace_mentions(json.loads(trace_path.read_text(encoding="utf-8"))))
    for chart in charts:
        mentions.extend(_chart_mentions(chart))
    return tuple(mentions)


def _trace_mentions(payload: Any) -> list[NumericMention]:
    calls = payload.get("tool_calls") if isinstance(payload, dict) else []
    mentions: list[NumericMention] = []
    if not isinstance(calls, list):
        return mentions
    for call in calls:
        render_data = call.get("render_data") if isinstance(call, dict) else None
        if isinstance(render_data, dict):
            _flatten_render_data(render_data, mentions, "trace")
    return mentions


def _flatten_render_data(data: dict[str, Any], mentions: list[NumericMention], path: str) -> None:
    for key, value in data.items():
        child_path = f"{path}.{key}"
        if isinstance(value, dict):
            _flatten_render_data(value, mentions, child_path)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, dict):
                    _flatten_render_data(item, mentions, f"{child_path}[{index}]")
        elif isinstance(value, int | float):
            mention = _mention_from_key(key, float(value), child_path)
            if mention is not None:
                mentions.append(mention)


def _mention_from_key(key: str, value: float, context: str) -> NumericMention | None:
    lowered = key.lower()
    if "krw" in lowered:
        return NumericMention(raw=f"{value / 100_000_000:.2f}억원", value=value / 100_000_000, unit="eok", context=context)
    if "억원" in key or lowered in {"value_recent_eok", "sales_eok"}:
        return NumericMention(raw=f"{value:.2f}억원", value=value, unit="eok", context=context)
    if "pct" in lowered or "share" in lowered or lowered in {"market_share", "ms"}:
        return NumericMention(raw=f"{value:.2f}%", value=value, unit="percent", context=context)
    if lowered == "rank":
        return NumericMention(raw=f"{value:.0f}위", value=value, unit="rank", context=context)
    if "hhi" in lowered:
        return NumericMention(raw=f"{value:.2f}", value=value, unit="plain", context=context)
    return None


def _chart_mentions(chart: dict[str, object]) -> list[NumericMention]:
    labels = chart.get("labels")
    datasets = chart.get("datasets")
    mentions: list[NumericMention] = []
    if not isinstance(labels, list) or not isinstance(datasets, list):
        return mentions
    for dataset in datasets:
        if not isinstance(dataset, dict):
            continue
        unit = _unit_from_chart(dataset.get("unit") or chart.get("unit"))
        data = dataset.get("data")
        label = str(dataset.get("label") or chart.get("title") or "chart")
        if unit is None or not isinstance(data, list):
            continue
        for index, value in enumerate(data):
            if isinstance(value, int | float):
                period = labels[index] if index < len(labels) else index
                mentions.append(NumericMention(raw=f"{value}", value=float(value), unit=unit, context=f"chart:{label}:{period}"))
    return mentions


def _unit_from_chart(value: object) -> NumericUnit | None:
    text = str(value or "")
    if text == "%":
        return "percent"
    if text in {"억원", "KRW"}:
        return "eok"
    return None


def _schema_ok(trace_path: Path | None, gold_set: GoldSet) -> str:
    if trace_path is None or not trace_path.is_file():
        return "NA"
    payload = json.loads(trace_path.read_text(encoding="utf-8"))
    calls = payload.get("tool_calls") if isinstance(payload, dict) else []
    trace_text = json.dumps(calls, ensure_ascii=False)
    if gold_set.status == "unsupported":
        return "Y" if "unsupported" in trace_text or "미지원" in trace_text else "N"
    return "Y" if "query_result_id" in trace_text and "unsupported" not in trace_text else "N"


def _ok_value(ok: bool, has_mentions: bool) -> str:
    if not has_mentions:
        return "NA"
    return "Y" if ok else "N"


def _query_ok_value(match_result: MatchResult, has_mentions: bool, status: str) -> str:
    if status == "unsupported":
        return "Y" if not has_mentions else "N"
    if not has_mentions:
        return "NA"
    return "Y" if match_result.required_coverage >= 0.999 else "N"


def _detail(
    gold_set: GoldSet,
    row: ScoreRow,
    answer_match: MatchResult,
    query_match: MatchResult,
    answer_mentions: tuple[NumericMention, ...],
    query_mentions: tuple[NumericMention, ...],
    answer: str,
    timing: dict[str, object],
) -> dict[str, Any]:
    return {
        "row": asdict(row),
        "gold_facts": [asdict(fact) for fact in gold_set.facts],
        "answer_mentions": [asdict(mention) for mention in answer_mentions],
        "query_mentions": [asdict(mention) for mention in query_mentions],
        "answer_match": _match_dict(answer_match),
        "query_match": _match_dict(query_match),
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


def _summary(rows: list[ScoreRow]) -> dict[str, object]:
    total = len(rows)
    return {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total": total,
        "schema_ok": sum(row.schema_ok == "Y" for row in rows),
        "query_fact_ok": sum(row.query_fact_ok == "Y" for row in rows),
        "query_fact_applicable": sum(row.query_fact_ok != "NA" for row in rows),
        "answer_fact_ok": sum(row.answer_fact_ok == "Y" for row in rows),
        "avg_required_coverage": sum(row.required_coverage for row in rows) / total if total else 0.0,
    }


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_csv(path: Path, rows: list[ScoreRow]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=tuple(asdict(rows[0]).keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def _markdown_report(rows: list[ScoreRow], details: list[dict[str, Any]], mart_jsonl: Path) -> str:
    summary = _summary(rows)
    lines = [
        "# Chat Fact Scoreboard",
        f"- 생성: {summary['generated_utc']}",
        f"- mart snapshot: `{mart_jsonl}`",
        f"- schema_ok: {summary['schema_ok']}/{summary['total']}",
        f"- query_fact_ok: {summary['query_fact_ok']}/{summary['query_fact_applicable']} applicable",
        f"- answer_fact_ok: {summary['answer_fact_ok']}/{summary['total']}",
        f"- 평균 required coverage: {summary['avg_required_coverage']:.3f}",
        "",
        "| qid | schema_ok | query_fact_ok | answer_fact_ok | coverage | missing | unmatched answer | notes |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row.qid} | {row.schema_ok} | {row.query_fact_ok} | {row.answer_fact_ok} | "
            f"{row.required_coverage:.2f} | {row.missing_required} | {row.unmatched_answer} | {row.notes} |"
        )
    for detail in details:
        row = detail["row"]
        lines.extend(["", f"## {row['qid']} {row['question']}", f"- status: {row['status']} · schema_ok={row['schema_ok']} · query_fact_ok={row['query_fact_ok']} · answer_fact_ok={row['answer_fact_ok']}"])
        if detail["answer_match"]["missing_required"]:
            lines.append("### Missing required facts")
            for item in detail["answer_match"]["missing_required"][:10]:
                lines.append(f"- {item['label']}: {item['value']:.4f} {item['unit']}")
        if detail["answer_match"]["unmatched_mentions"]:
            lines.append("### Unmatched answer numbers")
            for item in detail["answer_match"]["unmatched_mentions"][:10]:
                lines.append(f"- {item['raw']} ({item['unit']}) context: {item['context']}")
        lines.extend(["### Answer markdown", "```", detail["answer_markdown"], "```"])
    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build mart-grounded fact scoreboard for chat answers.")
    parser.add_argument("--mart-jsonl", type=Path, required=True)
    parser.add_argument("--sse-dir", type=Path, required=True)
    parser.add_argument("--trace-dir", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    main()
