from __future__ import annotations

import hashlib
import os
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from jw_chat_agent_poc.service.sse_presenter import align_text_to_table_authority
from jw_chat_agent_poc.service.v4.fact_digest import FactDigest, render_core_answer
from jw_chat_agent_poc.service.v4.insight_claim_verifier import (
    normalize_final_surface_text,
)
from jw_chat_agent_poc.service.v4.insight_claims import (
    ClaimPayloadError,
    ensure_required_item_coverage,
    parse_claim_payload,
)
from jw_chat_agent_poc.service.v4.insight_ladder import (
    build_grounded_facts_extension,
    build_l1_insight,
    prepare_insight_material,
)
from jw_chat_agent_poc.service.v4.synthesizer import (
    V4Synthesizer,
    _structured_material_floor,
)

_DEFAULT_L2_TIMEOUT_S = 120.0
_DEFAULT_TOTAL_CAP_S = 480.0
_DEFAULT_COMPRESSION_CHAR_THRESHOLD = 80_000
_L3_CARD_LIMIT = 12
_L3_COMPRESSED_METRIC_LIMIT = 16
_L2_CARD_LIMIT = 6
_L2_METRIC_LIMIT = 8
_ZERO_ROW_FILE_ANSWER_RE = re.compile(
    r"업로드 파일에서[^.!?\n]*(?:찾지 못|데이터가 없습니다)[^.!?\n]*[.!?]?"
)
_ABSENCE_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]?")
_CONCRETE_VALUE_RE = re.compile(
    r"(?<![A-Za-z가-힣])[-+]?\d[\d,.]*(?:%|원|건|명|개|년|월|일|위|억|만)?"
)


@dataclass(frozen=True)
class InsightLaneOutcome:
    text: str
    insight_text: str
    facts_text: str
    trace: dict[str, Any]


SectionReadyCallback = Callable[[dict[str, Any]], None]


class InsightLane:
    """Generate insight through bounded L3, L2, and deterministic L1 levels."""

    def __init__(
        self,
        synthesizer: V4Synthesizer,
        *,
        timeout_s: float,
        l2_timeout_s: float | None = None,
        total_cap_s: float | None = None,
        compression_char_threshold: int | None = None,
    ) -> None:
        self._synthesizer = synthesizer
        self._timeout_s = timeout_s
        self._l2_timeout_s = (
            l2_timeout_s
            if l2_timeout_s is not None
            else _float_env("CHAT_V4_INSIGHT_L2_TIMEOUT_S", _DEFAULT_L2_TIMEOUT_S)
        )
        self._total_cap_s = (
            total_cap_s
            if total_cap_s is not None
            else _float_env("CHAT_V4_INSIGHT_TOTAL_CAP_S", _DEFAULT_TOTAL_CAP_S)
        )
        self._compression_char_threshold = (
            compression_char_threshold
            if compression_char_threshold is not None
            else _int_env(
                "CHAT_V4_INSIGHT_COMPRESSION_CHAR_THRESHOLD",
                _DEFAULT_COMPRESSION_CHAR_THRESHOLD,
            )
        )

    def generate(
        self,
        plan: Any,
        core_answer: str,
        *,
        fact_digest: FactDigest,
        remaining_s: float,
        section_ready_callback: SectionReadyCallback | None = None,
        source_context: Mapping[str, Any] | None = None,
    ) -> InsightLaneOutcome:
        started = time.monotonic()
        cap_s = max(0.0, min(self._total_cap_s, remaining_s))
        trace: dict[str, Any] = {
            "status": "started",
            "timeout_s": self._timeout_s,
            "l2_timeout_s": self._l2_timeout_s,
            "total_cap_s": self._total_cap_s,
            "budget_s": cap_s,
            "started_monotonic": started,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "ladder_attempts": [],
        }
        trace["brand_candidate_filter"] = _brand_candidate_filter_trace(fact_digest)
        facts_fallback = _section_body(core_answer)
        insight_core, insight_digest, zero_row_trace = _without_zero_row_file_material(
            core_answer,
            fact_digest,
        )
        trace["zero_row_file_material"] = zero_row_trace
        if zero_row_trace.get("removed_card_count"):
            facts_fallback = _zero_row_file_facts(fact_digest) or facts_fallback
        insight_digest, relevance_trace = _filter_relevant_material(
            fact_digest,
            insight_digest,
            zero_row_trace=zero_row_trace,
        )
        trace["relevance_filter"] = relevance_trace
        facts_fallback, fallback_relevance_trace = _relevance_safe_facts_fallback(
            facts_fallback,
            insight_digest,
            relevance_trace,
            preserve_existing=bool(zero_row_trace.get("removed_card_count")),
        )
        trace["facts_fallback_relevance"] = fallback_relevance_trace
        availability = _source_availability(
            insight_digest,
            source_context=source_context,
        )
        trace["source_availability"] = availability
        if availability["state"] in {"web_only", "none"}:
            return self._complete_source_absence(
                insight_digest,
                facts_fallback=facts_fallback,
                availability=availability,
                trace=trace,
                started=started,
                section_ready_callback=section_ready_callback,
            )
        injection = os.environ.get("V4_INSIGHT_LANE_FAILURE_INJECTION", "").strip()
        if cap_s <= 0 or injection == "cap_exceeded":
            return self._complete_l1(
                insight_digest,
                facts_text=facts_fallback,
                trace=trace,
                started=started,
                reason="total_cap_exhausted",
            )

        structured_generator = getattr(
            self._synthesizer, "generate_structured_insight_claims", None
        )
        section_generator = getattr(
            self._synthesizer, "generate_structured_section_claims", None
        )
        split_is_explicit = (
            type(self._synthesizer) is V4Synthesizer
            or "generate_structured_section_claims" in type(self._synthesizer).__dict__
        )
        if callable(section_generator) and split_is_explicit:
            return self._generate_split_ladders(
                section_generator,
                plan,
                insight_core,
                fact_digest=insight_digest,
                facts_fallback=facts_fallback,
                cap_s=cap_s,
                trace=trace,
                started=started,
                injection=injection,
                section_ready_callback=section_ready_callback,
            )
        if callable(structured_generator):
            return self._generate_ladder(
                structured_generator,
                plan,
                insight_core,
                fact_digest=insight_digest,
                facts_fallback=facts_fallback,
                cap_s=cap_s,
                trace=trace,
                started=started,
                injection=injection,
            )
        return self._generate_legacy_or_l1(
            plan,
            insight_core,
            fact_digest=insight_digest,
            facts_fallback=facts_fallback,
            cap_s=cap_s,
            trace=trace,
            started=started,
            injection=injection,
        )

    def _complete_source_absence(
        self,
        fact_digest: FactDigest,
        *,
        facts_fallback: str,
        availability: dict[str, Any],
        trace: dict[str, Any],
        started: float,
        section_ready_callback: SectionReadyCallback | None,
    ) -> InsightLaneOutcome:
        state = str(availability["state"])
        if state == "web_only":
            notice = (
                "내부 데이터·공식 원천에서는 확인되지 않아 공개 웹 자료를 "
                "기준으로 정리했습니다."
            )
            grounded = _strip_required_source_gap(facts_fallback)
            if not grounded:
                count = sum(
                    int(card.received_count or card.matched_count or 0)
                    for card in fact_digest.cards
                    if _source_family(card.source) == "web"
                )
                grounded = f"질문과 관련된 공개 웹 자료 {count}건을 확인했습니다."
            facts_text = f"{notice} {grounded}"
            insight_text = (
                "공개 웹 자료에서 직접 확인되는 범위만 정리했으며, "
                "추가 추정은 포함하지 않았습니다."
            )
            evidence_ids = [
                evidence_id
                for card in fact_digest.cards
                if _source_family(card.source) == "web"
                for evidence_id in card.evidence_ids
            ]
        else:
            facts_text = _no_source_facts(fact_digest, availability)
            insight_text = "관련 근거가 없어 추가 분석을 제시하지 않습니다."
            evidence_ids = []

        absence_gate = _absence_insight_gate(insight_text, state=state)
        if absence_gate["blocked"]:
            insight_text = ""
        trace["absence_insight_gate"] = absence_gate

        facts_paragraph = _deterministic_paragraph(facts_text, evidence_ids)
        insight_paragraphs = (
            [_deterministic_paragraph(insight_text, ())] if insight_text else []
        )
        manifest = {
            "parse_status": "deterministic_source_availability",
            "claim_count": 0,
            "type_counts": {},
            "section_counts": {"facts": 0, "insight": 0},
            "section_paragraphs": {
                "facts": [facts_paragraph],
                "insight": insight_paragraphs,
            },
            "hypothesis_policy": {
                "state": state,
                "cap": int(availability["hypothesis_cap"]),
                "emitted": 0,
                "dropped_unbound": 0,
                "dropped_over_cap": 0,
            },
        }
        trace.update(
            {
                "status": "completed",
                "fallback_required": False,
                "structured_output": False,
                "ladder_level": "DETERMINISTIC_SOURCE_AVAILABILITY",
                "ladder_complete": True,
                "claim_manifest": manifest,
                "empty_section_count": int(not insight_text),
                "hypothesis_cap": dict(manifest["hypothesis_policy"]),
            }
        )
        if section_ready_callback is not None:
            completed_at = datetime.now(timezone.utc).isoformat()
            for section_id, text, paragraphs in (
                ("facts", facts_text, [facts_paragraph]),
                ("insight", insight_text, insight_paragraphs),
            ):
                section_ready_callback(
                    {
                        "section_id": section_id,
                        "status": "complete",
                        "text": text,
                        "paragraphs": paragraphs,
                        "evidence": [
                            evidence
                            for paragraph in paragraphs
                            for evidence in paragraph["evidence"]
                        ],
                        "checks": {
                            "source_availability": "passed",
                            "skipped": 0,
                            "unresolved_mismatch_count": 0,
                        },
                        "completed_at": completed_at,
                        "elapsed_ms": (time.monotonic() - started) * 1000,
                    }
                )
        return InsightLaneOutcome(
            text=f"## 종합 인사이트\n{insight_text}" if insight_text else "",
            insight_text=insight_text,
            facts_text=facts_text,
            trace=_finish_trace(trace, started),
        )

    def _generate_split_ladders(
        self,
        generator: Any,
        plan: Any,
        core_answer: str,
        *,
        fact_digest: FactDigest,
        facts_fallback: str,
        cap_s: float,
        trace: dict[str, Any],
        started: float,
        injection: str,
        section_ready_callback: SectionReadyCallback | None,
    ) -> InsightLaneOutcome:
        l3_material = prepare_insight_material(
            fact_digest,
            force_compression=True,
            char_threshold=self._compression_char_threshold,
            card_limit=_L3_CARD_LIMIT,
            metric_limit=_L3_COMPRESSED_METRIC_LIMIT,
        )
        l2_material = prepare_insight_material(
            fact_digest,
            force_compression=True,
            char_threshold=self._compression_char_threshold,
            card_limit=_L2_CARD_LIMIT,
            metric_limit=_L2_METRIC_LIMIT,
        )
        trace["material"] = l3_material.trace
        trace["l2_material"] = l2_material.trace

        def run(section: str) -> dict[str, Any]:
            return self._generate_section_ladder(
                generator,
                plan,
                core_answer,
                section=section,
                fact_digest=fact_digest,
                l3_digest=l3_material.digest,
                l2_digest=l2_material.digest,
                cap_s=cap_s,
                started=started,
                injection=injection,
            )

        section_started: dict[str, float] = {}
        if section_ready_callback is not None:
            for section in ("facts", "insight"):
                section_started[section] = time.monotonic()
                section_ready_callback(
                    {
                        "section_id": section,
                        "status": "streaming",
                        "started_at": datetime.now(timezone.utc).isoformat(),
                    }
                )

        sections: dict[str, dict[str, Any]] = {}
        prepared: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="insight-section") as pool:
            futures = {pool.submit(run, section): section for section in ("facts", "insight")}
            for future in as_completed(futures):
                section = futures[future]
                result = future.result()
                sections[section] = result
                prepared[section] = _prepare_stream_section(
                    section,
                    result,
                    fact_digest=fact_digest,
                    facts_fallback=facts_fallback,
                )
                release_status = (
                    "complete"
                    if int(prepared[section]["checks"]["unresolved_mismatch_count"]) == 0
                    else "held"
                )
                prepared[section]["release_status"] = release_status
                if section_ready_callback is not None and release_status == "complete":
                    section_ready_callback(
                        {
                            "section_id": section,
                            "status": "complete",
                            "text": prepared[section]["text"],
                            "paragraphs": prepared[section]["paragraphs"],
                            "evidence": prepared[section]["evidence"],
                            "checks": prepared[section]["checks"],
                            "completed_at": datetime.now(timezone.utc).isoformat(),
                            "elapsed_ms": (
                                time.monotonic() - section_started.get(section, started)
                            )
                            * 1000,
                        }
                    )

        attempts = [
            attempt
            for section in ("facts", "insight")
            for attempt in sections[section]["attempts"]
        ]
        trace["ladder_attempts"] = attempts
        levels = {section: str(sections[section]["level"]) for section in sections}
        parsed_facts = sections["facts"].get("parsed")
        parsed_insight = sections["insight"].get("parsed")
        facts_text = str(prepared["facts"]["text"])
        insight_text = str(prepared["insight"]["text"])
        required_item_coverage = dict(prepared["facts"]["required_item_coverage"])
        hira_scope_notice = dict(prepared["facts"]["hira_scope_notice"])
        material_floor = dict(prepared["facts"]["material_floor"])
        facts_floor = dict(prepared["facts"]["facts_floor"])
        audited_insight, numeric_consistency = _align_section_metric_values(
            facts_text,
            insight_text,
            parsed_facts=parsed_facts,
            parsed_insight=parsed_insight,
            fact_digest=fact_digest,
        )
        audited_facts, population_audit_insight, section_population_consistency = (
            align_section_population_counts(
                facts_text,
                insight_text,
                answer_type=fact_digest.answer_type,
            )
        )
        query_breakdown_surface = dict(
            prepared["facts"].get("query_breakdown_surface") or {}
        )
        facts_surface_counts = dict(
            prepared["facts"]["checks"]["surface_counters"]
        )
        insight_surface_counts = dict(
            prepared["insight"]["checks"]["surface_counters"]
        )
        claim_manifest = _merge_section_manifests(
            parsed_facts.manifest if parsed_facts is not None else None,
            parsed_insight.manifest if parsed_insight is not None else None,
            required_item_coverage=required_item_coverage,
        )
        claim_manifest = _append_query_breakdown_paragraphs(
            claim_manifest,
            query_breakdown_surface,
        )
        claim_manifest = _append_hira_scope_notice_paragraph(
            claim_manifest,
            hira_scope_notice,
        )
        claim_manifest = _append_facts_floor_paragraphs(
            claim_manifest,
            facts_fallback=facts_fallback,
            deterministic_extension=build_grounded_facts_extension(fact_digest),
            floor_trace=facts_floor,
            final_facts_text=facts_text,
            fact_digest=fact_digest,
        )
        claim_manifest = _sync_manifest_surface_text(
            claim_manifest,
            facts_text=facts_text,
            answer_type=fact_digest.answer_type,
        )
        claim_manifest = _use_released_section_paragraphs(
            claim_manifest,
            prepared,
        )
        trace.update(
            {
                "status": "completed",
                "fallback_required": False,
                "error_type": None,
                "structured_output": parsed_facts is not None or parsed_insight is not None,
                "ladder_level": levels["facts"] if levels["facts"] == levels["insight"] else "MIXED",
                "section_ladder_levels": levels,
                "ladder_complete": True,
                "numeric_consistency": numeric_consistency,
                "section_population_consistency": section_population_consistency,
                "final_surface_counters": {
                    key: int(facts_surface_counts.get(key, 0))
                    + int(insight_surface_counts.get(key, 0))
                    for key in set(facts_surface_counts) | set(insight_surface_counts)
                },
                "parse_attempt_count": len(attempts),
                "attempts": attempts,
                "section_retries": {
                    "empty_or_parse": sum(
                        int(section.get("retry_reasons", {}).get(reason, 0))
                        for section in sections.values()
                        for reason in ("empty_response", "parse_error")
                    ),
                    "transport": sum(
                        int(section.get("retry_reasons", {}).get("transport_error", 0))
                        for section in sections.values()
                    ),
                    "length": 0,
                },
                "section_failure_counts": {
                    reason: sum(
                        attempt.get("failure_reason") == reason for attempt in attempts
                    )
                    for reason in ("length", "empty_response", "parse_error", "transport_error")
                },
                "section_ladders": {
                    key: {
                        "level": value["level"],
                        "retry_count": value["retry_count"],
                        "descent_reason": value.get("descent_reason"),
                    }
                    for key, value in sections.items()
                },
                "claim_manifest": claim_manifest,
                "facts_floor": facts_floor,
                "hira_scope_notice": hira_scope_notice,
                "query_breakdown_surface": query_breakdown_surface,
                "material_floor": material_floor,
                "section_release": {
                    section: {
                        "status": prepared[section]["release_status"],
                        "checks": dict(prepared[section]["checks"]),
                    }
                    for section in ("facts", "insight")
                },
                "section_reasoning": _section_reasoning_trace(sections),
                "provider_partial_json_streaming": {
                    "enabled": False,
                    "reason": "complete_claim_envelope_required",
                },
                "post_release_mutation_count": 0,
                "post_release_audit": {
                    "numeric_would_mutate": int(audited_insight != insight_text),
                    "population_would_mutate": int(
                        audited_facts != facts_text
                        or population_audit_insight != insight_text
                    ),
                },
                "empty_section_count": int(not insight_text) + int(not facts_text),
            }
        )
        return InsightLaneOutcome(
            text=f"## 종합 인사이트\n{insight_text}".rstrip(),
            insight_text=insight_text,
            facts_text=facts_text,
            trace=_finish_trace(trace, started),
        )

    def _generate_section_ladder(
        self,
        generator: Any,
        plan: Any,
        core_answer: str,
        *,
        section: str,
        fact_digest: FactDigest,
        l3_digest: FactDigest,
        l2_digest: FactDigest,
        cap_s: float,
        started: float,
        injection: str,
    ) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []
        retry_count = 0
        retry_reasons: dict[str, int] = {}
        l3_budget = min(self._timeout_s, cap_s)
        attempt = self._structured_attempt(
            generator,
            plan,
            core_answer,
            fact_digest=l3_digest,
            validation_digest=fact_digest,
            level="L3",
            budget_s=l3_budget,
            retry_error=None,
            injection=injection,
            section=section,
        )
        attempts.append(attempt["trace"])
        if attempt["parsed"] is not None:
            return {"parsed": attempt["parsed"], "level": "L3", "attempts": attempts, "retry_count": 0, "retry_reasons": retry_reasons}
        if attempt["failure_reason"] in {"empty_response", "parse_error", "transport_error"}:
            retry_count += 1
            retry_reasons[attempt["failure_reason"]] = 1
            retry = self._structured_attempt(
                generator,
                plan,
                core_answer,
                fact_digest=l3_digest,
                validation_digest=fact_digest,
                level="L3",
                budget_s=max(0.1, min(l3_budget, cap_s - (time.monotonic() - started))),
                retry_error=f"L3 {section} 생성 실패: {attempt['error']}",
                injection=injection,
                section=section,
            )
            attempts.append(retry["trace"])
            attempt = retry
            if retry["parsed"] is not None:
                return {"parsed": retry["parsed"], "level": "L3", "attempts": attempts, "retry_count": retry_count, "retry_reasons": retry_reasons}

        remaining = max(0.0, cap_s - (time.monotonic() - started))
        if remaining <= 0:
            return {"parsed": None, "level": "L1", "attempts": attempts, "retry_count": retry_count, "retry_reasons": retry_reasons, "descent_reason": "total_cap_exhausted"}
        l2 = self._structured_attempt(
            generator,
            plan,
            core_answer,
            fact_digest=l2_digest,
            validation_digest=fact_digest,
            level="L2",
            budget_s=min(self._l2_timeout_s, remaining),
            retry_error=f"L3 {section} 실패: {attempt['error']}",
            injection=injection,
            section=section,
        )
        attempts.append(l2["trace"])
        if l2["parsed"] is not None:
            return {"parsed": l2["parsed"], "level": "L2", "attempts": attempts, "retry_count": retry_count, "retry_reasons": retry_reasons}
        if l2["failure_reason"] in {"empty_response", "parse_error", "transport_error"}:
            remaining = max(0.0, cap_s - (time.monotonic() - started))
            if remaining > 0:
                retry_count += 1
                retry_reasons[l2["failure_reason"]] = retry_reasons.get(l2["failure_reason"], 0) + 1
                retry = self._structured_attempt(
                    generator,
                    plan,
                    core_answer,
                    fact_digest=l2_digest,
                    validation_digest=fact_digest,
                    level="L2",
                    budget_s=min(self._l2_timeout_s, remaining),
                    retry_error=f"L2 {section} 생성 실패: {l2['error']}",
                    injection=injection,
                    section=section,
                )
                attempts.append(retry["trace"])
                if retry["parsed"] is not None:
                    return {"parsed": retry["parsed"], "level": "L2", "attempts": attempts, "retry_count": retry_count, "retry_reasons": retry_reasons}
                l2 = retry
        return {"parsed": None, "level": "L1", "attempts": attempts, "retry_count": retry_count, "retry_reasons": retry_reasons, "descent_reason": l2["error"]}

    def _generate_ladder(
        self,
        generator: Any,
        plan: Any,
        core_answer: str,
        *,
        fact_digest: FactDigest,
        facts_fallback: str,
        cap_s: float,
        trace: dict[str, Any],
        started: float,
        injection: str,
    ) -> InsightLaneOutcome:
        l3_material = prepare_insight_material(
            fact_digest,
            force_compression=False,
            char_threshold=self._compression_char_threshold,
            card_limit=_L3_CARD_LIMIT,
            metric_limit=_L3_COMPRESSED_METRIC_LIMIT,
        )
        trace["material"] = l3_material.trace
        l2_reserve = min(
            self._l2_timeout_s,
            cap_s * 0.4,
        )
        l3_budget = min(self._timeout_s, max(0.0, cap_s - l2_reserve))
        if l3_budget <= 0:
            return self._complete_l1(
                fact_digest,
                facts_text=facts_fallback,
                trace=trace,
                started=started,
                reason="l3_budget_unavailable",
            )

        l3 = self._structured_attempt(
            generator,
            plan,
            core_answer,
            fact_digest=l3_material.digest,
            validation_digest=fact_digest,
            level="L3",
            budget_s=l3_budget,
            retry_error=None,
            injection=injection,
        )
        trace["ladder_attempts"].append(l3["trace"])
        if l3["parsed"] is not None:
            return self._complete_structured(
                l3,
                facts_fallback=facts_fallback,
                fact_digest=fact_digest,
                trace=trace,
                started=started,
                level="L3",
            )

        remaining_cap = max(0.0, cap_s - (time.monotonic() - started))
        l2_budget = min(self._l2_timeout_s, remaining_cap)
        if l2_budget <= 0:
            return self._complete_l1(
                fact_digest,
                facts_text=facts_fallback,
                trace=trace,
                started=started,
                reason="total_cap_exhausted",
            )

        l2_material = prepare_insight_material(
            fact_digest,
            force_compression=True,
            char_threshold=self._compression_char_threshold,
            card_limit=_L2_CARD_LIMIT,
            metric_limit=_L2_METRIC_LIMIT,
        )
        trace["l2_material"] = l2_material.trace
        l2_error = (
            f"L3 실패: {l3['error']}. L2 축약 재시도: 12~16개 claim, "
            "INTERP+HYPO 4개 이상을 포함해 facts와 insight 두 섹션을 "
            "모두 완결하세요."
        )
        l2 = self._structured_attempt(
            generator,
            plan,
            core_answer,
            fact_digest=l2_material.digest,
            validation_digest=fact_digest,
            level="L2",
            budget_s=l2_budget,
            retry_error=l2_error,
            injection=injection,
        )
        trace["ladder_attempts"].append(l2["trace"])
        if l2["parsed"] is not None:
            return self._complete_structured(
                l2,
                facts_fallback=facts_fallback,
                fact_digest=fact_digest,
                trace=trace,
                started=started,
                level="L2",
            )
        return self._complete_l1(
            fact_digest,
            facts_text=facts_fallback,
            trace=trace,
            started=started,
            reason=f"L2 실패: {l2['error']}",
        )

    def _structured_attempt(
        self,
        generator: Any,
        plan: Any,
        core_answer: str,
        *,
        fact_digest: FactDigest,
        validation_digest: FactDigest,
        level: str,
        budget_s: float,
        retry_error: str | None,
        injection: str,
        section: str | None = None,
    ) -> dict[str, Any]:
        attempt_started = time.monotonic()
        attempt_trace: dict[str, Any] = {
            "level": level,
            "budget_s": budget_s,
        }
        try:
            if level == "L3" and injection == "exception":
                raise RuntimeError("forced insight lane exception")
            if level == "L3" and injection == "timeout":
                raise TimeoutError("forced insight lane timeout")
            if injection == "l3_l2_failure":
                raise TimeoutError(f"forced {level} insight lane timeout")
            outcome = generator(
                plan,
                core_answer,
                fact_digest=fact_digest,
                retry_error=retry_error,
                budget_s=budget_s,
                **({"section": section} if section is not None else {}),
            )
            attempt_trace.update(dict(outcome.trace))
            finish_reason = outcome.trace.get("finish_reason")
            if finish_reason == "length":
                raise ClaimPayloadError("finish_reason=length")
            if finish_reason != "stop":
                raise ClaimPayloadError(f"finish_reason={finish_reason}")
            if not outcome.text.strip():
                raise ClaimPayloadError("empty response body")
            parsed = parse_claim_payload(
                outcome.text,
                validation_digest,
                supplement_missing=False,
            )
            if section == "facts" and not parsed.facts_text:
                raise ClaimPayloadError("facts section missing")
            if section == "facts":
                material_floor = _structured_material_floor(validation_digest)
                facts_chars = len(re.sub(r"\s+", "", parsed.facts_text))
                minimum_chars = int(material_floor["facts_minimum_chars"])
                attempt_trace["facts_volume_floor"] = {
                    "chars": facts_chars,
                    "minimum_chars": minimum_chars,
                    "relaxed": bool(material_floor["relaxed"]),
                    "enforced": material_floor["reason"] == "파일 재료량 기준 응답",
                    "floor_met_before_extension": facts_chars >= minimum_chars,
                    "deferred_to_grounded_extension": facts_chars < minimum_chars,
                }
            if section == "insight" and not parsed.insight_text:
                raise ClaimPayloadError("insight section missing")
            attempt_trace.update(
                {
                    "status": "completed",
                    "parse_status": "parsed",
                    "elapsed_ms": (time.monotonic() - attempt_started) * 1000,
                }
            )
            return {"parsed": parsed, "trace": attempt_trace, "error": None, "failure_reason": None}
        except Exception as exc:  # noqa: BLE001 - one failed rung must descend
            claim_payload_error = (
                exc.details if isinstance(exc, ClaimPayloadError) else None
            )
            finish_reason = attempt_trace.get("finish_reason")
            failure_reason = (
                "length"
                if finish_reason == "length"
                else "empty_response"
                if "empty response body" in str(exc)
                else "parse_error"
                if isinstance(exc, ClaimPayloadError) and finish_reason == "stop"
                else "transport_error"
            )
            return {
                "parsed": None,
                "trace": {
                    **attempt_trace,
                    "status": "failed",
                    "parse_status": "rejected",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "claim_payload_error": claim_payload_error,
                    "failure_reason": failure_reason,
                    "elapsed_ms": (time.monotonic() - attempt_started) * 1000,
                },
                "error": f"{type(exc).__name__}: {exc}",
                "failure_reason": failure_reason,
            }

    def _complete_structured(
        self,
        attempt: dict[str, Any],
        *,
        facts_fallback: str,
        fact_digest: FactDigest,
        trace: dict[str, Any],
        started: float,
        level: str,
    ) -> InsightLaneOutcome:
        parsed = attempt["parsed"]
        trace.update(attempt["trace"])
        trace.update(
            {
                "status": "completed",
                "fallback_required": False,
                "error_type": None,
                "structured_output": True,
                "ladder_level": level,
                "ladder_complete": True,
                "parse_attempt_count": len(trace["ladder_attempts"]),
                "attempts": list(trace["ladder_attempts"]),
                "claim_manifest": parsed.manifest,
            }
        )
        insight_text = parsed.insight_text or _section_body(
            build_l1_insight(fact_digest).text
        )
        facts_text = parsed.facts_text or facts_fallback
        facts_text, required_item_coverage = ensure_required_item_coverage(
            facts_text,
            parsed.claims,
            fact_digest,
        )
        parsed.manifest["required_item_coverage"] = required_item_coverage
        trace["empty_section_count"] = int(not insight_text) + int(not facts_text)
        return InsightLaneOutcome(
            text=parsed.text,
            insight_text=insight_text,
            facts_text=facts_text,
            trace=_finish_trace(trace, started),
        )

    def _generate_legacy_or_l1(
        self,
        plan: Any,
        core_answer: str,
        *,
        fact_digest: FactDigest,
        facts_fallback: str,
        cap_s: float,
        trace: dict[str, Any],
        started: float,
        injection: str,
    ) -> InsightLaneOutcome:
        try:
            if injection == "exception":
                raise RuntimeError("forced insight lane exception")
            if injection == "timeout":
                raise TimeoutError("forced insight lane timeout")
            outcome = self._synthesizer.repair_insight_after_semantic(
                plan,
                core_answer,
                fact_digest=fact_digest,
                failure_trace={"reason_code": "split_lane_generation"},
                budget_s=min(self._timeout_s, cap_s),
            )
            text = outcome.text.strip()
            if text:
                trace.update(outcome.trace)
                trace.update(
                    {
                        "status": "completed",
                        "fallback_required": False,
                        "structured_output": False,
                        "ladder_level": "L3_LEGACY",
                        "ladder_complete": True,
                    }
                )
                return InsightLaneOutcome(
                    text=text,
                    insight_text=_section_body(text),
                    facts_text=facts_fallback,
                    trace=_finish_trace(trace, started),
                )
        except Exception as exc:  # noqa: BLE001 - legacy failure descends to L1
            trace["legacy_error"] = f"{type(exc).__name__}: {exc}"
        return self._complete_l1(
            fact_digest,
            facts_text=facts_fallback,
            trace=trace,
            started=started,
            reason=str(trace.get("legacy_error") or "legacy_empty"),
        )

    def _complete_l1(
        self,
        fact_digest: FactDigest,
        *,
        facts_text: str,
        trace: dict[str, Any],
        started: float,
        reason: str,
    ) -> InsightLaneOutcome:
        deterministic = build_l1_insight(fact_digest)
        insight_text = _section_body(deterministic.text)
        facts_text = facts_text or "조회된 사실을 조사 결과로 구성하지 못했습니다."
        if reason == "total_cap_exhausted":
            notice = "일부 원천이 시간 내 응답하지 않아 제외됨"
            if notice not in facts_text:
                facts_text = f"{facts_text.rstrip()}\n\n{notice}"
        facts_text, required_item_coverage = ensure_required_item_coverage(
            facts_text,
            (),
            fact_digest,
        )
        claim_manifest = dict(deterministic.manifest)
        claim_manifest["required_item_coverage"] = required_item_coverage
        ladder_attempts = list(trace.get("ladder_attempts", ()))
        trace.update(
            {
                "status": "completed",
                "fallback_required": False,
                "error_type": None,
                "structured_output": False,
                "ladder_level": "L1",
                "ladder_complete": True,
                "descent_reason": reason,
                "parse_attempt_count": len(ladder_attempts),
                "attempts": ladder_attempts,
                "parse_errors": [
                    str(attempt.get("error_message") or "")
                    for attempt in ladder_attempts
                    if attempt.get("status") == "failed"
                ],
                "claim_manifest": claim_manifest,
                "empty_section_count": int(not insight_text) + int(not facts_text),
            }
        )
        return InsightLaneOutcome(
            text=deterministic.text,
            insight_text=insight_text,
            facts_text=facts_text,
            trace=_finish_trace(trace, started),
        )


def _prepare_stream_section(
    section: str,
    result: Mapping[str, Any],
    *,
    fact_digest: FactDigest,
    facts_fallback: str,
) -> dict[str, Any]:
    parsed = result.get("parsed")
    manifest = dict(parsed.manifest) if parsed is not None else {}
    required_item_coverage: dict[str, Any] = {}
    hira_scope_notice: dict[str, Any] = {}
    facts_floor: dict[str, Any] = {}
    material_floor = _structured_material_floor(fact_digest)
    if section == "facts":
        text = parsed.facts_text if parsed is not None else facts_fallback
        text = text or "조회된 사실을 조사 결과로 구성하지 못했습니다."
        deterministic_extension = build_grounded_facts_extension(fact_digest)
        text, facts_floor = _ensure_facts_floor(
            text,
            facts_fallback,
            deterministic_extension=deterministic_extension,
            minimum_chars=int(material_floor["facts_minimum_chars"]),
        )
        text, required_item_coverage = ensure_required_item_coverage(
            text,
            parsed.claims if parsed is not None else (),
            fact_digest,
        )
        text, hira_scope_notice = _ensure_hira_scope_notice(text, fact_digest)
        text, _unused, query_surface = _ensure_query_breakdown_sections(
            text, "", fact_digest
        )
        manifest = _append_query_breakdown_paragraphs(manifest, query_surface)
        manifest = _append_hira_scope_notice_paragraph(manifest, hira_scope_notice)
        manifest = _append_facts_floor_paragraphs(
            manifest,
            facts_fallback=facts_fallback,
            deterministic_extension=deterministic_extension,
            floor_trace=facts_floor,
            final_facts_text=text,
            fact_digest=fact_digest,
        )
    else:
        text = (
            parsed.insight_text
            if parsed is not None
            else _section_body(build_l1_insight(fact_digest).text)
        )
        _unused, text, query_surface = _ensure_query_breakdown_sections(
            "", text, fact_digest
        )
        manifest = _append_query_breakdown_paragraphs(manifest, query_surface)
    text, table_trace = align_text_to_table_authority(text, (facts_fallback,))
    table_trace = _require_complete_clinical_sigma(table_trace, fact_digest)
    text, metric_trace = _align_stream_metrics(text, parsed, fact_digest)
    text, population_trace = _align_stream_population(text, fact_digest)
    text, distribution_trace = _align_stream_clinical_distribution(text, fact_digest)
    text, missing_counts = remove_missing_period_zero_claims(text, fact_digest)
    text, surface_counts = normalize_final_surface_text(text)
    surface_counts.update(missing_counts)
    if section == "facts":
        text, final_floor = _ensure_facts_floor(
            text,
            facts_fallback,
            deterministic_extension=deterministic_extension,
            minimum_chars=int(material_floor["facts_minimum_chars"]),
        )
        if (
            final_floor["core_fallback_appended"]
            or final_floor["deterministic_extension_appended"]
        ):
            text, final_surface_counts = normalize_final_surface_text(text)
            surface_counts.update(final_surface_counts)
            manifest = _append_facts_floor_paragraphs(
                manifest,
                facts_fallback=facts_fallback,
                deterministic_extension=deterministic_extension,
                floor_trace=final_floor,
                final_facts_text=text,
                fact_digest=fact_digest,
            )
        facts_floor["final_before_chars"] = final_floor["before_chars"]
        facts_floor["final_after_chars"] = final_floor["after_chars"]
        facts_floor["final_floor_met"] = final_floor["floor_met"]
    manifest = _sync_manifest_surface_text(
        manifest,
        facts_text=text if section == "facts" else "",
        answer_type=fact_digest.answer_type,
    )
    paragraphs: list[dict[str, Any]] = []
    paragraph_template_seen: set[str] = set()
    paragraph_sentence_seen: set[str] = set()
    for raw_paragraph in dict(manifest.get("section_paragraphs") or {}).get(
        section, ()
    ):
        if not isinstance(raw_paragraph, Mapping):
            continue
        paragraph = dict(raw_paragraph)
        paragraph_text = str(paragraph.get("text") or "")
        paragraph_text, _table = align_text_to_table_authority(
            paragraph_text, (facts_fallback,)
        )
        paragraph_text, _metric = _align_stream_metrics(
            paragraph_text, parsed, fact_digest
        )
        paragraph_text, _population = _align_stream_population(
            paragraph_text, fact_digest
        )
        paragraph_text, _distribution = _align_stream_clinical_distribution(
            paragraph_text, fact_digest
        )
        paragraph_text, _missing = remove_missing_period_zero_claims(
            paragraph_text, fact_digest
        )
        paragraph_text, _surface = normalize_final_surface_text(
            paragraph_text,
            template_seen=paragraph_template_seen,
            sentence_seen=paragraph_sentence_seen,
        )
        if paragraph_text:
            paragraph["text"] = paragraph_text
            paragraphs.append(paragraph)
    evidence: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for paragraph in paragraphs:
        for item in paragraph.get("evidence", ()):
            if not isinstance(item, Mapping):
                continue
            evidence_id = str(item.get("evidence_id") or "")
            if evidence_id and evidence_id not in seen_ids:
                evidence.append(dict(item))
                seen_ids.add(evidence_id)
    binding_trace = _binding_release_trace(manifest, section)
    unresolved = (
        int(metric_trace["unresolved_mismatch_count"])
        + int(population_trace["unresolved_mismatch_count"])
        + int(distribution_trace["unresolved_mismatch_count"])
        + int(table_trace["unresolved_count"])
        + int(binding_trace["unresolved_mismatch_count"])
    )
    return {
        "text": text,
        "paragraphs": paragraphs,
        "evidence": evidence,
        "checks": {
            "numeric": "passed" if not metric_trace["unresolved_mismatch_count"] else "failed",
            "direction": "passed",
            "labels": "passed",
            "population": (
                "passed"
                if not population_trace["unresolved_mismatch_count"]
                else "failed"
            ),
            "evidence_binding": (
                "passed"
                if not binding_trace["unresolved_mismatch_count"]
                else "failed"
            ),
            "binding_release": binding_trace,
            "skipped": 0,
            "unresolved_mismatch_count": unresolved,
            "metric_corrections": metric_trace["correction_count"],
            "population_corrections": population_trace["correction_count"],
            "clinical_distribution": distribution_trace,
            "table_body_parity": table_trace,
            "surface_counters": surface_counts,
        },
        "required_item_coverage": required_item_coverage,
        "hira_scope_notice": hira_scope_notice,
        "facts_floor": facts_floor,
        "material_floor": material_floor,
        "query_breakdown_surface": query_surface,
    }


def _require_complete_clinical_sigma(
    table_trace: Mapping[str, Any],
    fact_digest: FactDigest,
) -> dict[str, Any]:
    """Hold clinical sections until all deterministic distributions are checked."""

    updated = dict(table_trace)
    has_clinical = any(
        card.source == "clinicaltrials" for card in fact_digest.cards
    )
    expected = 3 if has_clinical else 0
    checked = int(updated.get("sigma_checked_count") or 0)
    missing = max(0, expected - checked)
    updated["expected_sigma_check_count"] = expected
    updated["missing_sigma_check_count"] = missing
    updated["unresolved_count"] = int(updated.get("unresolved_count") or 0) + missing
    return updated


def _binding_release_trace(
    manifest: Mapping[str, Any], section: str
) -> dict[str, int]:
    verification = dict(manifest.get("verification") or {})
    handled_blocks = int(verification.get("hard_block_count") or 0)
    explicit_unresolved = int(
        verification.get("unresolved_mismatch_count")
        or verification.get("unresolved_count")
        or 0
    )
    emitted_text = " ".join(
        str(paragraph.get("text") or "")
        for paragraph in dict(manifest.get("section_paragraphs") or {}).get(
            section, ()
        )
        if isinstance(paragraph, Mapping)
    )
    normalized_emitted = " ".join(emitted_text.split())
    leaked_blocks = 0
    for claim in verification.get("claims", ()):
        if not isinstance(claim, Mapping) or claim.get("action") != "blocked":
            continue
        if claim.get("final_text") is not None:
            continue
        original_text = " ".join(str(claim.get("original_text") or "").split())
        if original_text and original_text in normalized_emitted:
            leaked_blocks += 1
    return {
        "handled_block_count": handled_blocks,
        "blocked_claim_leak_count": leaked_blocks,
        "explicit_unresolved_count": explicit_unresolved,
        "unresolved_mismatch_count": explicit_unresolved + leaked_blocks,
    }


def _align_stream_metrics(
    text: str,
    parsed: Any,
    fact_digest: FactDigest,
) -> tuple[str, dict[str, int]]:
    corrected = text
    metrics = {metric.id: metric for metric in fact_digest.derived_metrics}
    mismatch_count = 0
    correction_count = 0
    if parsed is not None:
        for claim in parsed.claims:
            identifiers = [item for item in claim.evidence_ids if item in metrics]
            if len(identifiers) != 1:
                continue
            expected = _decimal_value(metrics[identifiers[0]].value)
            if expected is None or _contains_decimal(claim.text, expected):
                continue
            tokens = _NUMBER_TOKEN_RE.findall(claim.text)
            if len(tokens) != 1:
                continue
            mismatch_count += 1
            replacement = _metric_surface_value(metrics[identifiers[0]].value)
            replacement_text = claim.text.replace(tokens[0], replacement, 1)
            if claim.text in corrected:
                corrected = corrected.replace(claim.text, replacement_text, 1)
                correction_count += 1
    return corrected, {
        "mismatch_count": mismatch_count,
        "correction_count": correction_count,
        "unresolved_mismatch_count": mismatch_count - correction_count,
    }


def _align_stream_population(
    text: str,
    fact_digest: FactDigest,
) -> tuple[str, dict[str, int]]:
    specifications: list[tuple[int, tuple[re.Pattern[str], ...]]] = []
    patent_card = next(
        (item for item in fact_digest.cards if item.source == "patent"), None
    )
    if patent_card is not None:
        raw_expected = patent_card.full_stats.get("product_combination_count")
        if raw_expected is not None:
            patterns = (
                _PATENT_INSIGHT_POPULATION_PATTERNS
                if fact_digest.answer_type == "patent"
                else _PATENT_FACTS_POPULATION_PATTERNS
            )
            specifications.append((int(raw_expected), patterns))
    clinical_card = next(
        (item for item in fact_digest.cards if item.source == "clinicaltrials"), None
    )
    if clinical_card is not None:
        raw_expected = clinical_card.full_stats.get(
            "direct_related_count",
            clinical_card.full_stats.get("direct_combination_count"),
        )
        if raw_expected is not None:
            specifications.append((int(raw_expected), _CLINICAL_POPULATION_PATTERNS))
    if not specifications:
        return text, {
            "mismatch_count": 0,
            "correction_count": 0,
            "unresolved_mismatch_count": 0,
        }
    corrected = text
    mismatch_count = 0
    correction_count = 0
    for expected, patterns in specifications:
        offset = 0
        candidate = corrected
        for match in _population_matches(candidate, patterns):
            if int(match.group("count").replace(",", "")) == expected:
                continue
            mismatch_count += 1
            replacement = f"{expected:,}"
            start = match.start("count") + offset
            end = match.end("count") + offset
            corrected = f"{corrected[:start]}{replacement}{corrected[end:]}"
            offset += len(replacement) - (end - start)
            correction_count += 1
    return corrected, {
        "mismatch_count": mismatch_count,
        "correction_count": correction_count,
        "unresolved_mismatch_count": mismatch_count - correction_count,
    }


_CLINICAL_STATUS_SEQUENCE_RE = re.compile(
    r"(?:완료|모집\s*중|모집\s*전|활성\s*비모집|중단|진행)\s*"
    r"\d[\d,]*건(?:\s*[·,]\s*"
    r"(?:완료|모집\s*중|모집\s*전|활성\s*비모집|중단|진행)\s*"
    r"\d[\d,]*건)+"
)


def _align_stream_clinical_distribution(
    text: str,
    fact_digest: FactDigest,
) -> tuple[str, dict[str, int]]:
    card = next(
        (item for item in fact_digest.cards if item.source == "clinicaltrials"),
        None,
    )
    raw_counts = card.full_stats.get("direct_status_counts") if card else None
    population = card.full_stats.get("direct_related_count") if card else None
    counters = {
        "distribution_checked": 0,
        "distribution_mismatch": 0,
        "distribution_corrected": 0,
        "distribution_sum_mismatch": 0,
        "unresolved_mismatch_count": 0,
    }
    if not isinstance(raw_counts, Mapping) or not isinstance(population, int):
        return text, counters
    buckets: dict[str, int] = {}
    active = {
        "RECRUITING",
        "NOT_YET_RECRUITING",
        "ACTIVE_NOT_RECRUITING",
        "ENROLLING_BY_INVITATION",
    }
    stopped = {"TERMINATED", "WITHDRAWN", "SUSPENDED"}
    for raw_status, raw_count in raw_counts.items():
        if not isinstance(raw_count, int) or raw_count <= 0:
            continue
        status = str(raw_status).strip().upper()
        label = (
            "완료"
            if status == "COMPLETED"
            else "모집중"
            if status in active
            else "중단"
            if status in stopped
            else "상태 미기재"
            if status == "__MISSING__"
            else str(raw_status)
        )
        buckets[label] = buckets.get(label, 0) + raw_count
    if sum(buckets.values()) != population:
        counters["distribution_sum_mismatch"] = 1
        counters["unresolved_mismatch_count"] = 1
        return text, counters
    order = ("완료", "모집중", "중단", "상태 미기재")
    labels = sorted(buckets, key=lambda label: (order.index(label) if label in order else len(order), label))
    authoritative = " · ".join(f"{label} {buckets[label]:,}건" for label in labels)
    matches = list(_CLINICAL_STATUS_SEQUENCE_RE.finditer(text))
    if not matches:
        return text, counters
    counters["distribution_checked"] = len(matches)
    updated = text
    for match in reversed(matches):
        if " ".join(match.group(0).split()) == authoritative:
            continue
        counters["distribution_mismatch"] += 1
        updated = f"{updated[:match.start()]}{authoritative}{updated[match.end():]}"
        counters["distribution_corrected"] += 1
    return updated, counters


def _section_reasoning_trace(
    sections: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    keys = ("prompt_tokens", "completion_tokens", "reasoning_tokens", "text_tokens")
    result: dict[str, Any] = {}
    totals = {key: 0 for key in keys}
    for section in ("facts", "insight"):
        usage = {key: 0 for key in keys}
        requested_reasoning_efforts: list[str] = []
        for attempt in sections.get(section, {}).get("attempts", ()):
            requested_effort = (
                str(attempt.get("reasoning_effort") or "not_requested")
                if isinstance(attempt, Mapping)
                else "not_requested"
            )
            if requested_effort not in requested_reasoning_efforts:
                requested_reasoning_efforts.append(requested_effort)
            raw_usage = attempt.get("usage") if isinstance(attempt, Mapping) else None
            if isinstance(raw_usage, Mapping):
                usage["prompt_tokens"] += int(raw_usage.get("prompt_tokens") or 0)
                usage["completion_tokens"] += int(raw_usage.get("completion_tokens") or 0)
                details = raw_usage.get("completion_tokens_details")
                if isinstance(details, Mapping):
                    usage["reasoning_tokens"] += int(
                        details.get("reasoning_tokens") or 0
                    )
                    usage["text_tokens"] += int(details.get("text_tokens") or 0)
                continue
            thinking = attempt.get("thinking") if isinstance(attempt, Mapping) else None
            if not isinstance(thinking, Mapping):
                continue
            usage["completion_tokens"] += int(thinking.get("completion_tokens") or 0)
            usage["reasoning_tokens"] += int(thinking.get("reasoning_tokens") or 0)
            usage["text_tokens"] += int(thinking.get("text_tokens") or 0)
        result[section] = {
            **usage,
            "requested_reasoning_efforts": requested_reasoning_efforts,
        }
        for key in keys:
            totals[key] += usage[key]
    result["turn"] = totals
    result["budget_support"] = "unverified"
    return result


def _merge_section_manifests(
    facts: dict[str, Any] | None,
    insight: dict[str, Any] | None,
    *,
    required_item_coverage: dict[str, Any],
) -> dict[str, Any]:
    facts = facts or {}
    insight = insight or {}
    claims = [*facts.get("claims", ()), *insight.get("claims", ())]
    verification_claims = [
        *facts.get("verification", {}).get("claims", ()),
        *insight.get("verification", {}).get("claims", ()),
    ]
    type_counts: dict[str, int] = {}
    for manifest in (facts, insight):
        for claim_type, count in manifest.get("type_counts", {}).items():
            type_counts[str(claim_type)] = type_counts.get(str(claim_type), 0) + int(count)
    section_paragraphs = {
        "facts": list(facts.get("section_paragraphs", {}).get("facts", ())),
        "insight": list(insight.get("section_paragraphs", {}).get("insight", ())),
    }
    patent_scope_keys = (
        "checked_number_count",
        "violation_count",
        "replacement_count",
        "rewrite_count",
    )
    patent_scope_counters = {
        key: sum(
            int(
                manifest.get("verification", {})
                .get("patent_scope_counters", {})
                .get(key, 0)
                or 0
            )
            for manifest in (facts, insight)
        )
        for key in patent_scope_keys
    }
    surface_counter_keys = {
        key
        for manifest in (facts, insight)
        for key in (
            manifest.get("verification", {})
            .get("surface_counters", {})
        )
    }
    surface_counters = {
        key: sum(
            int(
                manifest.get("verification", {})
                .get("surface_counters", {})
                .get(key, 0)
                or 0
            )
            for manifest in (facts, insight)
        )
        for key in sorted(surface_counter_keys)
    }
    evidence_supply_keys = {
        key
        for manifest in (facts, insight)
        for key in manifest.get("evidence_supply", {})
    }
    evidence_supply = {
        key: sum(
            int(manifest.get("evidence_supply", {}).get(key, 0) or 0)
            for manifest in (facts, insight)
        )
        for key in sorted(evidence_supply_keys)
    }
    return {
        "parse_status": "parsed_split",
        "claim_count": len(claims),
        "type_counts": dict(sorted(type_counts.items())),
        "claims": claims,
        "verification": {
            "claims": verification_claims,
            "hard_block_count": int(facts.get("verification", {}).get("hard_block_count", 0) or 0)
            + int(insight.get("verification", {}).get("hard_block_count", 0) or 0),
            "patent_scope_counters": patent_scope_counters,
            "surface_counters": surface_counters,
        },
        "section_counts": {
            "facts": int(facts.get("section_counts", {}).get("facts", 0) or 0),
            "insight": int(insight.get("section_counts", {}).get("insight", 0) or 0),
        },
        "section_paragraphs": section_paragraphs,
        "paragraph_evidence": {
            "facts": facts.get("paragraph_evidence", {}).get("facts", {}),
            "insight": insight.get("paragraph_evidence", {}).get("insight", {}),
        },
        "claim_evidence": {
            "facts": facts.get("claim_evidence", {}).get("facts", {}),
            "insight": insight.get("claim_evidence", {}).get("insight", {}),
        },
        "evidence_supply": evidence_supply,
        "referenced_evidence_ids": sorted(
            set(facts.get("referenced_evidence_ids", ()))
            | set(insight.get("referenced_evidence_ids", ()))
        ),
        "required_item_coverage": required_item_coverage,
        "section_manifests": {"facts": facts, "insight": insight},
    }


def _sync_manifest_surface_text(
    manifest: dict[str, Any],
    *,
    facts_text: str,
    answer_type: str,
) -> dict[str, Any]:
    result = dict(manifest)
    section_paragraphs = {
        section: [dict(item) for item in paragraphs]
        for section, paragraphs in dict(result.get("section_paragraphs") or {}).items()
    }
    template_seen: set[str] = set()
    for section, paragraphs in section_paragraphs.items():
        sentence_seen: set[str] = set()
        for paragraph in paragraphs:
            text, _counts = normalize_final_surface_text(
                str(paragraph.get("text") or ""),
                template_seen=template_seen,
                sentence_seen=sentence_seen,
            )
            if section == "insight":
                _facts, text, _parity = align_section_population_counts(
                    facts_text,
                    text,
                    answer_type=answer_type,
                )
            paragraph["text"] = text
    result["section_paragraphs"] = section_paragraphs
    return result


def _use_released_section_paragraphs(
    manifest: dict[str, Any],
    prepared: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    result = dict(manifest)
    section_paragraphs: dict[str, list[dict[str, Any]]] = {}
    for section in ("facts", "insight"):
        paragraphs: list[dict[str, Any]] = []
        for raw_paragraph in prepared[section].get("paragraphs", ()):
            if not isinstance(raw_paragraph, Mapping):
                continue
            paragraph = dict(raw_paragraph)
            group = paragraph.get("evidence_group")
            if isinstance(group, Mapping):
                refreshed_group = dict(group)
                members = [
                    item
                    for item in refreshed_group.get("members", ())
                    if isinstance(item, Mapping) and item.get("evidence_id")
                ]
                group_seed = "\x1f".join(
                    (
                        str(paragraph.get("text") or "").strip(),
                        *(str(item["evidence_id"]) for item in members),
                    )
                )
                refreshed_group["group_id"] = (
                    f"eg-{hashlib.sha256(group_seed.encode()).hexdigest()[:16]}"
                )
                paragraph["evidence_group"] = refreshed_group
            paragraphs.append(paragraph)
        section_paragraphs[section] = paragraphs
    result["section_paragraphs"] = section_paragraphs
    return result


_NUMBER_TOKEN_RE = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")


def _ensure_facts_floor(
    facts_text: str,
    facts_fallback: str,
    *,
    deterministic_extension: str = "",
    minimum_chars: int = 950,
) -> tuple[str, dict[str, Any]]:
    """Extend short facts with distinct code-owned, grounded material."""

    before = facts_text.strip()
    fallback = facts_fallback.strip()
    extension = deterministic_extension.strip()
    before_chars = len(re.sub(r"\s+", "", before))
    parts = [before] if before else []
    result = before
    fallback_appended = False
    extension_appended = False
    if before_chars < minimum_chars and fallback and fallback not in result:
        parts.append(fallback)
        result = "\n\n".join(parts).strip()
        fallback_appended = True
    if (
        len(re.sub(r"\s+", "", result)) < minimum_chars
        and extension
        and extension not in result
    ):
        parts.append(extension)
        result = "\n\n".join(parts).strip()
        extension_appended = True
    after_chars = len(re.sub(r"\s+", "", result))
    return result, {
        "minimum_chars": minimum_chars,
        "before_chars": before_chars,
        "after_chars": after_chars,
        "core_fallback_appended": fallback_appended,
        "deterministic_extension_appended": extension_appended,
        "floor_met": after_chars >= minimum_chars,
    }


def _brand_candidate_filter_trace(fact_digest: FactDigest) -> dict[str, Any]:
    """Expose the deterministic file-brand rejection decision in the live trace."""

    for card in fact_digest.cards:
        raw = card.file_facts.get("brand_candidate_filter")
        if not isinstance(raw, Mapping) or not raw:
            continue
        return {
            "candidate": str(raw.get("candidate") or "") or None,
            "excluded": bool(raw.get("excluded")),
            "reason": str(raw.get("reason") or "no_candidate"),
            "excluded_count": int(raw.get("excluded_count") or 0),
        }
    return {
        "candidate": None,
        "excluded": False,
        "reason": "not_applicable",
        "excluded_count": 0,
    }


def _ensure_hira_scope_notice(
    facts_text: str,
    fact_digest: FactDigest,
) -> tuple[str, dict[str, Any]]:
    """Preserve code-owned HIRA population notices on the facts surface."""

    if fact_digest.answer_type != "disease":
        return facts_text, {
            "appended": False,
            "text": "",
            "notice_count": 0,
            "evidence_ids": (),
            "reason": "lane_not_relevant",
        }
    notices: list[str] = []
    evidence_ids: list[str] = []
    for card in fact_digest.cards:
        if card.source != "hira":
            continue
        raw_notices = card.full_stats.get("scope_notices")
        if isinstance(raw_notices, Sequence) and not isinstance(raw_notices, str):
            for raw_notice in raw_notices:
                notice = str(raw_notice).strip()
                if notice and notice not in notices:
                    notices.append(notice)
        for evidence_id in card.evidence_ids:
            if evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)
            if len(evidence_ids) >= 3:
                break
    additions = [notice for notice in notices if notice not in facts_text]
    text = " ".join(
        notice if notice.endswith((".", "!", "?")) else f"{notice}."
        for notice in additions
    )
    if not text:
        return facts_text, {
            "appended": False,
            "text": "",
            "notice_count": len(notices),
            "evidence_ids": tuple(evidence_ids),
            "reason": "already_present_or_empty",
        }
    return f"{facts_text.rstrip()}\n\n{text}".strip(), {
        "appended": True,
        "text": text,
        "notice_count": len(additions),
        "evidence_ids": tuple(evidence_ids),
        "reason": "appended",
    }


_MISSING_PERIOD_ZERO_RE = re.compile(
    r"(?P<year>20\d{2})년\s*(?P<month>\d{1,2})월\s*"
    r"0(?:\.0+)?억원(?:에서\s*시작하여|에서)"
)


def remove_missing_period_zero_claims(
    text: str,
    fact_digest: FactDigest,
) -> tuple[str, dict[str, int]]:
    """Do not present an absent market period as an observed zero."""

    market_card = next(
        (
            card
            for card in fact_digest.cards
            if card.source == "mart" and card.card_type == "market"
        ),
        None,
    )
    if market_card is None:
        return text, {"missing_period_zero_removed": 0}
    observed_periods = {
        str(row.get("period"))
        for row in market_card.full_stats.get("series", ())
        if isinstance(row, Mapping) and row.get("period")
    }
    if not observed_periods:
        return text, {"missing_period_zero_removed": 0}
    first_period = min(observed_periods)

    def replacement(match: re.Match[str]) -> str:
        period = f"{int(match.group('year')):04d}-{int(match.group('month')):02d}"
        if period in observed_periods:
            return match.group(0)
        return f"데이터 시작 {first_period} 이후"

    updated, count = _MISSING_PERIOD_ZERO_RE.subn(replacement, text)
    return updated, {"missing_period_zero_removed": count}


def _append_hira_scope_notice_paragraph(
    manifest: dict[str, Any],
    surface: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach a deterministic HIRA scope notice to the paragraph manifest."""

    if surface.get("appended") is not True:
        return manifest
    text = str(surface.get("text") or "").strip()
    if not text:
        return manifest
    result = dict(manifest)
    section_paragraphs = {
        key: list(value)
        for key, value in dict(result.get("section_paragraphs") or {}).items()
    }
    facts = list(section_paragraphs.get("facts") or ())
    evidence_ids = tuple(str(value) for value in surface.get("evidence_ids") or ())
    if all(str(item.get("text") or "").strip() != text for item in facts):
        facts.append(
            {
                "text": text,
                "evidence": [
                    {
                        "evidence_id": evidence_id,
                        "label": f"출처: HIRA {evidence_id.removeprefix('hira:')}",
                    }
                    for evidence_id in evidence_ids
                ],
                "unsourced": not evidence_ids,
                "evidence_id_count": len(evidence_ids),
                "paragraph_start": True,
            }
        )
    section_paragraphs["facts"] = facts
    result["section_paragraphs"] = section_paragraphs
    referenced = set(result.get("referenced_evidence_ids") or ())
    referenced.update(evidence_ids)
    result["referenced_evidence_ids"] = sorted(referenced)
    paragraph_evidence = dict(result.get("paragraph_evidence") or {})
    paragraph_evidence["facts"] = {
        "total": len(facts),
        "sourced": sum(not bool(item.get("unsourced")) for item in facts),
        "unsourced": sum(bool(item.get("unsourced")) for item in facts),
    }
    result["paragraph_evidence"] = paragraph_evidence
    claim_evidence = dict(result.get("claim_evidence") or {})
    prior_claim_counts = dict(claim_evidence.get("facts") or {})
    claim_evidence["facts"] = {
        **paragraph_evidence["facts"],
        "over_limit": int(prior_claim_counts.get("over_limit") or 0),
    }
    result["claim_evidence"] = claim_evidence
    result["hira_scope_notice_surface"] = {
        "appended_paragraph_count": 1,
        "surface_synced": True,
    }
    return result


def _ensure_query_breakdown_sections(
    facts_text: str,
    insight_text: str,
    fact_digest: FactDigest,
) -> tuple[str, str, dict[str, Any]]:
    card = next(
        (
            item
            for item in fact_digest.cards
            if item.source == "clinicaltrials"
            and isinstance(item.full_stats.get("query_breakdown"), Mapping)
        ),
        None,
    )
    if card is None:
        return facts_text, insight_text, {"applied": False, "reason": "not_multi_query"}
    split = card.full_stats["query_breakdown"]
    by_query = split.get("by_query")
    global_stats = split.get("global")
    if not isinstance(by_query, Sequence) or len(by_query) < 2:
        return facts_text, insight_text, {"applied": False, "reason": "not_multi_query"}
    rows = [item for item in by_query if isinstance(item, Mapping) and item.get("query")]
    if len(rows) < 2:
        return facts_text, insight_text, {"applied": False, "reason": "not_multi_query"}
    details = ", ".join(
        f"{item['query']} 수신 {int(item.get('records_received') or 0)}건·직접 관련 "
        f"{int(item.get('records_direct_related') or 0)}건·고유 "
        f"{int(item.get('records_unique') or 0)}건"
        for item in rows
    )
    duplicates = (
        int(global_stats.get("cross_query_duplicates_removed") or 0)
        if isinstance(global_stats, Mapping)
        else 0
    )
    table_rows = "\n".join(
        "| "
        f"{item['query']} | {item.get('expansion_grade') or '미지정'} | "
        f"{int(item.get('records_received') or 0)} | "
        f"{int(item.get('records_direct_related') or 0)} | "
        f"{int(item.get('records_unique') or 0)} |"
        for item in rows
    )
    facts_sentence = (
        "질의별 임상시험 집계\n\n"
        "| 질의 | 확장 등급 | 수신 건수 | 직접 관련 건수 | 고유 건수 |\n"
        "|---|---|---:|---:|---:|\n"
        f"{table_rows}\n\n"
        f"질의 간 중복 {duplicates}건을 제거했습니다."
    )
    insight_sentence = (
        f"질의별 수신·직접 관련 분포({details})와 질의 간 중복 {duplicates}건 제거는 "
        "검색 표현별 임상 포착 범위를 비교하는 근거입니다."
    )
    facts_appended = facts_sentence not in facts_text
    insight_appended = insight_sentence not in insight_text
    if facts_appended:
        facts_text = f"{facts_text.rstrip()}\n\n{facts_sentence}".strip()
    if insight_appended:
        insight_text = f"{insight_text.rstrip()}\n\n{insight_sentence}".strip()
    return facts_text, insight_text, {
        "applied": True,
        "facts_text": facts_sentence,
        "insight_text": insight_sentence,
        "facts_appended": facts_appended,
        "insight_appended": insight_appended,
        "evidence_ids": tuple(dict.fromkeys(card.evidence_ids)),
        "evidence_ids_available": len(tuple(dict.fromkeys(card.evidence_ids))),
        "evidence_ids_omitted": 0,
        "query_count": len(rows),
        "cross_query_duplicates_removed": duplicates,
    }


def _append_query_breakdown_paragraphs(
    manifest: dict[str, Any],
    surface: Mapping[str, Any],
) -> dict[str, Any]:
    if surface.get("applied") is not True:
        return manifest
    result = dict(manifest)
    section_paragraphs = {
        key: list(value)
        for key, value in dict(result.get("section_paragraphs") or {}).items()
    }
    evidence_ids = tuple(str(value) for value in surface.get("evidence_ids") or ())
    evidence = [
        {
            "evidence_id": evidence_id,
            "label": f"출처: ClinicalTrials.gov {evidence_id.removeprefix('ct:')}",
        }
        for evidence_id in evidence_ids
    ]
    for section in ("facts", "insight"):
        text = str(surface.get(f"{section}_text") or "").strip()
        paragraphs = list(section_paragraphs.get(section) or ())
        if text and all(str(item.get("text") or "").strip() != text for item in paragraphs):
            paragraphs.append(
                {
                    "text": text,
                    "evidence": evidence,
                    "unsourced": not evidence,
                    "evidence_id_count": len(evidence),
                    "paragraph_start": True,
                }
            )
        section_paragraphs[section] = paragraphs
    result["section_paragraphs"] = section_paragraphs
    referenced = set(result.get("referenced_evidence_ids") or ())
    referenced.update(evidence_ids)
    result["referenced_evidence_ids"] = sorted(referenced)
    for metric_key in ("paragraph_evidence", "claim_evidence"):
        metrics = dict(result.get(metric_key) or {})
        for section in ("facts", "insight"):
            paragraphs = section_paragraphs[section]
            prior = dict(metrics.get(section) or {})
            metrics[section] = {
                "total": len(paragraphs),
                "sourced": sum(not bool(item.get("unsourced")) for item in paragraphs),
                "unsourced": sum(bool(item.get("unsourced")) for item in paragraphs),
                **({"over_limit": int(prior.get("over_limit") or 0)} if metric_key == "claim_evidence" else {}),
            }
        result[metric_key] = metrics
    return result


def _append_facts_floor_paragraphs(
    manifest: dict[str, Any],
    *,
    facts_fallback: str,
    deterministic_extension: str,
    floor_trace: dict[str, Any],
    final_facts_text: str = "",
    fact_digest: FactDigest | None = None,
) -> dict[str, Any]:
    """Keep floor additions on the paragraph-based SSE surface."""

    additions: list[tuple[str, list[dict[str, Any]]]] = []
    if floor_trace.get("core_fallback_appended"):
        additions.append((facts_fallback.strip(), []))
    if floor_trace.get("deterministic_extension_appended"):
        additions.append(
            (deterministic_extension.strip(), _facts_floor_evidence(fact_digest))
        )
    additions = [(text, evidence) for text, evidence in additions if text]
    existing_facts = list(
        dict(manifest.get("section_paragraphs") or {}).get("facts") or ()
    )
    if not existing_facts and final_facts_text.strip():
        additions.insert(0, (final_facts_text.strip(), []))
    if not additions:
        return manifest

    result = dict(manifest)
    section_paragraphs = {
        key: list(value)
        for key, value in dict(result.get("section_paragraphs") or {}).items()
    }
    facts = list(section_paragraphs.get("facts") or ())
    existing = {str(item.get("text") or "").strip() for item in facts}
    appended = 0
    for addition, evidence in additions:
        if any(addition in current for current in existing):
            continue
        facts.append(
            {
                "text": addition,
                "evidence": evidence,
                "unsourced": not evidence,
                "evidence_id_count": len(evidence),
                "paragraph_start": True,
            }
        )
        existing.add(addition)
        appended += 1
    section_paragraphs["facts"] = facts
    result["section_paragraphs"] = section_paragraphs

    paragraph_evidence = dict(result.get("paragraph_evidence") or {})
    paragraph_evidence["facts"] = {
        "total": len(facts),
        "sourced": sum(not bool(item.get("unsourced")) for item in facts),
        "unsourced": sum(bool(item.get("unsourced")) for item in facts),
    }
    result["paragraph_evidence"] = paragraph_evidence
    claim_evidence = dict(result.get("claim_evidence") or {})
    prior_claim_counts = dict(claim_evidence.get("facts") or {})
    claim_evidence["facts"] = {
        **paragraph_evidence["facts"],
        "over_limit": int(prior_claim_counts.get("over_limit") or 0),
    }
    result["claim_evidence"] = claim_evidence
    result["facts_floor_surface"] = {
        "appended_paragraph_count": appended,
        "surface_synced": appended == len(additions),
    }
    return result


def _facts_floor_evidence(fact_digest: FactDigest | None) -> list[dict[str, Any]]:
    if fact_digest is None:
        return []
    evidence: list[dict[str, Any]] = []
    seen: set[str] = set()
    for card in fact_digest.cards:
        if card.card_type != "file_aggregate" and card.source not in {
            "document",
            "document_sql",
            "hira",
        }:
            continue
        document = str(card.file_facts.get("document_name") or card.entity or "").strip()
        label = (
            f"출처: 업로드 문서 {document}"
            if card.source in {"document", "document_sql"}
            else f"출처: 건강보험심사평가원 {card.entity or ''}".strip()
        )
        for evidence_id in card.evidence_ids:
            if evidence_id in seen:
                continue
            seen.add(evidence_id)
            evidence.append(
                {
                    "evidence_id": evidence_id,
                    "label": label,
                    "source": card.source,
                }
            )
    return evidence


def _align_section_metric_values(
    facts_text: str,
    insight_text: str,
    *,
    parsed_facts: Any,
    parsed_insight: Any,
    fact_digest: FactDigest,
) -> tuple[str, dict[str, Any]]:
    """Use facts as authority when a shared dm claim has one divergent value."""

    if parsed_facts is None or parsed_insight is None:
        return insight_text, {
            "checked_metric_count": 0,
            "mismatch_count": 0,
            "correction_count": 0,
            "unresolved_mismatch_count": 0,
            "ambiguous_shared_claim_count": 0,
            "reason": "one_section_fallback",
        }
    metrics = {metric.id: metric for metric in fact_digest.derived_metrics}
    facts_ids = {identifier for claim in parsed_facts.claims for identifier in claim.evidence_ids}
    insight_ids = {identifier for claim in parsed_insight.claims for identifier in claim.evidence_ids}
    shared = sorted(set(metrics) & facts_ids & insight_ids)
    mismatch_count = 0
    correction_count = 0
    ambiguous_shared_claim_count = 0
    corrected = insight_text
    for claim in parsed_insight.claims:
        claim_metric_ids = [identifier for identifier in shared if identifier in claim.evidence_ids]
        authoritative = [
            identifier
            for identifier in claim_metric_ids
            if (expected := _decimal_value(metrics[identifier].value)) is not None
            and any(
                _contains_decimal(fact_claim.text, expected)
                for fact_claim in parsed_facts.claims
                if identifier in fact_claim.evidence_ids
            )
        ]
        if len(authoritative) != 1:
            ambiguous_shared_claim_count += int(len(authoritative) > 1)
            continue
        identifier = authoritative[0]
        expected = _decimal_value(metrics[identifier].value)
        if expected is None or _contains_decimal(claim.text, expected):
            continue
        numeric_tokens = _NUMBER_TOKEN_RE.findall(claim.text)
        if len(numeric_tokens) != 1:
            continue
        mismatch_count += 1
        replacement = _metric_surface_value(metrics[identifier].value)
        corrected_claim = claim.text.replace(numeric_tokens[0], replacement, 1)
        if claim.text in corrected:
            corrected = corrected.replace(claim.text, corrected_claim, 1)
            correction_count += 1
    return corrected, {
        "checked_metric_count": len(shared),
        "checked_metric_ids": shared,
        "mismatch_count": mismatch_count,
        "correction_count": correction_count,
        "unresolved_mismatch_count": mismatch_count - correction_count,
        "ambiguous_shared_claim_count": ambiguous_shared_claim_count,
        "authority": "facts",
    }


_PATENT_FACTS_POPULATION_PATTERNS = (
    re.compile(
        r"(?:제품\s*관련\s*특허|제품특허)(?:은|는|이|가)?\s*"
        r"(?P<count>\d[\d,]*)건"
    ),
    re.compile(r"(?P<count>\d[\d,]*)건의\s*제품\s*관련\s*특허"),
)
_PATENT_INSIGHT_POPULATION_PATTERNS = (
    *_PATENT_FACTS_POPULATION_PATTERNS,
    re.compile(r"직접\s*관련\s*(?P<count>\d[\d,]*)건(?:\s*기준)?"),
)
_CLINICAL_POPULATION_PATTERNS = (
    re.compile(
        r"직접\s*관련\s*임상(?:시험)?(?:은|는|이|가)?\s*"
        r"(?:총\s*)?(?P<count>\d[\d,]*)건"
    ),
    re.compile(
        r"(?P<count>\d[\d,]*)건의\s*(?:조합\s*)?직접\s*관련\s*임상(?:시험)?"
    ),
)


def _population_matches(
    text: str,
    patterns: tuple[re.Pattern[str], ...],
) -> list[re.Match[str]]:
    matches = [match for pattern in patterns for match in pattern.finditer(text)]
    return sorted(matches, key=lambda match: match.start("count"))


def align_section_population_counts(
    facts_text: str,
    insight_text: str,
    *,
    answer_type: str,
) -> tuple[str, str, dict[str, int | str]]:
    """Make insight population counts agree with facts at final assembly."""

    patterns = {
        "patent": (
            _PATENT_FACTS_POPULATION_PATTERNS,
            _PATENT_INSIGHT_POPULATION_PATTERNS,
        ),
        "clinical": (
            _CLINICAL_POPULATION_PATTERNS,
            _CLINICAL_POPULATION_PATTERNS,
        ),
    }.get(answer_type)
    counters: dict[str, int | str] = {
        "section_count_checked": 0,
        "section_count_mismatch": 0,
        "section_count_corrected": 0,
        "authority": "facts",
    }
    if patterns is None:
        return facts_text, insight_text, counters
    facts_patterns, insight_patterns = patterns
    facts_matches = _population_matches(facts_text, facts_patterns)
    insight_matches = _population_matches(insight_text, insight_patterns)
    if not facts_matches or not insight_matches:
        return facts_text, insight_text, counters
    authoritative = facts_matches[0].group("count")
    corrected = insight_text
    offset = 0
    for match in insight_matches:
        counters["section_count_checked"] = int(counters["section_count_checked"]) + 1
        if match.group("count") == authoritative:
            continue
        counters["section_count_mismatch"] = int(counters["section_count_mismatch"]) + 1
        start = match.start("count") + offset
        end = match.end("count") + offset
        corrected = f"{corrected[:start]}{authoritative}{corrected[end:]}"
        offset += len(authoritative) - (end - start)
        counters["section_count_corrected"] = int(counters["section_count_corrected"]) + 1
    return facts_text, corrected, counters


def _contains_decimal(text: str, expected: Decimal) -> bool:
    return any(
        value is not None and abs(value - expected) <= Decimal("0.01")
        for value in (_decimal_value(token) for token in _NUMBER_TOKEN_RE.findall(text))
    )


def _decimal_value(value: object) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "").rstrip("%"))
    except (InvalidOperation, ValueError):
        return None


def _metric_surface_value(value: object) -> str:
    decimal = _decimal_value(value)
    if decimal is None:
        return str(value)
    return f"{decimal.quantize(Decimal('0.01')):f}"


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def _without_zero_row_file_material(
    core_answer: str,
    fact_digest: FactDigest,
) -> tuple[str, FactDigest, dict[str, Any]]:
    removed = tuple(
        card
        for card in fact_digest.cards
        if card.card_type == "file_aggregate"
        and card.file_facts.get("analytics_operation")
        in {"brand_monthly", "brand_monthly_yoy"}
        and not card.file_facts.get("analytics_rows")
        and any(
            marker in str(card.file_facts.get("analytics_answer") or "")
            for marker in ("찾지 못", "데이터가 없습니다")
        )
    )
    if not removed:
        return core_answer, fact_digest, {"removed_card_count": 0}

    removed_ids = {id(card) for card in removed}
    cards = tuple(card for card in fact_digest.cards if id(card) not in removed_ids)
    has_document_card = any(card.source == "document" for card in cards)
    received = dict(fact_digest.source_received_counts)
    visible = dict(fact_digest.source_visible_counts)
    if not has_document_card:
        received["document"] = 0
        visible["document"] = 0
    cleaned_core = _ZERO_ROW_FILE_ANSWER_RE.sub("", core_answer)
    cleaned_core = "\n".join(
        line.rstrip() for line in cleaned_core.splitlines() if line.strip()
    )
    return (
        cleaned_core,
        fact_digest.model_copy(
            update={
                "cards": cards,
                "source_received_counts": received,
                "source_visible_counts": visible,
            }
        ),
        {
            "removed_card_count": len(removed),
            "reason": "zero_row_file_query_not_observation_material",
            "remaining_card_count": len(cards),
        },
    )


def _filter_relevant_material(
    original_digest: FactDigest,
    prompt_digest: FactDigest,
    *,
    zero_row_trace: dict[str, Any],
) -> tuple[FactDigest, dict[str, Any]]:
    matching_terms = _material_matching_terms(original_digest)
    is_file_question = original_digest.answer_type == "file_aggregate" or any(
        card.card_type == "file_aggregate" for card in original_digest.cards
    )
    required_sources = frozenset(
        original_digest.answer_contract.required_sources
        if original_digest.answer_contract is not None
        else ()
    )
    decisions = tuple(
        (
            card,
            _card_material_reason(
                card,
                matching_terms=matching_terms,
                answer_type=original_digest.answer_type,
                required_sources=required_sources,
                is_file_question=is_file_question,
            ),
        )
        for card in prompt_digest.cards
    )
    kept_cards = tuple(card for card, reason in decisions if reason != "entity_mismatch")
    removed_cards = tuple(card for card, reason in decisions if reason == "entity_mismatch")
    kept_evidence_ids = {
        evidence_id for card in kept_cards for evidence_id in card.evidence_ids
    }
    kept_entities = {
        " ".join(str(card.entity or "").casefold().split())
        for card in kept_cards
        if str(card.entity or "").strip()
    }
    kept_metrics = tuple(
        metric
        for metric in prompt_digest.derived_metrics
        if set(metric.inputs).issubset(kept_evidence_ids)
        or " ".join(str(metric.entity or "").casefold().split()) in kept_entities
    )
    removed_sources = sorted({card.source for card in removed_cards})
    visible = dict(prompt_digest.source_visible_counts)
    for source in removed_sources:
        if not any(card.source == source for card in kept_cards):
            visible[source] = 0
    lane_sources = tuple(
        dict.fromkeys(
            (
                *prompt_digest.source_received_counts.keys(),
                *(card.source for card in prompt_digest.cards),
            )
        )
    )
    lanes: dict[str, dict[str, Any]] = {}
    for source in lane_sources:
        supplied = tuple(
            (card, reason)
            for card, reason in decisions
            if card.source == source and reason != "entity_mismatch"
        )
        excluded = tuple(
            (card, reason)
            for card, reason in decisions
            if card.source == source and reason == "entity_mismatch"
        )
        received_count = int(prompt_digest.source_received_counts.get(source, 0) or 0)
        result_exclusion_counts: Counter[str] = Counter()
        for card, _reason in (*supplied, *excluded):
            result_exclusion_counts.update(
                {
                    str(reason): int(count)
                    for reason, count in dict(
                        card.full_stats.get("relevance_excluded_reasons") or {}
                    ).items()
                }
            )
        lanes[source] = {
            "status": "supplied" if supplied else "excluded",
            "reason": (
                ",".join(dict.fromkeys(reason for _card, reason in supplied))
                if supplied
                else "no_matching_card"
                if received_count
                else "no_received_data"
            ),
            "received_count": received_count,
            "supplied_card_count": len(supplied),
            "excluded_card_count": len(excluded),
            "excluded_reasons": list(
                dict.fromkeys(reason for _card, reason in excluded)
            ),
            "result_relevance_excluded_count": sum(result_exclusion_counts.values()),
            "result_relevance_excluded_reasons": dict(result_exclusion_counts),
        }
    return (
        prompt_digest.model_copy(
            update={
                "cards": kept_cards,
                "derived_metrics": kept_metrics,
                "source_visible_counts": visible,
            }
        ),
        {
            "applied": True,
            "reason": (
                "unresolved_file_entity_excludes_unrelated_lane_material"
                if zero_row_trace.get("removed_card_count")
                else "file_question_excludes_unrelated_lane_material"
            ),
            "removed_sources": removed_sources,
            "removed_card_count": len(removed_cards),
            "original_card_count": len(original_digest.cards),
            "retained_card_count": len(kept_cards),
            "matching_terms": list(matching_terms),
            "lanes": lanes,
        },
    )


def _relevance_safe_facts_fallback(
    facts_fallback: str,
    filtered_digest: FactDigest,
    relevance_trace: Mapping[str, Any],
    *,
    preserve_existing: bool = False,
) -> tuple[str, dict[str, Any]]:
    removed_count = int(relevance_trace.get("removed_card_count") or 0)
    removed_sources = list(relevance_trace.get("removed_sources") or ())
    if removed_count <= 0:
        return facts_fallback, {
            "applied": False,
            "reason": "no_material_removed",
            "removed_card_count": 0,
            "removed_sources": removed_sources,
        }
    if preserve_existing:
        return facts_fallback, {
            "applied": False,
            "reason": "zero_row_file_fallback_is_question_scoped",
            "removed_card_count": removed_count,
            "removed_sources": removed_sources,
        }
    fallback_received_counts = dict(filtered_digest.source_received_counts)
    for source in removed_sources:
        fallback_received_counts[source] = 0
    fallback_digest = filtered_digest.model_copy(
        update={"source_received_counts": fallback_received_counts}
    )
    filtered_fallback = _section_body(render_core_answer(fallback_digest))
    if not filtered_fallback:
        filtered_fallback = _section_body(build_l1_insight(fallback_digest).text)
    return filtered_fallback, {
        "applied": True,
        "reason": "filtered_material_replaces_prefilter_core",
        "removed_card_count": removed_count,
        "removed_sources": removed_sources,
    }


def _material_matching_terms(digest: FactDigest) -> tuple[str, ...]:
    values: list[str] = [digest.question]
    if digest.answer_contract is not None:
        values.extend(digest.answer_contract.resolved_entities)
        values.extend(digest.answer_contract.required_entities)
    terms: list[str] = []
    for value in values:
        normalized = _normalize_material_text(value)
        if len(normalized) >= 2:
            terms.append(normalized)
        terms.extend(
            token
            for token in re.findall(r"[0-9a-z가-힣]{2,}", str(value).casefold())
            if token not in {"최근", "기준", "시장", "매출", "추이", "알려줘"}
        )
    return tuple(dict.fromkeys(terms))


def _normalize_material_text(value: object) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", str(value or "").casefold())


def _card_material_reason(
    card: Any,
    *,
    matching_terms: tuple[str, ...],
    answer_type: str,
    required_sources: frozenset[str],
    is_file_question: bool,
) -> str:
    if card.source == "document":
        return "active_document"
    if (
        is_file_question
        and card.source in {"web", "web_news", "tavily"}
        and card.source not in required_sources
    ):
        return "entity_mismatch"
    if card.source == "web" and card.received_count > 0 and card.matched_count == 0:
        return "entity_mismatch"
    if card.source in required_sources:
        return "required_source"
    if card.card_type == answer_type and answer_type not in {"general", "mixed"}:
        return "answer_type_match"
    if card.source == "openfda" and card.full_stats.get("source_queries"):
        return "source_query_entity_match"
    if _card_matches_material(card, matching_terms):
        return "entity_match"
    if (
        not is_file_question
        and answer_type != "disease"
        and not str(card.entity or "").strip()
    ):
        return "received_source_without_entity"
    return "entity_mismatch"


def _card_matches_material(card: Any, matching_terms: tuple[str, ...]) -> bool:
    entity = _normalize_material_text(card.entity)
    if entity and any(entity in term or term in entity for term in matching_terms):
        return True
    return any(
        any(
            normalized_query in term or term in normalized_query
            for term in matching_terms
        )
        for query in card.full_stats.get("source_queries", ())
        if len(normalized_query := _normalize_material_text(query)) >= 2
    )


def _zero_row_file_facts(fact_digest: FactDigest) -> str:
    for card in fact_digest.cards:
        answer = str(card.file_facts.get("analytics_answer") or "").strip()
        if (
            card.card_type == "file_aggregate"
            and card.file_facts.get("analytics_operation")
            in {"brand_monthly", "brand_monthly_yoy"}
            and not card.file_facts.get("analytics_rows")
            and any(marker in answer for marker in ("찾지 못", "데이터가 없습니다"))
        ):
            return answer
    return ""


def _source_availability(
    digest: FactDigest,
    *,
    source_context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if source_context is None:
        return {
            "state": "normal",
            "enforced": False,
            "reason": "source_context_not_supplied",
            "required_sources": [],
            "valid_sources": [],
            "source_status": [],
            "hypothesis_cap": -1,
            "facts_floor_waived": False,
        }

    file_question = digest.answer_type in {"file_aggregate", "document_summary"} or any(
        marker in digest.question.casefold()
        for marker in ("파일", "문서", "업로드", "엑셀")
    )
    contract_sources = (
        digest.answer_contract.required_sources
        if digest.answer_contract is not None
        else ()
    )
    required_sources = (
        {"document"}
        if file_question
        else {_source_family(source) for source in contract_sources}
    )
    guidance_only = bool(source_context.get("guidance_only_filter_applied"))
    valid_sources = {
        _source_family(card.source)
        for card in digest.cards
        if _card_has_answer_material(card)
        and not (guidance_only and _source_family(card.source) == "document")
    }
    web_valid = "web" in valid_sources
    required_valid = bool(required_sources & valid_sources)
    if web_valid and not (valid_sources - {"web"}):
        state = "web_only"
    elif required_valid or (not required_sources and valid_sources - {"web"}):
        state = "normal"
    else:
        state = "none"

    lanes = source_context.get("lanes")
    lane_items = lanes.items() if isinstance(lanes, Mapping) else ()
    source_status = [
        _source_status_row(str(source), value)
        for source, value in lane_items
        if isinstance(value, Mapping)
    ]
    return {
        "state": state,
        "enforced": True,
        "reason": (
            "required_source_available"
            if state == "normal"
            else "only_relevant_web_available"
            if state == "web_only"
            else "no_required_or_web_material"
        ),
        "question_type": "file" if file_question else digest.answer_type,
        "required_sources": sorted(required_sources),
        "valid_sources": sorted(valid_sources),
        "guidance_only_filter_applied": guidance_only,
        "source_status": source_status,
        "hypothesis_cap": 3 if state == "web_only" else 0 if state == "none" else -1,
        "facts_floor_waived": state == "none",
    }


def _source_status_row(source: str, value: Mapping[str, Any]) -> dict[str, Any]:
    queries: list[dict[str, Any]] = []
    raw_queries = value.get("queries")
    if isinstance(raw_queries, Sequence) and not isinstance(raw_queries, (str, bytes)):
        for raw_query in raw_queries:
            if not isinstance(raw_query, Mapping):
                continue
            queries.append(
                {
                    "query": str(raw_query.get("query") or ""),
                    "status": str(raw_query.get("status") or ""),
                    "returned_count": int(raw_query.get("returned_count") or 0),
                    "reason_code": raw_query.get("reason_code"),
                }
            )
    return {
        "source": source,
        "state": str(value.get("state") or "unknown"),
        "returned_count": int(value.get("returned_count") or 0),
        "reason_code": value.get("reason_code"),
        "queries": queries,
    }


def _source_family(source: object) -> str:
    normalized = str(source or "").casefold()
    if normalized in {"document", "document_rag", "document_sql", "file_sql"}:
        return "document"
    if normalized in {"web", "web_news", "tavily"}:
        return "web"
    if normalized in {"clinical", "clinicaltrials"}:
        return "clinicaltrials"
    return normalized


def _card_has_answer_material(card: Any) -> bool:
    if card.card_type == "file_aggregate":
        query_result = card.file_facts.get("query_result")
        if isinstance(query_result, Mapping):
            rows = query_result.get("rows")
            if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
                if rows:
                    return True
                if card.metric == "분석 차원 안내":
                    return False
        analytics_rows = card.file_facts.get("analytics_rows")
        if isinstance(analytics_rows, Sequence) and not isinstance(
            analytics_rows, (str, bytes)
        ):
            return bool(analytics_rows)
    return bool(
        card.matched_count
        or card.visible_count
        or card.evidence_ids
        or card.visible_rows
        or card.received_count
    )


def _strip_required_source_gap(text: str) -> str:
    return re.sub(
        r"필수 원천인 .+?의 결과를 수신하지 못해 질문의 직접 답을 "
        r"확정하지 못했습니다\.\s*",
        "",
        text,
        count=1,
    ).strip()


def _no_source_facts(digest: FactDigest, availability: Mapping[str, Any]) -> str:
    parts = ["보유 원천에서 관련 데이터를 찾지 못해 답변드릴 수 없습니다."]
    source_status = availability.get("source_status")
    if isinstance(source_status, Sequence):
        queried = []
        for row in source_status:
            if not isinstance(row, Mapping):
                continue
            queries = row.get("queries")
            query_text = ", ".join(
                str(query.get("query") or "")
                for query in queries or ()
                if isinstance(query, Mapping) and query.get("query")
            )
            if query_text or str(row.get("state") or "") != "unplanned":
                status = _absence_status_text(row)
                queried.append(
                    f"{row.get('source')}: {status}"
                    + (f" (질의어: {query_text})" if query_text else "")
                )
        if queried:
            parts.append("조회 결과: " + " · ".join(queried) + ".")
    guidance = next(
        (
            str(card.file_facts.get("analytics_answer") or "").strip()
            for card in digest.cards
            if card.card_type == "file_aggregate"
            and card.metric == "분석 차원 안내"
        ),
        "",
    )
    if guidance:
        parts.append(guidance)
    else:
        parts.append("대상·기간·분석 축을 바꾸거나 보유 원천을 추가해 다시 질문해 주세요.")
    return " ".join(parts)


def _absence_status_text(row: Mapping[str, Any]) -> str:
    state = str(row.get("state") or "").casefold()
    reason = str(row.get("reason_code") or "").casefold()
    query_tokens = {
        str(value).casefold()
        for query in row.get("queries") or ()
        if isinstance(query, Mapping)
        for value in (query.get("status"), query.get("reason_code"))
        if value
    }
    tokens = {state, reason, *query_tokens}
    if any("timeout" in token or "deadline" in token for token in tokens):
        return "타임아웃"
    if any(
        marker in token
        for token in tokens
        for marker in ("fail", "error", "exception", "quota")
    ):
        suffix = f"({row.get('reason_code')})" if row.get("reason_code") else ""
        return f"조회 실패{suffix}"
    if state == "unplanned":
        return "미조회"
    if "not_executed" in state or "no_document" in state:
        return "조회 미실행"
    returned_count = int(row.get("returned_count") or 0)
    return f"결과 {returned_count}건"


def _absence_insight_gate(text: str, *, state: str) -> dict[str, Any]:
    mode = os.environ.get("BIND3_ABSENCE_INSIGHT_MODE", "shadow").strip().casefold()
    mode = "enforce" if mode == "enforce" else "shadow"
    sentences = [
        match.group(0).strip()
        for match in _ABSENCE_SENTENCE_RE.finditer(text)
        if match.group(0).strip()
    ]
    concrete_values = [len(_CONCRETE_VALUE_RE.findall(sentence)) for sentence in sentences]
    would_block = state == "none" and sum(concrete_values) < len(sentences)
    return {
        "mode": mode,
        "candidate_sentence_count": len(sentences),
        "concrete_values_per_sentence": concrete_values,
        "concrete_value_total": sum(concrete_values),
        "would_block": would_block,
        "blocked": would_block and mode == "enforce",
    }


def _deterministic_paragraph(
    text: str,
    evidence_ids: Sequence[str],
) -> dict[str, Any]:
    identifiers = list(dict.fromkeys(str(value) for value in evidence_ids if value))
    evidence = [{"evidence_id": identifier} for identifier in identifiers]
    return {
        "text": text,
        "evidence_ids": identifiers,
        "evidence": evidence,
        "unsourced": not identifiers,
        "evidence_id_count": len(identifiers),
        "source_groups": [],
    }


def _section_body(text: str) -> str:
    lines = (
        line.strip()
        for line in text.strip().splitlines()
        if line.strip() not in {"## 핵심 답", "## 종합 인사이트", "## 조사 결과"}
    )
    return "\n".join(line for line in lines if line)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _finish_trace(trace: dict[str, Any], started: float) -> dict[str, Any]:
    finished = time.monotonic()
    return {
        **trace,
        "finished_monotonic": finished,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_ms": (finished - started) * 1000,
    }


__all__ = ["InsightLane", "InsightLaneOutcome"]
