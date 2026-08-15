"""R12.7d failure injection (F1..F5), bidirectional stdout.

Run: python tests/r127d_failure_injection.py

Every case prints BOTH directions so a pass cannot be confused with a guard that
never ran. F1/F2 additionally assert the deterministic market surface survives —
that is the regression which forced the R12.7c rollback.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone

import requests

from jw_chat_agent_poc.service.v4 import runtime as runtime_module
from jw_chat_agent_poc.service.v4.contracts import (
    SOURCE_NAMES,
    Citation,
    EvidenceEnvelope,
    PlannerOutput,
    RequestedAnswerShape,
    SourceResult,
    ToolQueries,
)
from jw_chat_agent_poc.service.v4.inspection import _record_accounting
from jw_chat_agent_poc.service.v4.llm import (
    CompletionResult,
    CompletionTransportError,
    _chat_completion_with_token_cap,
)
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    CoverageLedger,
    EvidenceRecord,
    EvidenceSet,
)
from jw_chat_agent_poc.service.v4.planner import V4Planner
from jw_chat_agent_poc.service.v4.synthesis_policy import (
    SynthesisPolicy,
    bound_synthesis_messages,
    limit_evidence_sets_for_render,
    prune_unsupported_source_queries,
)
from jw_chat_agent_poc.service.v4.synthesizer import V4Synthesizer

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{(' :: ' + detail) if detail else ''}")
    if not condition:
        FAILURES.append(label)


def head(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


HISTORY_VALUES = ("69.24", "75.34", "77.73", "78.05", "87.38", "90.86")


def _plan() -> PlannerOutput:
    values = {name: (f"{name} query",) for name in SOURCE_NAMES}
    return PlannerOutput(
        resolved_question="리바로 매출 알려줘",
        expanded_intents=("매출",),
        answer_sources=("mart",),
        tool_queries=ToolQueries(**values),
        linking_plan="mart sales",
        requested_answer_shape=RequestedAnswerShape(measure_or_attribute=("sales",)),
    )


def _mart_result() -> SourceResult:
    series = [
        {"period": "2021-07", "value_억원": 69.24},
        {"period": "2021-12", "value_억원": 75.34},
        {"period": "2022-12", "value_억원": 77.73},
        {"period": "2023-12", "value_억원": 78.05},
        {"period": "2024-12", "value_억원": 87.38},
        {"period": "2025-12", "value_억원": 90.86},
        {"period": "2026-06", "value_억원": 85.87},
    ]
    return SourceResult(
        source="mart",
        query="리바로 매출 알려줘",
        status="ok",
        payload={
            "calls": [
                {
                    "render_data": {"brand": "리바로", "brand_value_series_10pt": series},
                    "entity_bundle": {
                        "anchor": "리바로",
                        "period_start": "2025-09",
                        "period_end": "2026-06",
                        "same_period_and_denominator": True,
                        "members": [
                            {
                                "brand": "리바로",
                                "company": "JW중외제약",
                                "rank": 6,
                                "role": "target",
                                "render_data": {
                                    "brand_value_series_10pt": [
                                        {"period": "2025-09", "value_억원": 89.29},
                                        {"period": "2026-06", "value_억원": 85.87},
                                    ]
                                },
                            },
                            {
                                "brand": "로수젯",
                                "company": "한미약품",
                                "rank": 1,
                                "role": "competitor",
                                "render_data": {
                                    "brand_value_series_10pt": [
                                        {"period": "2025-09", "value_억원": 208.26},
                                        {"period": "2026-06", "value_억원": 214.32},
                                    ]
                                },
                            },
                        ],
                    },
                }
            ]
        },
        evidence=EvidenceEnvelope(
            kind="mart",
            entity_match="EXACT",
            source_scope="KR",
            time_match="MATCH",
            subject_grain="brand",
            eligible_attributions=("observed_association",),
        ),
        citations=(
            Citation(
                source="UBIST",
                query="리바로 매출 알려줘",
                retrieved_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
                used=True,
            ),
        ),
    )


def _table_rows(text: str) -> int:
    return sum(
        1
        for line in text.splitlines()
        if line.startswith("|") and not set(line) <= set("| -:")
    )


class _GoodClient:
    def complete_detailed(self, _messages, *, budget_s, max_tokens):
        return CompletionResult(
            text="리바로 매출은 감소했습니다 [출처: 내부 데이터마트].",
            finish_reason="stop",
            usage={},
            elapsed_ms=50.0,
        )


# ---------------------------------------------------------------- F1
head("F1  synthesis budget driven to zero -> facts + deterministic table, never 500")
policy = SynthesisPolicy(
    total_request_budget_s=180.0,
    max_synthesis_budget_s=75.0,
    min_synthesis_budget_s=15.0,
    prompt_char_limit=120_000,
    source_render_limit=40,
)
print(f"  injection OFF: remaining=90.0s -> budget={policy.allocate_synthesis_budget(remaining_s=90.0)}")
print(f"  injection ON : remaining=0.5s  -> budget={policy.allocate_synthesis_budget(remaining_s=0.5)}")
check("F1 normal budget allocated", policy.allocate_synthesis_budget(remaining_s=90.0) == 75.0)
check("F1 starved budget skips synthesis", policy.allocate_synthesis_budget(remaining_s=0.5) is None)


class _BudgetSkippedClient:
    """Stands in for the runtime's budget_skipped branch: no completion at all."""

    def complete_detailed(self, _messages, *, budget_s, max_tokens):
        raise CompletionTransportError(
            "budget_timeout",
            partial=CompletionResult(text="", finish_reason=None, usage={}, elapsed_ms=0.0),
        )


starved = V4Synthesizer(_BudgetSkippedClient()).synthesize_with_trace(
    _plan(), (_mart_result(),), (), budget_s=30.0
)
print(f"  starved answer: table_rows={_table_rows(starved.text)} status={starved.trace['status']}")
check("F1 deterministic table present under starvation", "## 동일 기간 브랜드 비교" in starved.text)
check("F1 table rows >= 3", _table_rows(starved.text) >= 3, f"rows={_table_rows(starved.text)}")
check(
    "F1 5-year history present under starvation",
    all(v in starved.text for v in HISTORY_VALUES),
    f"missing={[v for v in HISTORY_VALUES if v not in starved.text]}",
)
check("F1 no internal type leaked", "CompletionTransportError" not in starved.text)


# ---------------------------------------------------------------- F2
head("F2  exception injected into the synthesis path -> deterministic table survives")


class _RaisingClient:
    def complete_detailed(self, _messages, *, budget_s, max_tokens):
        raise RuntimeError("injected synthesis explosion")


ok = V4Synthesizer(_GoodClient()).synthesize_with_trace(_plan(), (_mart_result(),), (), budget_s=30.0)
print(f"  injection OFF: status={ok.trace['status']} table_rows={_table_rows(ok.text)}")
raised = None
try:
    bad = V4Synthesizer(_RaisingClient()).synthesize_with_trace(
        _plan(), (_mart_result(),), (), budget_s=30.0
    )
except Exception as exc:  # noqa: BLE001 - the point of the probe
    bad, raised = None, exc
print(
    f"  injection ON : raised={type(raised).__name__ if raised else None} "
    f"status={bad.trace['status'] if bad else None} table_rows={_table_rows(bad.text) if bad else 0}"
)
check("F2 synthesizer absorbs the exception", raised is None)
check("F2 deterministic table present on success", "## 동일 기간 브랜드 비교" in ok.text)
check("F2 deterministic table present on failure", bool(bad and "## 동일 기간 브랜드 비교" in bad.text))
_ok_decimals = set(re.findall(r"\d+\.\d+", ok.text))
_bad_decimals = set(re.findall(r"\d+\.\d+", bad.text)) if bad else set()
_failure_only = sorted(_bad_decimals - _ok_decimals)
check(
    "F2 same decimal values both ways",
    bool(bad) and not _failure_only,
    f"failure-only={_failure_only}",
)
check("F2 no internal type on the surface", bool(bad and "RuntimeError" not in bad.text))
check("F2 failure recorded in trace", bool(bad and bad.trace.get("error_type")))


# ---------------------------------------------------------------- F3
head("F3  prompt char limit at the floor -> zero record discard")
records = [{"id": f"R{i:03d}", "raw_text": "x" * 3_000} for i in range(80)]
payload = json.dumps({"external_evidence": [{"detail": {"records": records}}]}, ensure_ascii=False)
messages = [{"role": "system", "content": "system contract"}, {"role": "user", "content": payload}]
loose, loose_trace = bound_synthesis_messages(messages, char_limit=10_000_000)
tight, tight_trace = bound_synthesis_messages(messages, char_limit=2_000)
print(f"  injection OFF: applied={loose_trace['applied']} chars={loose_trace['after_chars']:,}")
print(
    f"  injection ON : applied={tight_trace['applied']} strategy={tight_trace['strategy']} "
    f"{tight_trace['before_chars']:,} -> {tight_trace['after_chars']:,}"
)
check("F3 loose limit does not compact", loose_trace["applied"] is False)
check("F3 tight limit compacts", tight_trace["applied"] is True)
check("F3 records_discarded == 0", tight_trace["records_discarded"] == 0)
check("F3 inspection retains full payload", tight_trace["inspection_retains_full_payload"] is True)
check(
    "F3 source messages not mutated",
    json.loads(messages[1]["content"])["external_evidence"][0]["detail"]["records"] == records,
)

evidence = EvidenceSet(
    source="clinicaltrials",
    retrieved_at="2026-08-15T00:00:00Z",
    records=tuple(
        EvidenceRecord(
            evidence_id=f"clinicaltrials:1:{i}",
            source="clinicaltrials",
            result_kind="study",
            payload={"nct_id": f"NCT{i:08d}"},
        )
        for i in range(80)
    ),
    coverage=CoverageLedger(records_received=80, records_rendered=0, total_reported=80),
)
kept, keep_trace = limit_evidence_sets_for_render((evidence,), per_source_limit=1_000)
cut, cut_trace = limit_evidence_sets_for_render((evidence,), per_source_limit=5)
print(f"  injection OFF: per_source_limit=1000 applied={keep_trace['applied']} rendered={len(kept[0].records)}")
print(
    f"  injection ON : per_source_limit=5    applied={cut_trace['applied']} rendered={len(cut[0].records)} "
    f"received_still={cut[0].coverage.records_received}"
)
check("F3 render cap engages", cut_trace["applied"] and len(cut[0].records) == 5)
check("F3 received ledger not shrunk", cut[0].coverage.records_received == 80)
check("F3 cap disclosed", "surface_render_limit" in cut[0].coverage.partial_reasons)

acc_all = _record_accounting(evidence.records, set())
acc_some = _record_accounting(evidence.records, {f"clinicaltrials:1:{i}" for i in range(5)})
print(f"  invariant-2 ledger  none rendered: {acc_all['received']}={acc_all['rendered']}+{acc_all['omitted']}")
print(f"  invariant-2 ledger  5 rendered   : {acc_some['received']}={acc_some['rendered']}+{acc_some['omitted']}")
check("F3 ledger closes (none rendered)", acc_all["received"] == acc_all["rendered"] + acc_all["omitted"])
check("F3 ledger closes (some rendered)", acc_some["received"] == acc_some["rendered"] + acc_some["omitted"])
check(
    "F3 omitted identifiers accounted",
    len(acc_some["omitted_identifiers"]) + acc_some["omitted_without_identifier"] == acc_some["omitted"]
    or acc_some["omitted_identifiers_truncated"],
)


# ---------------------------------------------------------------- F4
head("F4  pruning disabled -> unrelated lanes come back (both directions)")
plan = _plan()
plan = plan.model_copy(update={"answer_sources": ("mart",)})
before_hira = len(plan.tool_queries.hira)
os.environ.pop("CHAT_V4_PRUNE_UNSUPPORTED_SOURCE_QUERIES", None)
pruned, prune_trace = prune_unsupported_source_queries(plan)
os.environ["CHAT_V4_PRUNE_UNSUPPORTED_SOURCE_QUERIES"] = "0"
unpruned, unprune_trace = prune_unsupported_source_queries(plan)
os.environ.pop("CHAT_V4_PRUNE_UNSUPPORTED_SOURCE_QUERIES", None)
print(f"  injection OFF (pruning ON) : hira {before_hira} -> {len(pruned.tool_queries.hira)} applied={prune_trace['applied']}")
print(f"  injection ON  (pruning OFF): hira {before_hira} -> {len(unpruned.tool_queries.hira)} disabled={unprune_trace.get('disabled')}")
check("F4 pruning removes unsupported lanes", len(pruned.tool_queries.hira) == 0)
check("F4 omissions recorded", bool(prune_trace["omitted"]))
check("F4 disabling restores lanes", len(unpruned.tool_queries.hira) == before_hira)


# ---------------------------------------------------------------- F5
head("F5  planner transport failure -> _fallback_plan, never a 500 (R12.7c regression)")


class _PlannerBoom:
    serving_id = "planner"

    def complete_detailed(self, _messages, *, budget_s, max_tokens):
        raise CompletionTransportError(
            "budget_timeout",
            partial=CompletionResult(text="", finish_reason=None, usage={}, elapsed_ms=1.0),
        )


class _PlannerOk:
    serving_id = "planner"

    def complete_detailed(self, _messages, *, budget_s, max_tokens):
        return CompletionResult(text="not json", finish_reason="stop", usage={}, elapsed_ms=1.0)


good = V4Planner(_PlannerOk()).plan_with_trace("리바로 매출 알려줘", (), budget_s=5.0)
print(f"  injection OFF (bad json): status={good.trace['status']} error_type={good.trace.get('error_type')}")
escaped = None
try:
    degraded = V4Planner(_PlannerBoom()).plan_with_trace("리바로 매출 알려줘", (), budget_s=5.0)
except Exception as exc:  # noqa: BLE001
    degraded, escaped = None, exc
print(
    f"  injection ON  (transport): raised={type(escaped).__name__ if escaped else None} "
    f"status={degraded.trace['status'] if degraded else None} "
    f"error_type={degraded.trace.get('error_type') if degraded else None}"
)
check("F5 transport error does not escape the planner", escaped is None)
check("F5 planner degrades to a fallback plan", bool(degraded and degraded.trace["status"] == "fallback"))
check("F5 failure recorded, not swallowed", bool(degraded and degraded.trace.get("error_type") == "CompletionTransportError"))
check(
    "F5 type contract intact",
    issubclass(CompletionTransportError, requests.RequestException),
    f"mro={[c.__name__ for c in CompletionTransportError.__mro__][:4]}",
)

guarded = runtime_module._synthesis_failure_outcome(ValueError("injected runtime-stage explosion"))
print(f"  runtime stage guard: reason={guarded.trace['fallback_reason']} error_type={guarded.trace['error_type']}")
check("F5 runtime guard records reason", guarded.trace["fallback_reason"] == "synthesis_step_failed")
check("F5 runtime guard hides internals", "ValueError" not in guarded.text)


head("SUMMARY")
if FAILURES:
    print(f"FAILED CHECKS ({len(FAILURES)}):")
    for item in FAILURES:
        print("  -", item)
    sys.exit(1)
print("ALL FAILURE-INJECTION CHECKS PASSED (F1..F5, both directions)")
