from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.compose_ab_poc.analyses import execute_intent, primitive_trace, query_spec_trace
from scripts.compose_ab_poc.catalog import CompositionCatalog
from scripts.compose_ab_poc.grounding import ground_plan
from scripts.compose_ab_poc.llm import GenosJsonClient
from scripts.compose_ab_poc.mart_store import MartStore
from scripts.compose_ab_poc.models import Approach, ApproachRun
from scripts.compose_ab_poc.questions import INTENT_DESCRIPTIONS, QUESTIONS
from scripts.compose_ab_poc.render_html import render_report


def main() -> None:
    """Run both composition approaches over the fixed evaluation set."""

    args = _parse_args()
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    store = MartStore.from_tsv(args.data_tsv)
    catalog = CompositionCatalog.from_store(store)
    data_sha = hashlib.sha256(args.data_tsv.read_bytes()).hexdigest()
    client = GenosJsonClient(base_url=args.genos_base_url, catalog=catalog) if args.use_llm else None
    runs = [_run_question(store, catalog, client, question, approach) for question in QUESTIONS for approach in ("primitive", "query_spec")]
    payload = _payload(runs, data_sha)
    results_path = out_dir / "compose_ab_results.json"
    summary_path = out_dir / "compose_ab_summary.json"
    results_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_path.write_text(json.dumps(_summary(runs), ensure_ascii=False, indent=2), encoding="utf-8")
    render_report(results_path, summary_path, out_dir / "compose_ab_report.html")


def _run_question(store: MartStore, catalog: CompositionCatalog, client: GenosJsonClient | None, question: Any, approach: Approach) -> ApproachRun:
    run = ApproachRun(question.qid, approach, question.question, question.intent_id)
    started = time.perf_counter()
    try:
        if client is None:
            raw = json.dumps(_offline_plan(question.intent_id, approach), ensure_ascii=False)
            parsed = json.loads(raw)
        else:
            raw, parsed = client.plan(question.question, approach)
        run.llm_raw = raw
        run.llm_json = parsed
        run.llm_parse_ok = True
        grounded = ground_plan(parsed, approach, catalog)
        run.llm_raw_schema_errors = list(grounded.raw_errors)
        run.llm_raw_schema_ok = not run.llm_raw_schema_errors
        run.llm_grounded_json = grounded.plan
        run.grounding_changes = list(grounded.changes)
        run.llm_schema_errors = list(grounded.final_errors)
        run.llm_schema_ok = not run.llm_schema_errors
        run.llm_intent = str(grounded.plan.get("intent_id") or "")
        if run.llm_intent not in INTENT_DESCRIPTIONS:
            raise ValueError(f"unknown intent_id: {run.llm_intent}")
        run.analysis = execute_intent(store, run.llm_intent)
        run.trace = primitive_trace(run.llm_intent, run.analysis) if approach == "primitive" else query_spec_trace(run.llm_intent, run.analysis)
    except Exception as exc:  # noqa: BLE001 - PoC must capture failure shape.
        run.llm_error = type(exc).__name__ + ": " + str(exc)
        if not run.llm_intent:
            run.llm_intent = "parse_error"
    run.elapsed_ms = (time.perf_counter() - started) * 1000
    return run


def _offline_plan(intent_id: str, approach: Approach) -> dict[str, Any]:
    if approach == "primitive":
        return {"intent_id": intent_id, "steps": [{"tool": "fetch", "args": {"market": "ml_006"}}, {"tool": "compute_series", "args": {"intent_id": intent_id}}]}
    return {"intent_id": intent_id, "spec": {"source": "ubist", "view": "market_landscape", "market": "ml_006", "derive": ["trend"]}}


def _payload(runs: list[ApproachRun], data_sha: str) -> dict[str, Any]:
    by_qid: dict[str, dict[str, Any]] = {}
    for run in runs:
        item = by_qid.setdefault(run.qid, {"qid": run.qid, "question": run.question})
        item[run.approach] = _run_dict(run)
    return {
        "generated_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_sha256": data_sha,
        "questions": list(by_qid.values()),
    }


def _run_dict(run: ApproachRun) -> dict[str, Any]:
    analysis = run.analysis
    return {
        "expected_intent": run.expected_intent,
        "llm_intent": run.llm_intent,
        "intent_ok": run.intent_ok,
        "llm_parse_ok": run.llm_parse_ok,
        "llm_raw_schema_ok": run.llm_raw_schema_ok,
        "llm_raw_schema_errors": run.llm_raw_schema_errors,
        "llm_schema_ok": run.llm_schema_ok,
        "llm_schema_errors": run.llm_schema_errors,
        "grounding_changes": run.grounding_changes,
        "llm_error": run.llm_error,
        "llm_raw": run.llm_raw,
        "llm_json": run.llm_json,
        "llm_grounded_json": run.llm_grounded_json,
        "status": analysis.status if analysis else "error",
        "answer_md": analysis.answer_md if analysis else run.llm_error,
        "fact_keys": list(analysis.fact_keys) if analysis else [],
        "trace": [asdict(step) for step in run.trace],
        "step_count": len(run.trace),
        "elapsed_ms": run.elapsed_ms,
    }


def _summary(runs: list[ApproachRun]) -> dict[str, Any]:
    return {"approaches": {approach: _summary_one([run for run in runs if run.approach == approach]) for approach in ("primitive", "query_spec")}}


def _summary_one(runs: list[ApproachRun]) -> dict[str, Any]:
    total = len(runs)
    return {
        "total": total,
        "parse_ok": sum(run.llm_parse_ok for run in runs),
        "schema_ok": sum(run.llm_schema_ok for run in runs),
        "raw_schema_ok": sum(run.llm_raw_schema_ok for run in runs),
        "intent_ok": sum(run.intent_ok for run in runs),
        "executable": sum(run.executable and run.llm_schema_ok for run in runs),
        "fact_ok": sum(run.llm_schema_ok and run.analysis is not None and run.analysis.status == "ok" for run in runs),
        "answered_ok": sum(run.analysis is not None and run.analysis.status == "ok" for run in runs),
        "unsupported": sum(run.analysis is not None and run.analysis.status == "unsupported" for run in runs),
        "llm_error_count": sum(bool(run.llm_error) for run in runs),
        "avg_steps": sum(len(run.trace) for run in runs) / total if total else 0.0,
        "avg_elapsed_ms": sum(run.elapsed_ms for run in runs) / total if total else 0.0,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare primitive chain vs query spec composition on mart snapshot.")
    parser.add_argument("--data-tsv", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--genos-base-url", default="https://jwai-dev.jwhealthcare.com/api/gateway/rep/serving/76")
    parser.add_argument("--use-llm", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    main()
