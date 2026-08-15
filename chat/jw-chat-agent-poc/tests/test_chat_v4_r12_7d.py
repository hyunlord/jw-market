"""R12.7d — the deterministic market surface must not depend on the LLM.

R12.7c orphaned ``_evidence_fallback``: its only production caller was the
"synthesis produced nothing" branch. Once the prompt cap made synthesis actually
succeed, that branch stopped running and the brand-comparison table plus the
multi-year history series disappeared from market answers.

The constitution says code owns the whole fact surface and the LLM only adds
commentary. A deterministic table that appears only when the LLM fails is the
inverse of that. These tests pin the corrected behaviour.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from jw_chat_agent_poc.service.v4.contracts import (
    SOURCE_NAMES,
    Citation,
    EvidenceEnvelope,
    PlannerOutput,
    RequestedAnswerShape,
    SourceResult,
    ToolQueries,
)
from jw_chat_agent_poc.service.v4.llm import CompletionResult
from jw_chat_agent_poc.service.v4.synthesizer import (
    V4Synthesizer,
    _deterministic_market_blocks,
    _evidence_fallback,
)


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


def _mart_payload() -> dict:
    """Mirrors the live payload shape: entity bundle + 10pt series per member."""
    series = [
        {"period": "2021-07", "value_억원": 69.24},
        {"period": "2021-12", "value_억원": 75.34},
        {"period": "2022-12", "value_억원": 77.73},
        {"period": "2023-12", "value_억원": 78.05},
        {"period": "2024-12", "value_억원": 87.38},
        {"period": "2025-12", "value_억원": 90.86},
        {"period": "2026-06", "value_억원": 85.87},
    ]
    return {
        "calls": [
            {
                # call-level render_data drives the multi-year history block
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
                                    {"period": "2021-07", "value_억원": 69.24},
                                    {"period": "2021-12", "value_억원": 75.34},
                                    {"period": "2022-12", "value_억원": 77.73},
                                    {"period": "2023-12", "value_억원": 78.05},
                                    {"period": "2024-12", "value_억원": 87.38},
                                    {"period": "2025-09", "value_억원": 89.29},
                                    {"period": "2025-12", "value_억원": 90.86},
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
                }
            }
        ]
    }


def _mart_result() -> SourceResult:
    return SourceResult(
        source="mart",
        query="리바로 매출 알려줘",
        status="ok",
        payload=_mart_payload(),
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


class _GoodClient:
    """Synthesis succeeds and returns commentary with no table of its own."""

    def complete_detailed(self, _messages, *, budget_s, max_tokens):
        return CompletionResult(
            text=(
                "**핵심 답**\n"
                "리바로의 2026년 6월 매출은 약 86억원입니다 [출처: 내부 데이터마트].\n\n"
                "**근거와 맥락**\n"
                "경쟁 제품인 로수젯이 시장 1위를 유지하고 있습니다 [출처: 내부 데이터마트]."
            ),
            finish_reason="stop",
            usage={},
            elapsed_ms=100.0,
        )


class _FailingClient:
    def complete_detailed(self, _messages, *, budget_s, max_tokens):
        raise RuntimeError("injected synthesis failure")


def _table_rows(text: str) -> list[str]:
    return [
        line
        for line in text.splitlines()
        if line.startswith("|") and not set(line) <= set("| -:")
    ]


HISTORY_VALUES = ("69.24", "75.34", "77.73", "78.05", "87.38", "90.86")


def test_deterministic_blocks_are_shared_by_both_paths() -> None:
    """One generator, so the success path and the fallback path cannot drift."""
    blocks = _deterministic_market_blocks((_mart_result(),), question="리바로 매출 알려줘")
    assert blocks, "expected deterministic mart blocks"
    joined = "\n\n".join(blocks)
    assert "## 동일 기간 브랜드 비교" in joined
    for value in HISTORY_VALUES:
        assert value in joined, f"history value {value} missing from deterministic blocks"

    # _evidence_fallback must be built from the very same blocks.
    fallback = _evidence_fallback((_mart_result(),), question="리바로 매출 알려줘")
    for block in blocks:
        assert block in fallback


def test_market_table_and_history_survive_successful_synthesis() -> None:
    """The regression that caused the R12.7c rollback."""
    outcome = V4Synthesizer(_GoodClient()).synthesize_with_trace(
        _plan(), (_mart_result(),), (), budget_s=30.0
    )

    assert outcome.trace["status"] == "synthesized"
    assert "## 동일 기간 브랜드 비교" in outcome.text
    rows = _table_rows(outcome.text)
    assert len(rows) >= 3, f"expected header + 2 brand rows, got {len(rows)}"
    assert "리바로" in outcome.text and "로수젯" in outcome.text
    for value in HISTORY_VALUES:
        assert value in outcome.text, f"5-year history value {value} lost on success path"

    surface = outcome.trace["deterministic_market_surface"]
    assert surface["blocks_available"] >= 1
    assert surface["blocks_injected"] >= 1
    assert surface["table_rows"] >= 3


def test_market_table_and_history_survive_failed_synthesis() -> None:
    """Invariant 3: commentary may be lost, the deterministic surface may not."""
    outcome = V4Synthesizer(_FailingClient()).synthesize_with_trace(
        _plan(), (_mart_result(),), (), budget_s=30.0
    )

    assert outcome.trace["status"] == "fallback"
    assert "## 동일 기간 브랜드 비교" in outcome.text
    assert len(_table_rows(outcome.text)) >= 3
    for value in HISTORY_VALUES:
        assert value in outcome.text
    assert "RuntimeError" not in outcome.text


def test_success_and_failure_expose_the_same_deterministic_values() -> None:
    """The fact surface must not vary with the LLM's outcome."""
    import re

    ok = V4Synthesizer(_GoodClient()).synthesize_with_trace(
        _plan(), (_mart_result(),), (), budget_s=30.0
    )
    bad = V4Synthesizer(_FailingClient()).synthesize_with_trace(
        _plan(), (_mart_result(),), (), budget_s=30.0
    )
    decimals = lambda text: set(re.findall(r"\d+\.\d+", text))  # noqa: E731
    missing = decimals(bad.text) - decimals(ok.text)
    assert not missing, f"values present only when synthesis fails: {sorted(missing)}"


def test_deterministic_blocks_are_not_duplicated_when_already_present() -> None:
    """Idempotent: the fallback branch already emitted them, so do not repeat."""
    outcome = V4Synthesizer(_FailingClient()).synthesize_with_trace(
        _plan(), (_mart_result(),), (), budget_s=30.0
    )
    assert outcome.text.count("## 동일 기간 브랜드 비교") == 1


def test_inspection_proves_invariant_2_per_lane(monkeypatch) -> None:
    """Invariant 2 must be checkable, not just asserted.

    R13-B' stopped shipping record payloads to the browser, which also removed
    any way to confirm that a record dropped from the surface still exists in the
    inspection panel. Per-lane accounting restores the proof without shipping
    payloads back.
    """
    from jw_chat_agent_poc.service.v4.inspection import _record_accounting
    from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceRecord

    records = tuple(
        EvidenceRecord(
            evidence_id=f"clinicaltrials:1:{i}",
            source="clinicaltrials",
            result_kind="study",
            payload={"nct_id": f"NCT{i:08d}"} if i < 8 else {"unnamed": i},
        )
        for i in range(10)
    )
    rendered_ids = {"clinicaltrials:1:0", "clinicaltrials:1:1"}

    accounting = _record_accounting(records, rendered_ids)

    assert accounting["received"] == 10
    assert accounting["rendered"] == 2
    assert accounting["omitted"] == 8
    # the ledger must close: nothing may vanish between the three buckets
    assert accounting["received"] == accounting["rendered"] + accounting["omitted"]
    assert (
        len(accounting["omitted_identifiers"]) + accounting["omitted_without_identifier"]
        == accounting["omitted"]
    )
    assert "NCT00000002" in accounting["omitted_identifiers"]
    # records with no identifier are counted, never invented
    assert accounting["omitted_without_identifier"] == 2
    assert accounting["omitted_identifiers_truncated"] is False


def test_inspection_identifier_bound_is_configurable(monkeypatch) -> None:
    """A cap that cannot be injected is a hardcoded limit."""
    from jw_chat_agent_poc.service.v4.inspection import _record_accounting
    from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceRecord

    records = tuple(
        EvidenceRecord(
            evidence_id=f"web:1:{i}",
            source="web",
            result_kind="page",
            payload={"title": f"doc {i}"},
        )
        for i in range(50)
    )
    monkeypatch.setenv("CHAT_V4_INSPECTION_OMITTED_IDENTIFIER_LIMIT", "5")

    accounting = _record_accounting(records, set())

    assert accounting["omitted"] == 50
    assert len(accounting["omitted_identifiers"]) == 5
    assert accounting["omitted_identifiers_truncated"] is True
    assert accounting["identifier_limit"] == 5
    # the count stays truthful even when the list is bounded
    assert accounting["received"] == accounting["rendered"] + accounting["omitted"]


def test_market_gate_recipe_is_pinned_in_the_repository() -> None:
    """The previous expected SHA died with an audit archive. Pin it here instead."""
    from scripts.market_regression_gate import (
        CANONICAL_QUESTION,
        EMPTY_SHA256,
        digest,
        mart_records,
        normalize,
    )

    assert CANONICAL_QUESTION == "리바로 매출 알려줘"

    sample = (
        "리바로 매출은 2025-09 89.29억원에서 2026-06 85.87억원까지 변했고, "
        "표시값 기준 증감은 -3.42억원입니다."
    )
    assert normalize(sample) == "2025|09|89.29|2026|06|85.87|3.42"
    assert digest(sample) == hashlib.sha256(
        "2025|09|89.29|2026|06|85.87|3.42".encode()
    ).hexdigest()

    # wording may drift without moving the digest; a value may not
    reworded = (
        "2025-09 89.29억원 → 2026-06 85.87억원, 증감 -3.42억원 [출처: 내부 데이터마트]"
    )
    assert digest(reworded) == digest(sample)
    assert digest(sample.replace("85.87", "85.88")) != digest(sample)

    # the empty-evidence flake is recognisable, not silently averaged in
    assert digest("근거가 없어 답을 구성하지 못했습니다") == EMPTY_SHA256

    trace = {
        "lossless_spine": {
            "evidence_sets": [
                {"source": "mart", "coverage": {"records_received": 32}},
                {"source": "hira", "coverage": {"records_received": 0}},
            ]
        }
    }
    assert mart_records(trace) == 32
    assert mart_records({}) == 0


def test_non_market_answers_are_untouched() -> None:
    """No mart result means no injection and no empty heading."""
    ct = SourceResult(
        source="clinicaltrials",
        query="ezetimibe",
        status="ok",
        payload={"studies": [{"protocolSection": {"identificationModule": {"nctId": "NCT1"}}}]},
    )
    outcome = V4Synthesizer(_GoodClient()).synthesize_with_trace(
        _plan(), (ct,), (), budget_s=30.0
    )
    assert "## 동일 기간 브랜드 비교" not in outcome.text
    assert outcome.trace["deterministic_market_surface"]["blocks_available"] == 0
