"""R12.7c retry — failure injection harness (F1..F5), bidirectional stdout.

Run: python tests/r127c_failure_injection.py
Each case prints BOTH directions (injection off / injection on) so a pass is
distinguishable from a test that never exercised the guard.
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import requests

from jw_chat_agent_poc.service.v4.contracts import (
    SOURCE_NAMES,
    PlannerOutput,
    RequestedAnswerShape,
    SourceResult,
    ToolQueries,
)
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
from jw_chat_agent_poc.service.v4.synthesis_policy import (
    SynthesisPolicy,
    bound_synthesis_messages,
    limit_evidence_sets_for_render,
    prune_unsupported_source_queries,
)
from jw_chat_agent_poc.service.v4.synthesizer import V4Synthesizer
from jw_chat_agent_poc.service.v4 import runtime as runtime_module

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    if not condition:
        FAILURES.append(label)
    print(f"  [{status}] {label}{(' :: ' + detail) if detail else ''}")


def head(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def _plan(**overrides) -> PlannerOutput:
    values = {name: (f"{name} query",) for name in SOURCE_NAMES}
    return PlannerOutput(
        resolved_question="리바로젯 제네릭 임상현황",
        expanded_intents=("임상",),
        answer_sources=overrides.get("answer_sources", ("clinicaltrials",)),
        tool_queries=ToolQueries(**values),
        linking_plan="clinical evidence",
        requested_answer_shape=RequestedAnswerShape(
            measure_or_attribute=overrides.get("measures", ("clinical_trials",))
        ),
    )


# ---------------------------------------------------------------- F1
head("F1  synthesis budget driven to zero -> facts-only, never a 500")
policy = SynthesisPolicy(
    total_request_budget_s=180.0,
    max_synthesis_budget_s=75.0,
    min_synthesis_budget_s=15.0,
    prompt_char_limit=120_000,
    source_render_limit=40,
)
normal = policy.allocate_synthesis_budget(remaining_s=90.0)
starved = policy.allocate_synthesis_budget(remaining_s=0.5)
print(f"  injection OFF: remaining=90.0s -> budget={normal}")
print(f"  injection ON : remaining=0.5s  -> budget={starved}")
check("F1 normal budget is allocated", normal == 75.0, f"got {normal}")
check("F1 starved budget yields skip (no attempt)", starved is None, f"got {starved}")

skipped = runtime_module._synthesis_failure_outcome(RuntimeError("x"))
check("F1 facts-only text is non-empty", bool(skipped.text.strip()))
check("F1 no internal type leaks to user surface", "RuntimeError" not in skipped.text)


# ---------------------------------------------------------------- F2
head("F2  stream cut mid-answer -> partial preserved, facts intact")


def _stream(cut: bool):
    class Response:
        def raise_for_status(self):
            return None

        def iter_lines(self, *, decode_unicode: bool):
            yield 'data: {"choices":[{"delta":{"content":"확인된 시험은 23건입니다. "}}]}'
            if cut:
                raise requests.ReadTimeout("injected stream cut")
            yield 'data: {"choices":[{"delta":{"content":"추가 해설입니다."}}]}'

        def close(self):
            return None

    return Response()


client = SimpleNamespace(
    base_url="https://example.invalid",
    token=None,
    model="test",
    timeout_s=30,
    total_budget_s=30,
    _extract_delta_from_data=lambda d: d["choices"][0]["delta"].get("content", ""),
)

_orig_post = requests.post
requests.post = lambda *a, **k: _stream(cut=False)
whole = _chat_completion_with_token_cap(client, [{"role": "user", "content": "q"}], max_tokens=32)
print(f"  injection OFF: text={whole.text!r}")

requests.post = lambda *a, **k: _stream(cut=True)
try:
    _chat_completion_with_token_cap(client, [{"role": "user", "content": "q"}], max_tokens=32)
    captured = None
except CompletionTransportError as exc:
    captured = exc
requests.post = _orig_post

print(f"  injection ON : kind={captured.kind if captured else None} "
      f"partial={captured.partial.text if captured else None!r}")
check("F2 uncut stream returns the full text", "추가 해설입니다." in whole.text)
check("F2 cut stream raises transport error", captured is not None)
check("F2 partial text is retained", bool(captured and captured.partial.text.strip()))
check(
    "F2 transport error remains catchable by pre-existing handlers",
    isinstance(captured, requests.RequestException),
    f"mro={[c.__name__ for c in type(captured).__mro__][:4]}" if captured else "",
)


# ---------------------------------------------------------------- F3
head("F3  prompt char limit driven to the floor -> zero record discard")
records = [{"id": f"R{i:03d}", "raw_text": "x" * 3_000} for i in range(80)]
payload = json.dumps({"external_evidence": [{"detail": {"records": records}}]}, ensure_ascii=False)
messages = [{"role": "system", "content": "system contract"}, {"role": "user", "content": payload}]
before_chars = sum(len(m["content"]) for m in messages)

loose, loose_trace = bound_synthesis_messages(messages, char_limit=10_000_000)
tight, tight_trace = bound_synthesis_messages(messages, char_limit=2_000)
print(f"  injection OFF: limit=10,000,000 -> applied={loose_trace['applied']} chars={loose_trace['after_chars']:,}")
print(f"  injection ON : limit=2,000       -> applied={tight_trace['applied']} "
      f"strategy={tight_trace['strategy']} chars={before_chars:,} -> {tight_trace['after_chars']:,}")
check("F3 loose limit does not compact", loose_trace["applied"] is False)
check("F3 tight limit compacts", tight_trace["applied"] is True)
check("F3 records_discarded == 0", tight_trace["records_discarded"] == 0)
check("F3 inspection retains full payload", tight_trace["inspection_retains_full_payload"] is True)
check(
    "F3 source messages are not mutated in place",
    json.loads(messages[1]["content"])["external_evidence"][0]["detail"]["records"] == records,
)

evidence = EvidenceSet(
    source="clinicaltrials",
    retrieved_at="2026-08-15T00:00:00Z",
    records=tuple(
        EvidenceRecord(
            evidence_id=f"clinicaltrials:{i}",
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
print(f"  injection OFF: per_source_limit=1000 -> applied={keep_trace['applied']} rendered={len(kept[0].records)}")
print(f"  injection ON : per_source_limit=5    -> applied={cut_trace['applied']} rendered={len(cut[0].records)} "
      f"received_still={cut[0].coverage.records_received}")
check("F3 render cap engages", cut_trace["applied"] is True and len(cut[0].records) == 5)
check("F3 render cap does not shrink the received ledger", cut[0].coverage.records_received == 80)
check("F3 render cap is disclosed", "surface_render_limit" in cut[0].coverage.partial_reasons)
check("F3 original evidence object untouched", len(evidence.records) == 80)


# ---------------------------------------------------------------- F4
head("F4  pruning disabled -> unrelated lanes come back (both directions)")
plan = _plan(answer_sources=("clinicaltrials",), measures=("clinical_trials",))
before_hira = len(plan.tool_queries.hira)

os.environ.pop("CHAT_V4_PRUNE_UNSUPPORTED_SOURCE_QUERIES", None)
pruned, prune_trace = prune_unsupported_source_queries(plan)
os.environ["CHAT_V4_PRUNE_UNSUPPORTED_SOURCE_QUERIES"] = "0"
unpruned, unprune_trace = prune_unsupported_source_queries(plan)
os.environ.pop("CHAT_V4_PRUNE_UNSUPPORTED_SOURCE_QUERIES", None)

print(f"  injection OFF (pruning ON) : hira queries {before_hira} -> {len(pruned.tool_queries.hira)} "
      f"applied={prune_trace['applied']} omitted_sources={sorted(prune_trace['omitted'])}")
print(f"  injection ON  (pruning OFF): hira queries {before_hira} -> {len(unpruned.tool_queries.hira)} "
      f"applied={unprune_trace['applied']} disabled={unprune_trace.get('disabled')}")
check("F4 pruning removes unsupported lanes", len(pruned.tool_queries.hira) == 0)
check("F4 pruning records what it omitted", bool(prune_trace["omitted"]))
check("F4 disabling pruning restores the lanes", len(unpruned.tool_queries.hira) == before_hira)
check("F4 omission is traceable on the plan", bool(pruned.query_scope and pruned.query_scope.omitted_queries))


# ---------------------------------------------------------------- F5
head("F5  exception injected into the synthesis family -> facts kept, no 500")


class RaisingClient:
    def complete_detailed(self, _messages, *, budget_s, max_tokens):
        raise RuntimeError("injected synthesis explosion")


class GoodClient:
    def complete_detailed(self, _messages, *, budget_s, max_tokens):
        return CompletionResult(text="정상 해설입니다.", finish_reason="stop", usage={}, elapsed_ms=5.0)


ct_result = SourceResult(
    source="clinicaltrials",
    query="ezetimibe AND pitavastatin",
    status="ok",
    payload={"studies": [{"protocolSection": {"identificationModule": {"nctId": "NCT00000001"}}}]},
)

ok = V4Synthesizer(GoodClient()).synthesize_with_trace(_plan(), (ct_result,), (), budget_s=30.0)
print(f"  injection OFF: status={ok.trace['status']} text_head={ok.text[:40]!r}")

raised = None
try:
    bad = V4Synthesizer(RaisingClient()).synthesize_with_trace(_plan(), (ct_result,), (), budget_s=30.0)
except Exception as exc:  # noqa: BLE001 - the point of the probe
    bad, raised = None, exc
_bad_status = bad.trace["status"] if bad else None
_bad_head = repr(bad.text[:40]) if bad else None
print(f"  injection ON : raised={type(raised).__name__ if raised else None} "
      f"status={_bad_status} text_head={_bad_head}")
check("F5 synthesizer absorbs the exception", raised is None)
check("F5 grounded surface still returned", bool(bad and bad.text.strip()))
check("F5 no internal type on the user surface", bool(bad and "RuntimeError" not in bad.text))
check("F5 failure is recorded in the trace", bool(bad and bad.trace.get("error_type")))

guarded = runtime_module._synthesis_failure_outcome(ValueError("injected runtime-stage explosion"))
print(f"  runtime stage guard: status={guarded.trace['status']} reason={guarded.trace['fallback_reason']} "
      f"error_type={guarded.trace['error_type']}")
check("F5 runtime stage guard records the reason", guarded.trace["fallback_reason"] == "synthesis_step_failed")
check("F5 runtime stage guard keeps a user surface", bool(guarded.text.strip()))
check("F5 runtime stage guard hides internals", "ValueError" not in guarded.text)


head("SUMMARY")
if FAILURES:
    print(f"FAILED CHECKS ({len(FAILURES)}):")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print("ALL FAILURE-INJECTION CHECKS PASSED (F1..F5, both directions)")
