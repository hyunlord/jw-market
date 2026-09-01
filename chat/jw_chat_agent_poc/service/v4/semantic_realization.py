from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict

from jw_chat_agent_poc.service.v4.fact_digest import is_document_summary_request
from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceSet
from jw_chat_agent_poc.service.v4.surface_binding import prune_empty_surface_sections

DowngradeAction = Literal["retain", "delete"]
_UNPROVIDED_CELL = "원천 미제공"
_YEAR_MONTH_RE = re.compile(
    r"(?P<year>20\d{2})(?:\s*년\s*|[-./])(?P<month>\d{1,2})(?:\s*월)?"
)
_BODY_METADATA_HEADING_RE = re.compile(
    r"^#{2,6}\s+(?:조회 제한|조사 범위와 완전성|출처(?:별\s+조회\s+결과)?)\s*$"
)
_BODY_NOTICE_TEXTS = (
    "특허 존속기간 만료가 곧 제네릭 진입 시점을 뜻하지 않습니다.",
    "ClinicalTrials.gov 모집상태는 갱신이 지연될 수 있습니다.",
    "HIRA 환자수는 주상병 기준 청구 실인원이며 유병률과 다릅니다.",
)
_BODY_FOOTNOTE_RE = re.compile(
    r"^(?:[-*]\s*)?(?:"
    r"원천 미제공\s+\d+행은 표에서 제외했습니다\.?|"
    r"품목\s+\d+건(?:\s+.*)?|"
    r"전체\s+\d+건\s+중\s+\d+건\s+표시(?:\s+.*)?|"
    r"표시 정렬:.*|"
    r"정렬 기준\s+per_source_.*"
    r")$"
)
_INTERNAL_LABELED_CONTENT_RE = re.compile(
    r"(?:\*\*)?\[(?:L[123]\s+)?(?:사실|해석|융합)\s*:\s*(?P<content>[^\]]+)\](?:\*\*)?"
)
_INTERNAL_INSIGHT_LABEL_RE = re.compile(
    r"(?:\*\*)?\[(?:L[123](?:\s+(?:사실|해석|융합))?|해석|융합)\](?:\*\*)?\s*"
)


class PredicateDowngrade(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    original_predicate_id: str
    predicate_id: str | None
    action: DowngradeAction
    causal_level: Literal["NONE", "TEMPORAL", "ASSOCIATION"]
    reason_code: str


class SemanticSurfaceResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    transformations: tuple[dict[str, str], ...] = ()
    downgrade_count: int = 0
    deletion_count: int = 0
    removed_empty_headings: int = 0


class CoreAnswerIntegrityResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    status: Literal[
        "present",
        "card_generated",
        "deterministic_fallback",
        "explicit_absence",
    ]
    reason: str | None = None


class SemanticEvidenceContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    has_temporal_support: bool
    supported_text: str
    temporal_support_texts: tuple[str, ...] = ()
    observed_count: int
    requested_count: int
    protected_line_sha256: tuple[str, ...] = ()
    has_hira_patient_count: bool = False
    hira_code_count: int = 0


def strip_s17_body_metadata(answer: str) -> tuple[str, dict[str, int]]:
    """Remove inspection-only prose while retaining every unique body table."""
    kept: list[str] = []
    skip_section = False
    in_core_section = False
    removed_sections = 0
    removed_notice_lines = 0
    removed_core_subheadings = 0
    removed_internal_labels = 0
    lines = answer.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped == "## 핵심 답":
            in_core_section = True
        elif stripped.startswith("## "):
            in_core_section = False
        if _BODY_METADATA_HEADING_RE.fullmatch(stripped):
            skip_section = True
            removed_sections += 1
            index += 1
            continue
        if skip_section:
            if stripped.startswith("## "):
                skip_section = False
                continue
            index += 1
            continue
        if in_core_section and re.match(r"^#{3,6}\s+", stripped):
            removed_core_subheadings += 1
            index += 1
            continue
        if stripped.startswith("[확인 한계]") or stripped in _BODY_NOTICE_TEXTS:
            removed_notice_lines += 1
            index += 1
            continue
        if _BODY_FOOTNOTE_RE.fullmatch(stripped):
            removed_notice_lines += 1
            index += 1
            continue
        line, labeled_content_count = _INTERNAL_LABELED_CONTENT_RE.subn(
            lambda match: match.group("content"),
            line,
        )
        line, label_count = _INTERNAL_INSIGHT_LABEL_RE.subn("", line)
        removed_internal_labels += labeled_content_count + label_count
        kept.append(line)
        index += 1

    deduplicated: list[str] = []
    seen_tables: set[str] = set()
    duplicate_tables_removed = 0
    index = 0
    while index < len(kept):
        if not kept[index].strip().startswith("|"):
            deduplicated.append(kept[index])
            index += 1
            continue
        table_lines: list[str] = []
        while index < len(kept) and kept[index].strip().startswith("|"):
            table_lines.append(kept[index].rstrip())
            index += 1
        signature = "\n".join(line.strip() for line in table_lines)
        if signature in seen_tables:
            duplicate_tables_removed += 1
            continue
        seen_tables.add(signature)
        deduplicated.extend(table_lines)

    compacted: list[str] = []
    for line in deduplicated:
        if not line.strip() and (not compacted or not compacted[-1].strip()):
            continue
        compacted.append(line)
    cleaned, removed_empty_headings = prune_empty_surface_sections(
        "\n".join(compacted).strip()
    )
    return cleaned, {
        "removed_sections": removed_sections,
        "removed_notice_lines": removed_notice_lines,
        "removed_core_subheadings": removed_core_subheadings,
        "removed_internal_labels": removed_internal_labels,
        "duplicate_tables_removed": duplicate_tables_removed,
        "removed_empty_headings": removed_empty_headings,
    }


_CAUSE_SENTENCE_PATTERNS = (
    re.compile(
        r"^(?P<left>.+?)(?:이|가)\s+(?P<right>.+?)(?:을|를)\s+"
        r"(?:일으켰|야기했)(?:습니다|다)\.?$"
    ),
    re.compile(
        r"^(?P<left>.+?)(?:은|는)\s+(?P<right>.+?)\s+"
        r"때문(?:입니다|이다)\.?$"
    ),
    re.compile(
        r"^(?P<left>.+?)\s+때문에\s+(?P<right>.+?)(?:입니다|이다|습니다|다)\.?$"
    ),
    re.compile(
        r"^(?P<left>.+?)의\s+원인은\s+(?P<right>.+?)(?:입니다|이다)\.?$"
    ),
    re.compile(
        r"^(?P<left>.+?)(?:이|가)\s+(?P<right>.+?)(?:에|에게)\s+"
        r"영향을\s*(?:줬|주었|미쳤)(?:습니다|다)\.?$"
    ),
    re.compile(
        r"^(?P<left>.+?)\s+(?:causes?|caused)\s+(?P<right>.+?)\.?$",
        re.IGNORECASE,
    ),
)
_CAUSAL_ASSERTION_RE = re.compile(
    r"(?:때문|원인|기인|야기|일으(?:키|켰)|영향을|"
    r"\bcaus(?:e(?:s|d)?|ing|al(?:ity|ly)?|ation|ative)\b)",
    re.IGNORECASE,
)
_TREND_RE = re.compile(
    r"(?:전망됩니다|예상됩니다|것으로\s*(?:전망|예상)|다가온다|"
    r"증가할\s*것|감소할\s*것|확대될\s*것|축소될\s*것)"
)
_GLOBAL_ABSENCE_RE = re.compile(r"(?:전\s*세계|모든|전체).{0,40}(?:없습니다|존재하지\s*않)")
_DATE_VALUE_RE = re.compile(
    r"^20\d{2}(?:-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12]\d|3[01]))?)?$"
)
_TABLE_SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")
_CAUSAL_LIMIT_TEXT = "[확인 한계] 인과 관계는 이 조회로 확정하지 않습니다."
_TREND_LIMIT_TEXT = "[확인 한계] 전망은 이 조회로 확정하지 않습니다."
_HIRA_RATE_OR_RISK_RE = re.compile(r"(?:발생\s*위험|발생률|유병률)")
_HIRA_LIMITATION_RE = re.compile(
    r"(?:아니|다르|판단하지|말할\s*수\s*없|확인되지|제시하지|산출하지)"
)
_HIRA_CARE_PATHWAY_RE = re.compile(
    r"(?=.*외래)(?=.*(?:만성|질환을\s*관리))"
    r"(?=.*(?:주로|대부분|보여|시사|해석|명확|뚜렷)).+"
)
_HIRA_RATE_LIMIT_TEXT = (
    "[확인 한계] 이 자료는 주상병 기준 청구 실인원이며, 인구 분모가 없어 "
    "성별·연령별 발생 위험이나 유병률을 판단하지 않습니다."
)
_HIRA_CARE_LIMIT_TEXT = (
    "[확인 한계] 외래·입원 실인원만으로 진료 방식이나 만성 관리 여부를 "
    "판단하지 않습니다."
)
_HIRA_SUM_LIMIT_TEXT = (
    "[확인 한계] 제시된 값은 코드별 실인원이며, 코드 간 중복 제거 여부가 "
    "확인되지 않아 합산한 총계는 제시하지 않습니다."
)


def downgrade_predicate(
    predicate_id: str,
    context: SemanticEvidenceContext,
) -> PredicateDowngrade:
    if predicate_id == "CAUSES" and context.has_temporal_support:
        return _retained(predicate_id, "TEMPORALLY_ASSOCIATED", "ASSOCIATION")
    if predicate_id == "GLOBAL_ABSENCE":
        return _retained(predicate_id, "NOT_FOUND_IN_THIS_QUERY", "NONE")
    if (
        predicate_id == "COMPLETE_COMPARISON"
        and context.observed_count < context.requested_count
    ):
        return _retained(predicate_id, "PARTIAL_SUBSET_COMPARISON", "NONE")
    return PredicateDowngrade(
        original_predicate_id=predicate_id,
        predicate_id=None,
        action="delete",
        causal_level="NONE",
        reason_code="predicate_has_no_supported_downgrade",
    )


def realize_semantic_surface(
    answer: str,
    context: SemanticEvidenceContext,
) -> SemanticSurfaceResult:
    output: list[str] = []
    transformations: list[dict[str, str]] = []
    downgrade_count = 0
    deletion_count = 0
    hira_rate_blocked = False
    hira_care_blocked = False
    seen_limitations: set[str] = set()
    retrieval_limits: list[str] = []
    for line in answer.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("```", "~~~")):
            output.append(line)
            continue
        if stripped.startswith("|"):
            updated, table_transformations, table_downgrades, table_deletions = (
                _sanitize_table_line(line, context)
            )
            output.append(updated)
            transformations.extend(table_transformations)
            downgrade_count += table_downgrades
            deletion_count += table_deletions
            table_reasons = {
                transformation.get("reason") for transformation in table_transformations
            }
            if "patient_count_has_no_population_denominator" in table_reasons:
                hira_rate_blocked = True
            if "patient_counts_do_not_establish_care_pathway" in table_reasons:
                hira_care_blocked = True
            if "unsupported" in table_reasons:
                retrieval_limits.append(
                    "- TREND_UNSUPPORTED: 확인된 기간·관계 근거가 없어 전망 서술을 제외함."
                )
            if "unsupported_table_claim" in table_reasons:
                retrieval_limits.append(
                    "- CAUSAL_UNSUPPORTED: 인과 근거가 없어 원인·효과 서술을 제외함."
                )
            continue
        if sha256(stripped.encode("utf-8")).hexdigest() in context.protected_line_sha256:
            output.append(line)
            continue
        limitation = re.sub(r"^[-*]\s+", "", stripped)
        if limitation.startswith("[확인 한계]"):
            if limitation in seen_limitations:
                transformations.append(
                    {
                        "from": "DUPLICATE_LIMITATION",
                        "to": "DELETE",
                        "reason": "identical_limitation_already_present",
                    }
                )
                deletion_count += 1
                continue
            seen_limitations.add(limitation)
            retrieval_limits.append(
                f"- GENERAL_EVIDENCE_LIMIT: {limitation.removeprefix('[확인 한계]').strip()}"
            )
            continue
        if re.match(r"^\s*(?:[-*]\s+)?또한,\s*", line):
            line = re.sub(r"^(\s*(?:[-*]\s+)?)또한,\s*", r"\1", line, count=1)
            stripped = line.strip()
            transformations.append(
                {
                    "from": "LEADING_CONNECTOR",
                    "to": "DIRECT_SENTENCE",
                    "reason": "paragraph_has_no_preceding_sentence",
                }
            )
        policy_text, markdown_prefix = _policy_text(stripped)
        if context.has_hira_patient_count and _unsupported_hira_rate_claim(policy_text):
            retained_sentences, removed_sentences = _remove_unsupported_hira_rate_sentences(
                policy_text
            )
            transformations.extend(
                {
                    "from": "HIRA_RATE_OR_RISK",
                    "to": "DELETE",
                    "reason": "patient_count_has_no_population_denominator",
                }
                for _sentence in removed_sentences
            )
            deletion_count += len(removed_sentences)
            hira_rate_blocked = True
            if not retained_sentences:
                continue
            indent = line[: len(line) - len(line.lstrip())]
            line = f"{indent}{markdown_prefix}{' '.join(retained_sentences)}"
            stripped = line.strip()
            policy_text, markdown_prefix = _policy_text(stripped)
        if context.has_hira_patient_count and _HIRA_CARE_PATHWAY_RE.search(policy_text):
            transformations.append(
                {
                    "from": "HIRA_CARE_PATHWAY_INTERPRETATION",
                    "to": "DELETE",
                    "reason": "patient_counts_do_not_establish_care_pathway",
                }
            )
            deletion_count += 1
            hira_care_blocked = True
            continue
        if _TREND_RE.search(policy_text):
            transformations.append(
                {"from": "TREND_PREDICTION", "to": "DELETE", "reason": "unsupported"}
            )
            deletion_count += 1
            continue
        causal_clauses = _causal_clauses(policy_text)
        if causal_clauses is not None or _CAUSAL_ASSERTION_RE.search(policy_text):
            left, right = causal_clauses or ("", "")
            clauses_bound = _clauses_temporally_bound(left, right, context)
            decision = downgrade_predicate(
                "CAUSES",
                context.model_copy(
                    update={
                        "has_temporal_support": (
                            context.has_temporal_support and clauses_bound
                        )
                    }
                ),
            )
            if decision.action == "delete":
                deletion_count += 1
                transformations.append(
                    {"from": "CAUSES", "to": "DELETE", "reason": decision.reason_code}
                )
                continue
            indent = line[: len(line) - len(line.lstrip())]
            output.append(
                f"{indent}{markdown_prefix}[관찰적 연결] {left}와 "
                f"{right}는 시간상 함께 관찰되었습니다."
            )
            transformations.append(
                {
                    "from": "CAUSES",
                    "to": str(decision.predicate_id),
                    "reason": decision.reason_code,
                }
            )
            downgrade_count += 1
            continue
        updated = line
        if context.observed_count < context.requested_count and re.search(
            r"(?:모든|전체)\s*브랜드(?:를|가|는|의)?\s*(?:완전하게\s*)?비교",
            policy_text,
        ):
            updated = re.sub(r"(?:모든|전체)\s*브랜드", "확인된 일부 브랜드", line)
            transformations.append(
                {
                    "from": "COMPLETE_COMPARISON",
                    "to": "PARTIAL_SUBSET_COMPARISON",
                    "reason": "partial_entity_snapshot",
                }
            )
            downgrade_count += 1
        if _GLOBAL_ABSENCE_RE.search(updated):
            updated = re.sub(
                r"(?:전\s*세계|모든|전체)",
                "이번 조회 범위에서",
                updated,
                count=1,
            )
            transformations.append(
                {
                    "from": "GLOBAL_ABSENCE",
                    "to": "NOT_FOUND_IN_THIS_QUERY",
                    "reason": "query_scoped_evidence",
                }
            )
            downgrade_count += 1
        output.append(updated)
    if hira_rate_blocked:
        retrieval_limits.append(
            "- HIRA_MISSING_DENOMINATOR: 환자수 표면에 인구 분모가 없어 "
            "유병률·발생위험을 산출하지 않음."
        )
    if hira_care_blocked:
        retrieval_limits.append(
            "- HIRA_CARE_PATHWAY_UNSUPPORTED: 외래·입원 실인원만으로 "
            "진료 방식·만성 관리 여부를 확정하지 않음."
        )
    if context.has_hira_patient_count and context.hira_code_count >= 2:
        retrieval_limits.append(
            "- HIRA_CROSS_CODE_DEDUP_UNKNOWN: 코드 간 중복 제거 여부가 "
            "확인되지 않아 코드별 실인원을 합산하지 않음."
        )
        transformations.append(
            {
                "from": "HIRA_CODE_SUM",
                "to": "NOT_REPORTED",
                "reason": "cross_code_deduplication_unknown",
            }
        )
    output = _merge_retrieval_limit_lines(output, retrieval_limits)
    surface_text, removed_empty_headings = prune_empty_surface_sections("\n".join(output))
    return SemanticSurfaceResult(
        text=surface_text,
        transformations=tuple(transformations),
        downgrade_count=downgrade_count,
        deletion_count=deletion_count,
        removed_empty_headings=removed_empty_headings,
    )


def _merge_retrieval_limit_lines(
    output: Sequence[str],
    retrieval_limits: Sequence[str],
) -> list[str]:
    limits = tuple(dict.fromkeys(line for line in retrieval_limits if line.strip()))
    if not limits:
        return list(output)
    merged = list(output)
    heading_index = next(
        (
            index
            for index, line in enumerate(merged)
            if line.strip() == "## 조회 제한"
        ),
        None,
    )
    if heading_index is None:
        if merged and merged[-1].strip():
            merged.append("")
        merged.extend(("## 조회 제한", *limits))
        return merged
    insert_at = next(
        (
            index
            for index in range(heading_index + 1, len(merged))
            if re.match(r"^##\s+", merged[index].strip())
        ),
        len(merged),
    )
    existing = set(merged[heading_index + 1 : insert_at])
    merged[insert_at:insert_at] = [line for line in limits if line not in existing]
    return merged


def ensure_core_answer_surface(
    answer: str,
    question: str,
    *,
    fallback_fact_body: Sequence[str] = (),
    available_axes: Sequence[str] = (),
    card_core: str | None = None,
    failure_core: str | None = None,
    prefer_generated_core: bool = False,
) -> CoreAnswerIntegrityResult:
    lines = answer.splitlines()
    resolved_card_core = (
        _enrich_short_core_from_insight(card_core.strip(), answer)
        if card_core and card_core.strip()
        else card_core
    )
    core_indexes = [
        index
        for index, line in enumerate(lines)
        if re.fullmatch(r"##\s+(?:핵심 답|핵심 요약)\s*", line.strip())
    ]
    label, _axis_terms = _question_axis_surface(question)
    if not core_indexes:
        if resolved_card_core and resolved_card_core.strip():
            text = f"## 핵심 답\n{resolved_card_core.strip()}"
            if answer.strip():
                text += f"\n\n{answer.strip()}"
            return CoreAnswerIntegrityResult(
                text=text,
                status="card_generated",
                reason="derived_core_card",
            )
        fallback = _core_fallback_sentence(
            label,
            has_facts=bool(answer.strip() or fallback_fact_body),
            question=question,
            fact_body=(*answer.splitlines(), *fallback_fact_body),
            available_axes=available_axes,
        )
        text = f"## 핵심 답\n{fallback}"
        if answer.strip():
            text += f"\n\n{answer.strip()}"
        return CoreAnswerIntegrityResult(
            text=text,
            status="deterministic_fallback" if answer.strip() else "explicit_absence",
            reason="core_heading_missing",
        )

    start = core_indexes[0]
    section_end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if re.match(r"^##\s+", lines[index].strip())
        ),
        len(lines),
    )
    prose_end = next(
        (
            index
            for index in range(start + 1, section_end)
            if re.match(r"^#{2,6}\s+", lines[index].strip())
        ),
        section_end,
    )
    prose_body = lines[start + 1 : prose_end]
    if resolved_card_core and resolved_card_core.strip() and not prefer_generated_core:
        updated = [
            *lines[: start + 1],
            resolved_card_core.strip(),
            "",
            *lines[prose_end:section_end],
            *lines[section_end:],
        ]
        return CoreAnswerIntegrityResult(
            text="\n".join(updated).strip(),
            status="card_generated",
            reason="derived_core_card",
        )
    prose = [
        line.strip()
        for line in prose_body
        if _is_core_prose_line(line)
    ]
    prose_text = " ".join(prose)
    enriched_answer = answer
    if prose and (resolved_card_core or fallback_fact_body):
        enriched_prose = _enrich_short_core_from_insight(prose_text, answer)
        if enriched_prose != prose_text:
            enriched_lines = [
                *lines[: start + 1],
                enriched_prose,
                "",
                *lines[prose_end:],
            ]
            enriched_answer = "\n".join(enriched_lines).strip()
            prose_text = enriched_prose
    if prefer_generated_core and not is_document_summary_request(question):
        sentences = tuple(
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?])\s+", prose_text)
            if sentence.strip()
        )
        trimmed_prose = " ".join(sentences[:3])
        if (
            len(sentences) > 3
            and not _document_summary_core_violates_contract(trimmed_prose, question)
        ):
            updated = [
                *lines[: start + 1],
                trimmed_prose,
                "",
                *lines[prose_end:section_end],
                *lines[section_end:],
            ]
            return CoreAnswerIntegrityResult(
                text="\n".join(updated).strip(),
                status="present",
                reason="generated_core_trimmed",
            )
    if prose and _all_absence_or_zero_sentences(prose_text):
        if resolved_card_core and resolved_card_core.strip():
            updated = [
                *lines[: start + 1],
                resolved_card_core.strip(),
                "",
                *lines[prose_end:section_end],
                *lines[section_end:],
            ]
            return CoreAnswerIntegrityResult(
                text="\n".join(updated).strip(),
                status="card_generated",
                reason="data_present_failure_core_replaced",
            )
        has_real_fact_surface = bool(fallback_fact_body) or any(
            _line_has_real_fact(line) for line in lines[prose_end:section_end]
        )
        if not has_real_fact_surface:
            explicit_failure = str(failure_core or "").strip()
            updated = [
                *lines[: start + 1],
                explicit_failure or "조회된 데이터가 없습니다.",
                *lines[section_end:],
            ]
            return CoreAnswerIntegrityResult(
                text="\n".join(updated).strip(),
                status="explicit_absence",
                reason=(
                    "retrieval_failure_core"
                    if explicit_failure
                    else "all_absence_core"
                ),
            )
    contract_violation = (
        _document_summary_core_violates_contract(prose_text, question)
        if prefer_generated_core
        else _core_prose_violates_contract(prose_text, question)
    )
    prose_violation = contract_violation or _core_prose_contradicts_available_axes(
        prose_text,
        available_axes,
    )
    if prose and not prose_violation:
        return CoreAnswerIntegrityResult(text=enriched_answer, status="present")
    if resolved_card_core and resolved_card_core.strip():
        updated = [
            *lines[: start + 1],
            resolved_card_core.strip(),
            "",
            *lines[prose_end:section_end],
            *lines[section_end:],
        ]
        return CoreAnswerIntegrityResult(
            text="\n".join(updated).strip(),
            status="card_generated",
            reason="derived_core_card",
        )

    fact_body = lines[start + 1 : section_end]
    combined_fact_body = (*fact_body, *fallback_fact_body)
    has_facts = bool(fallback_fact_body) or any(
        line.strip().startswith("### ")
        or (line.strip().startswith("|") and not re.fullmatch(r"[| :\-]+", line.strip()))
        for line in fact_body
    )
    fallback = _core_fallback_sentence(
        label,
        has_facts=has_facts,
        question=question,
        fact_body=combined_fact_body,
        existing_prose=prose_text,
        available_axes=available_axes,
    )
    preserved_fact_body = (
        lines[prose_end:section_end]
        if prose and prose_violation
        else fact_body
    )
    updated = [
        *lines[: start + 1],
        fallback,
        "",
        *preserved_fact_body,
        *lines[section_end:],
    ]
    return CoreAnswerIntegrityResult(
        text="\n".join(updated).strip(),
        status="deterministic_fallback" if has_facts else "explicit_absence",
        reason="core_prose_contract" if prose else "core_prose_missing",
    )


def _core_prose_violates_contract(value: str, question: str = "") -> bool:
    sentences = tuple(
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", value)
        if sentence.strip()
    )
    if len(sentences) > 5:
        return True
    if any(
        len(re.findall(r"\d[\d,.-]*", sentence)) > 8
        for sentence in sentences
    ):
        return True
    normalized_question = " ".join(question.casefold().split())
    if any(
        not any(alias in value for alias in aliases)
        for aliases in _requested_metric_requirements(normalized_question)
    ):
        return True
    first_sentence = sentences[0] if sentences else ""
    if "특허" in normalized_question and not _patent_core_has_required_slots(first_sentence):
        return True
    if "아래 표" in value and not re.search(r"\d", value):
        return True
    requested_axes = _requested_core_axes(question)
    return len(requested_axes) >= 2 and any(
        not any(term in value for term in terms)
        for _axis, terms in requested_axes
    )


def _document_summary_core_violates_contract(value: str, question: str = "") -> bool:
    sentences = tuple(
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", value)
        if sentence.strip()
    )
    summary_mode = is_document_summary_request(question)
    if not 3 <= len(sentences) <= 5:
        return True
    if any(
        len(re.findall(r"\d[\d,.-]*", sentence)) > 8
        for sentence in sentences
    ):
        return True
    if any(marker in value for marker in ("검색 청크", "목차", "인사말", "| ---")):
        return True
    if "#" in value or "**" in value or any("|" in sentence for sentence in sentences):
        return True
    if not summary_mode and _all_absence_or_zero_sentences(value):
        return True
    return any(
        not any(alias in value for alias in aliases)
        for aliases in _requested_metric_requirements(" ".join(question.casefold().split()))
    )


def _enrich_short_core_from_insight(core: str, answer: str) -> str:
    """Fill a short data-backed core with already-grounded L1 fact sentences."""

    core_sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", core)
        if sentence.strip()
    ][:5]
    if len(core_sentences) >= 3:
        return " ".join(core_sentences)
    insight_match = re.search(
        r"(?ms)^##\s+종합 인사이트\s*\n(?P<body>.*?)(?=^##\s+|\Z)",
        answer,
    )
    if insight_match is None:
        return " ".join(core_sentences)
    first_paragraph = re.split(r"\n\s*\n", insight_match.group("body"), maxsplit=1)[0]
    for candidate in re.split(r"(?<=[.!?])\s+", first_paragraph):
        sentence = candidate.strip()
        if not _line_has_real_fact(sentence):
            continue
        if any(_near_duplicate_core_fact(existing, sentence) for existing in core_sentences):
            continue
        core_sentences.append(sentence)
        if len(core_sentences) >= 3:
            break
    return " ".join(core_sentences)


def _near_duplicate_core_fact(left: str, right: str) -> bool:
    token_re = re.compile(r"[A-Za-z가-힣]{2,}")
    left_tokens = {token.casefold() for token in token_re.findall(left)}
    right_tokens = {token.casefold() for token in token_re.findall(right)}
    if not left_tokens or not right_tokens:
        return left.strip() == right.strip()
    overlap = len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens))
    return overlap >= 0.65


def _requested_metric_requirements(question: str) -> tuple[tuple[str, ...], ...]:
    normalized = " ".join(question.casefold().split())
    requirements: list[tuple[int, tuple[str, ...]]] = []
    for query_terms, output_aliases in (
        (("환자수", "환자 수", "청구 실인원"), ("환자수", "환자 수", "청구 실인원")),
        (("유병률",), ("유병률",)),
        (("매출", "실적"), ("매출", "실적")),
        (("총액", "sellout", "sell out"), ("총액", "sellout", "sell out")),
        (("점유율", "시장점유율", "market share"), ("점유율", "시장점유율")),
    ):
        positions = tuple(
            position
            for term in query_terms
            if (position := normalized.find(term)) >= 0
        )
        if positions:
            requirements.append((min(positions), output_aliases))
    return tuple(aliases for _position, aliases in sorted(requirements))


def _patent_core_has_required_slots(value: str) -> bool:
    has_number = bool(re.search(r"(?:특허번호\s*)?(?:10-\d{4,}|\d{7,})", value))
    has_status = any(status in value for status in ("등록", "소멸", _UNPROVIDED_CELL))
    has_expiry = bool(re.search(r"20\d{2}-\d{2}-\d{2}", value))
    invalid_single_product_date = bool(
        re.search(r"제품\s*특허(?:는|가).{0,30}20\d{2}-\d{2}-\d{2}.{0,12}만료", value)
    )
    return has_number and has_status and has_expiry and not invalid_single_product_date


def _core_prose_contradicts_available_axes(
    value: str,
    available_axes: Sequence[str],
) -> bool:
    absence_patterns = {
        "patient": re.compile(
            r"(?:환자수|환자 수).{0,24}(?:확인되지|확인하지 못|미제공|없습니다)"
        ),
        "market": re.compile(
            r"(?:매출|총액|점유율).{0,24}(?:확인되지|확인하지 못|미제공|없습니다)"
        ),
    }
    return any(
        pattern.search(value)
        for axis, pattern in absence_patterns.items()
        if axis in available_axes
    )


def _requested_core_axes(question: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    normalized = " ".join(question.casefold().split())
    axes: list[tuple[int, str, tuple[str, ...]]] = []
    for axis, term_pairs in (
        (
            "patient",
            (
                ("환자수", "환자수"),
                ("환자 수", "환자수"),
                ("청구 실인원", "청구 실인원"),
                ("유병률", "유병률"),
            ),
        ),
        (
            "market",
            (
                ("매출", "매출"),
                ("총액", "총액"),
                ("점유율", "점유율"),
                ("market share", "점유율"),
                ("sellout", "매출"),
                ("sell out", "매출"),
            ),
        ),
        ("patent", (("특허", "특허"), ("만료", "만료"))),
    ):
        surface_terms = tuple(
            dict.fromkeys(
                surface_term
                for query_term, surface_term in term_pairs
                if query_term in normalized
            )
        )
        if surface_terms:
            positions = tuple(
                position
                for query_term, _surface_term in term_pairs
                if (position := normalized.find(query_term)) >= 0
            )
            axes.append((min(positions), axis, surface_terms))
    return tuple((axis, terms) for _position, axis, terms in sorted(axes))


def _remove_unsupported_hira_rate_sentences(value: str) -> tuple[list[str], list[str]]:
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", value)
        if sentence.strip()
    ]
    retained: list[str] = []
    removed: list[str] = []
    for sentence in sentences:
        target = removed if _unsupported_hira_rate_claim(sentence) else retained
        target.append(sentence)
    return retained, removed


def _question_axis_surface(question: str) -> tuple[str, tuple[str, ...] | None]:
    normalized = " ".join(question.casefold().split())
    for label, tokens in (
        ("환자수", ("환자수", "환자 수")),
        ("매출", ("매출", "실적")),
        ("점유율", ("점유율", "시장점유")),
        ("특허", ("특허",)),
        ("급여기준", ("급여", "고시")),
        ("임상 현황", ("임상", "nct", "clinical", "trial")),
        ("허가 정보", ("허가", "품목")),
    ):
        if any(
            re.search(r"(?<![a-z0-9])trials?(?![a-z0-9])", normalized)
            if token == "trial"
            else token in normalized
            for token in tokens
        ):
            return label, tokens
    return "정보", None


def _core_fallback_sentence(
    label: str,
    *,
    has_facts: bool,
    question: str = "",
    fact_body: Sequence[str] = (),
    existing_prose: str = "",
    available_axes: Sequence[str] = (),
) -> str:
    if has_facts:
        direct_fact = _direct_fact_fallback(
            question,
            fact_body,
            existing_prose=existing_prose,
            available_axes=available_axes,
        )
        if direct_fact:
            return direct_fact
        return "조회된 데이터가 있지만 요청 항목의 대표값을 구성하지 못했습니다."
    if len(_requested_core_axes(question)) >= 2:
        mixed_fallback = _mixed_axis_fallback(
            question,
            fact_body,
            existing_prose=existing_prose,
            available_axes=available_axes,
        )
        if mixed_fallback:
            return mixed_fallback
    return f"요청하신 {label}{_topic_particle(label)} 이번 조회에서 확인되지 않았습니다."


def _direct_fact_fallback(
    question: str,
    fact_body: Sequence[str],
    *,
    existing_prose: str = "",
    available_axes: Sequence[str] = (),
) -> str | None:
    requested_axes = _requested_core_axes(question)
    if len(requested_axes) >= 2:
        mixed_fallback = _mixed_axis_fallback(
            question,
            fact_body,
            existing_prose=existing_prose,
            available_axes=available_axes,
        )
        if mixed_fallback:
            return mixed_fallback

    if "특허" in question and (patent_fact := _patent_fact_fallback(fact_body)):
        return patent_fact

    if _is_document_summary_question(question) and (
        document_fact := _document_summary_fallback(fact_body)
    ):
        return document_fact

    market_facts = _market_fact_sentences(question, fact_body)
    if any(axis == "market" for axis, _terms in requested_axes) and market_facts:
        return " ".join(market_facts[:3])

    if "환자" in question:
        patient_fallback = _patient_count_fallback(question, fact_body)
        if patient_fallback:
            return patient_fallback
    return None


def _mixed_axis_fallback(
    question: str,
    fact_body: Sequence[str],
    *,
    existing_prose: str,
    available_axes: Sequence[str] = (),
) -> str | None:
    axes = {axis for axis, _terms in _requested_core_axes(question)}
    sentences: list[str] = []
    if "patient" in axes:
        if "유병률" in question:
            sentences.append(
                "요청하신 유병률은 인구 분모가 없어 이번 조회에서 직접 산출하지 않았습니다."
            )
        else:
            patient = _patient_count_fallback(question, fact_body)
            if patient:
                sentences.extend(
                    _bounded_sentences(patient, limit=1 if len(axes) >= 3 else 2)
                )
            elif "patient" in available_axes:
                sentences.append(
                    "요청하신 환자수 자료는 수신됐지만 대표값을 결속하지 못했습니다."
                )
            else:
                sentences.append("요청하신 환자수는 이번 조회에서 확인되지 않았습니다.")

    if "market" in axes:
        market_sentence = next(
            (
                sentence
                for sentence in _bounded_sentences(existing_prose, limit=3)
                if any(term in sentence for term in ("매출", "총액", "점유율"))
                and re.search(r"\d", sentence)
                and not any(
                    term in sentence
                    for term in ("확인하지 못", "확인되지 않", "미제공", "없습니다")
                )
            ),
            None,
        )
        if market_sentence:
            sentences.append(market_sentence)
        elif market_facts := _market_fact_sentences(question, fact_body):
            sentences.extend(market_facts)
        elif "market" in available_axes:
            label = (
                "점유율"
                if "점유율" in question
                else "총액"
                if "총액" in question
                else "매출"
            )
            sentences.append(
                f"요청하신 {label} 표면은 확인됐지만 대표값을 결속하지 못했습니다."
            )
        elif "총액" in question:
            sentences.append("요청하신 총액은 이번 조회에서 확인되지 않았습니다.")
        else:
            sentences.append("요청하신 매출은 이번 조회에서 확인되지 않았습니다.")

    if "patent" in axes:
        patent = _direct_fact_fallback(
            "특허 만료",
            fact_body,
            existing_prose=existing_prose,
            available_axes=available_axes,
        )
        if patent:
            sentences.extend(_bounded_sentences(patent, limit=1))
        else:
            sentences.append("요청하신 특허 정보는 이번 조회에서 확인되지 않았습니다.")
    axis_order = tuple(axis for axis, _terms in _requested_core_axes(question))

    def sentence_axis(sentence: str) -> str:
        if "환자수" in sentence or "유병률" in sentence:
            return "patient"
        if any(term in sentence for term in ("매출", "총액", "점유율")):
            return "market"
        if "특허" in sentence or "존속기간" in sentence:
            return "patent"
        return ""

    sentences.sort(
        key=lambda sentence: (
            axis_order.index(sentence_axis(sentence))
            if sentence_axis(sentence) in axis_order
            else len(axis_order)
        )
    )
    return " ".join(sentences[:3]) or None


def _requested_market_labels(question: str) -> tuple[str, ...]:
    normalized = " ".join(question.casefold().split())
    labels: list[tuple[int, str]] = []
    for terms, label in (
        (("매출", "실적"), "매출"),
        (("총액", "sellout", "sell out"), "총액"),
        (("점유율", "시장점유율", "market share"), "점유율"),
    ):
        positions = tuple(
            position
            for term in terms
            if (position := normalized.find(term)) >= 0
        )
        if positions:
            labels.append((min(positions), label))
    return tuple(dict.fromkeys(label for _position, label in sorted(labels)))


def _market_fact_sentences(question: str, lines: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        fact
        for label in _requested_market_labels(question)
        if (fact := _market_fact_fallback(question, lines, metric_label=label))
    )


def _market_fact_fallback(
    question: str,
    lines: Sequence[str],
    *,
    metric_label: str | None = None,
) -> str | None:
    if metric_label is not None:
        label = metric_label
    elif "점유율" in question or "market share" in question.casefold():
        label = "점유율"
    else:
        label = "총액" if "총액" in question else "매출"
    metric_headers = {
        "점유율": ("점유율", "시장점유율"),
        "총액": ("총액", "total_value", "합계", "Sell Out", "sellout"),
        "매출": ("매출", "매출액", "판매액", "Sell Out", "sellout"),
    }[label]
    for index, line in enumerate(lines[:-1]):
        headers = _markdown_cells(line)
        if not headers:
            continue
        metric_index = next(
            (
                position
                for position, header in enumerate(headers)
                if any(_markdown_header_matches(header, expected) for expected in metric_headers)
            ),
            None,
        )
        if metric_index is None:
            continue
        separator = _markdown_cells(lines[index + 1])
        if not separator or not all(_TABLE_SEPARATOR_RE.fullmatch(cell) for cell in separator):
            continue
        entity_index = next(
            (
                position
                for expected in ("브랜드", "제품", "품목", "시장")
                for position, header in enumerate(headers)
                if header == expected
            ),
            None,
        )
        period_index = next(
            (position for position, header in enumerate(headers) if header in {"기간", "연월", "월"}),
            None,
        )
        candidates: list[tuple[str, str, str]] = []
        for row_line in lines[index + 2 :]:
            cells = _markdown_cells(row_line)
            if not cells:
                break
            if metric_index >= len(cells):
                continue
            value = cells[metric_index]
            if not value or value == _UNPROVIDED_CELL:
                continue
            entity = (
                cells[entity_index]
                if entity_index is not None and entity_index < len(cells)
                else "요청 대상"
            )
            period = (
                cells[period_index]
                if period_index is not None and period_index < len(cells)
                else "표시 기간"
            )
            unit_match = re.search(r"\(([^()]+)\)", headers[metric_index])
            unit = unit_match.group(1) if unit_match else ""
            rendered_value = value
            if unit and not value.endswith(unit):
                rendered_value = f"{value}{unit}"
            candidates.append((entity, period, rendered_value))
        matched = tuple(
            candidate
            for candidate in candidates
            if _question_mentions_table_entity(question, candidate[0])
        )
        unique_entities = {
            re.sub(r"[^0-9A-Za-z가-힣]", "", candidate[0]).casefold()
            for candidate in candidates
        }
        eligible = matched or (tuple(candidates) if len(unique_entities) == 1 else ())
        requested_periods = _requested_market_periods(question)
        if requested_periods:
            period_matched = tuple(
                candidate
                for candidate in eligible
                if _normalized_period(candidate[1]) in requested_periods
            )
            selected = max(period_matched, key=lambda item: _period_sort_key(item[1])) if period_matched else None
        else:
            selected = max(eligible, key=lambda item: _period_sort_key(item[1])) if eligible else None
        if selected is not None:
            entity, period, rendered_value = selected
            return (
                f"내부 데이터마트의 {period} 기준 {entity} {label}은 "
                f"{rendered_value}입니다."
            )
    return None


def _normalized_period(value: str) -> str:
    year_month = re.search(r"(?P<year>20\d{2})\D+(?P<month>\d{1,2})", value)
    if year_month:
        return f"{year_month.group('year')}-{int(year_month.group('month')):02d}"
    year = re.search(r"(?<!\d)20\d{2}(?!\d)", value)
    return year.group(0) if year else ""


def _requested_market_periods(question: str) -> tuple[str, ...]:
    values = [
        f"{match.group('year')}-{int(match.group('month')):02d}"
        for match in _YEAR_MONTH_RE.finditer(question)
    ]
    if not values:
        values.extend(re.findall(r"(?<!\d)20\d{2}(?!\d)", question))
    return tuple(dict.fromkeys(values))


def _period_sort_key(value: str) -> tuple[int, int, str]:
    normalized = _normalized_period(value)
    match = re.fullmatch(r"(?P<year>20\d{2})(?:-(?P<month>\d{2}))?", normalized)
    if match is None:
        return 0, 0, value
    return int(match.group("year")), int(match.group("month") or 0), value


def _question_mentions_table_entity(question: str, entity: str) -> bool:
    normalized_question = re.sub(r"[^0-9A-Za-z가-힣]", "", question).casefold()
    normalized_entity = re.sub(r"[^0-9A-Za-z가-힣]", "", entity).casefold()
    return bool(normalized_entity) and normalized_entity in normalized_question


def _patent_fact_fallback(lines: Sequence[str]) -> str | None:
    for index, line in enumerate(lines[:-1]):
        headers = _markdown_cells(line)
        if not headers:
            continue
        number_index = next(
            (position for position, header in enumerate(headers) if header in {"특허번호", "등록번호"}),
            None,
        )
        status_index = next(
            (
                position
                for position, header in enumerate(headers)
                if header in {"상태", "목록상 상태", "등록/소멸", "특허상태"}
            ),
            None,
        )
        expiry_index = next(
            (position for position, header in enumerate(headers) if header in {"존속기간 만료일", "만료일"}),
            None,
        )
        if number_index is None or expiry_index is None:
            continue
        separator = _markdown_cells(lines[index + 1])
        if not separator or not all(_TABLE_SEPARATOR_RE.fullmatch(cell) for cell in separator):
            continue
        rows: list[tuple[str, str, str]] = []
        for row_line in lines[index + 2 :]:
            cells = _markdown_cells(row_line)
            if not cells:
                break
            required_indexes = (number_index, expiry_index)
            largest_index = max(required_indexes)
            if largest_index >= len(cells):
                continue
            number = cells[number_index]
            status = (
                cells[status_index]
                if status_index is not None and status_index < len(cells)
                else _UNPROVIDED_CELL
            )
            expiry = cells[expiry_index]
            if number and status and re.fullmatch(r"20\d{2}-\d{2}-\d{2}", expiry):
                rows.append((number, status, expiry))
        if not rows:
            return None
        registered = tuple(row for row in rows if "등록" in row[1] and "소멸" not in row[1])
        candidates = registered or tuple(rows)
        number, status, expiry = max(candidates, key=lambda row: (row[2], row[0]))
        qualifier = "등록 특허 가운데 " if registered else "확인된 특허 가운데 "
        return (
            f"식약처 의약품 특허목록의 {qualifier}가장 늦은 존속기간 만료일은 "
            f"{expiry}이며, 특허번호 {number}의 상태는 {status}입니다."
        )
    return None


def _is_document_summary_question(question: str) -> bool:
    return is_document_summary_request(question)


def _all_absence_or_zero_sentences(value: str) -> bool:
    sentences = tuple(
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", value)
        if sentence.strip()
    )
    if not sentences:
        return False
    absence = re.compile(
        r"(?:확인되지|확인하지\s*못|미제공|반환되지|0\s*건|없습니다|시간\s*초과|"
        r"대표값(?:을)?\s*구성하지\s*못|조회된\s*데이터가\s*있지만)"
    )
    return all(absence.search(sentence) is not None for sentence in sentences)


def _line_has_real_fact(value: str) -> bool:
    line = value.strip()
    if not line or _all_absence_or_zero_sentences(line):
        return False
    return bool(re.search(r"\d", line) or line.startswith("|"))


def _document_summary_fallback(lines: Sequence[str]) -> str | None:
    for index, line in enumerate(lines[:-1]):
        headers = _markdown_cells(line)
        if not headers or "발췌" not in headers:
            continue
        separator = _markdown_cells(lines[index + 1])
        if not separator or not all(_TABLE_SEPARATOR_RE.fullmatch(cell) for cell in separator):
            continue
        excerpt_index = headers.index("발췌")
        file_index = headers.index("파일") if "파일" in headers else None
        section_index = headers.index("절") if "절" in headers else None
        facts: list[str] = []
        file_name = "업로드 문서"
        section = ""
        for row_line in lines[index + 2 :]:
            cells = _markdown_cells(row_line)
            if not cells:
                break
            if excerpt_index >= len(cells):
                continue
            if file_index is not None and file_index < len(cells) and cells[file_index]:
                file_name = cells[file_index]
            if section_index is not None and section_index < len(cells) and cells[section_index]:
                section = section or cells[section_index]
            excerpt = cells[excerpt_index].strip()
            if excerpt and excerpt != _UNPROVIDED_CELL:
                facts.append(excerpt if excerpt.endswith((".", "!", "?")) else f"{excerpt}.")
            if len(facts) == 2:
                break
        if not facts:
            return None
        topic = f"{file_name}은 {section} 내용을 다룹니다." if section else f"{file_name}의 핵심 내용을 확인했습니다."
        return " ".join((topic, *facts))
    return None


def _bounded_sentences(value: str, *, limit: int) -> tuple[str, ...]:
    return tuple(
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", value)
        if sentence.strip()
    )[:limit]


def _patient_count_fallback(question: str, lines: Sequence[str]) -> str | None:
    for index, line in enumerate(lines[:-1]):
        headers = _markdown_cells(line)
        if "환자수" not in headers:
            continue
        separator = _markdown_cells(lines[index + 1])
        if not separator or not all(_TABLE_SEPARATOR_RE.fullmatch(cell) for cell in separator):
            continue
        patient_index = headers.index("환자수")
        code_index = headers.index("상병코드") if "상병코드" in headers else None
        period_index = headers.index("기간") if "기간" in headers else None
        label_indexes = tuple(
            cell_index
            for cell_index, header in enumerate(headers)
            if header in {"구분", "성별", "연령"}
        )
        provided: list[tuple[str, str, str, int]] = []
        missing_count = 0
        table_codes: list[str] = []
        table_years: list[str] = []
        for row_line in lines[index + 2 :]:
            cells = _markdown_cells(row_line)
            if not cells or patient_index >= len(cells):
                break
            if code_index is not None and code_index < len(cells):
                match = re.fullmatch(r"[A-Z]\d{2}(?:\.\d+)?", cells[code_index].upper())
                if match:
                    table_codes.append(match.group(0))
            if period_index is not None and period_index < len(cells):
                year_match = re.search(r"(?<!\d)20\d{2}(?!\d)", cells[period_index])
                if year_match:
                    table_years.append(year_match.group(0))
            value = cells[patient_index]
            if value == _UNPROVIDED_CELL:
                missing_count += 1
                continue
            labels = tuple(
                cells[cell_index]
                for cell_index in label_indexes
                if cell_index < len(cells) and cells[cell_index] != _UNPROVIDED_CELL
            )
            label = "·".join(labels)
            rendered_value = value if value.endswith("명") else f"{value}명"
            numeric_value = int(re.sub(r"\D", "", value) or "0")
            code = (
                cells[code_index]
                if code_index is not None and code_index < len(cells)
                else ""
            )
            provided.append((code, label, rendered_value, numeric_value))

        if not provided and not missing_count:
            return None
        codes = tuple(dict.fromkeys(table_codes)) or tuple(
            dict.fromkeys(
                match.group(0).upper()
                for match in re.finditer(r"\b[A-Z]\d{2}(?:\.\d+)?\b", question)
            )
        )
        years = tuple(dict.fromkeys(table_years)) or tuple(
            dict.fromkeys(re.findall(r"(?<!\d)20\d{2}(?!\d)", question))
        )
        disease_match = re.search(
            r"([A-Za-z가-힣][A-Za-z가-힣0-9._-]*)\s*환자\s*수",
            question,
        )
        disease_name = disease_match.group(1) if disease_match else ""
        year_prefix = f"{years[0]}년 " if years else ""
        if disease_name and codes and disease_name.upper() not in codes:
            code_summary = (
                codes[0]
                if len(codes) == 1
                else f"{codes[0]} 등 {len(codes)}개 상병"
            )
            subject = f"{year_prefix}{disease_name}({code_summary})".strip()
        elif disease_name and codes:
            subject = " ".join((*years, *codes)).strip()
        elif disease_name:
            subject = f"{year_prefix}{disease_name}".strip()
        else:
            subject = " ".join((*years, *codes)).strip() or "요청한"
        sentences: list[str] = []
        if provided:
            if len(provided) <= 2:
                details = ", ".join(
                    " ".join(part for part in (label, value) if part)
                    for _code, label, value, _numeric in provided
                )
                sentences.append(f"{subject} 환자수는 {details}으로 확인됐습니다.")
            else:
                code, label, value, _numeric = max(provided, key=lambda item: item[3])
                representative = "·".join(
                    part
                    for part in (
                        code if len(codes) > 1 else "",
                        label,
                    )
                    if part
                )
                subject_value = " ".join(part for part in (representative, value) if part)
                sentences.append(
                    f"{subject} 환자수는 {subject_value}이 가장 큰 확인값입니다."
                )
                sentences.append("성별·진료 구분별 세부 값은 아래 표와 같습니다.")
        if missing_count:
            if provided:
                sentences.append(f"{missing_count}행은 수치 미제공 상태입니다.")
            else:
                sentences.append(
                    f"{subject} 환자수는 {missing_count}행 수신됐으나 "
                    "수치 미제공 상태입니다."
                )
        return " ".join(sentences)
    return None


def _markdown_column_values(
    lines: Sequence[str],
    *,
    headers: Sequence[str],
) -> tuple[str, ...]:
    for index, line in enumerate(lines[:-1]):
        header_cells = _markdown_cells(line)
        if not header_cells:
            continue
        column_index = next(
            (
                cell_index
                for cell_index, cell in enumerate(header_cells)
                if any(_markdown_header_matches(cell, header) for header in headers)
            ),
            None,
        )
        if column_index is None:
            continue
        separator_cells = _markdown_cells(lines[index + 1])
        if not separator_cells or not all(
            _TABLE_SEPARATOR_RE.fullmatch(cell) for cell in separator_cells
        ):
            continue
        values: list[str] = []
        for data_line in lines[index + 2 :]:
            cells = _markdown_cells(data_line)
            if not cells:
                break
            if column_index < len(cells):
                values.append(cells[column_index])
        return tuple(values)
    return ()


def _markdown_header_matches(value: str, expected: str) -> bool:
    normalized = " ".join(value.casefold().split())
    target = " ".join(expected.casefold().split())
    return normalized == target or bool(
        re.fullmatch(rf"{re.escape(target)}\s*\([^()]+\)", normalized)
    )


def _markdown_cells(line: str) -> tuple[str, ...]:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return ()
    return tuple(cell.strip() for cell in stripped.strip("|").split("|"))


def _topic_particle(value: str) -> str:
    if not value:
        return "는"
    last = value[-1]
    if last.isdigit():
        return "은"
    code = ord(last)
    if 0xAC00 <= code <= 0xD7A3:
        return "은" if (code - 0xAC00) % 28 else "는"
    return "는"


def _is_core_prose_line(line: str) -> bool:
    stripped = line.strip()
    return bool(
        stripped
        and not stripped.startswith(("#", "|", "- ", "* ", "[확인 한계]"))
        and re.fullmatch(r"본문 표시 \d+행 · 조회 상세 \d+건", stripped) is None
        and re.fullmatch(r"원천 미제공 \d+행은 표에서 제외했습니다\.", stripped)
        is None
        and re.fullmatch(r"\*\*[^*]+\*\*", stripped) is None
    )


def _unsupported_hira_rate_claim(value: str) -> bool:
    matches = tuple(_HIRA_RATE_OR_RISK_RE.finditer(value))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        if not _HIRA_LIMITATION_RE.search(value[match.start() : end]):
            return True
    return False


def evidence_has_hira_patient_count(evidence_sets: Sequence[EvidenceSet]) -> bool:
    return any(
        evidence_set.source == "hira"
        and any(
            record.result_kind == "patient_count"
            or "ptntcnt" in {key.casefold() for key in _mapping_keys(record.payload)}
            for record in evidence_set.records
        )
        for evidence_set in evidence_sets
    )


def evidence_hira_code_count(evidence_sets: Sequence[EvidenceSet]) -> int:
    codes = {
        match.group(0).upper()
        for evidence_set in evidence_sets
        if evidence_set.source == "hira"
        for record in evidence_set.records
        for value in _scalar_values(record.payload)
        for match in re.finditer(r"(?<![A-Za-z0-9])[A-Z]\d{2,3}(?![A-Za-z0-9])", value)
    }
    return len(codes)


def _mapping_keys(value: object) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        keys: list[str] = []
        for key, nested in value.items():
            keys.append(str(key))
            keys.extend(_mapping_keys(nested))
        return tuple(keys)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(key for nested in value for key in _mapping_keys(nested))
    return ()


def _policy_text(stripped: str) -> tuple[str, str]:
    heading = re.match(r"^(?P<prefix>#{1,6}\s+)(?P<body>.+)$", stripped)
    if heading is None:
        return stripped, ""
    return heading.group("body"), heading.group("prefix")


def _sanitize_table_line(
    line: str,
    context: SemanticEvidenceContext,
) -> tuple[str, tuple[dict[str, str], ...], int, int]:
    cells = line.split("|")
    transformations: list[dict[str, str]] = []
    downgrade_count = 0
    deletion_count = 0
    for index, raw_cell in enumerate(cells):
        cell = raw_cell.strip()
        if not cell or _TABLE_SEPARATOR_RE.fullmatch(cell):
            continue
        if context.has_hira_patient_count and _unsupported_hira_rate_claim(cell):
            cells[index] = f" {_UNPROVIDED_CELL} "
            transformations.append(
                {
                    "from": "HIRA_RATE_OR_RISK",
                    "to": "DELETE",
                    "reason": "patient_count_has_no_population_denominator",
                }
            )
            deletion_count += 1
            continue
        if context.has_hira_patient_count and _HIRA_CARE_PATHWAY_RE.search(cell):
            cells[index] = f" {_UNPROVIDED_CELL} "
            transformations.append(
                {
                    "from": "HIRA_CARE_PATHWAY_INTERPRETATION",
                    "to": "DELETE",
                    "reason": "patient_counts_do_not_establish_care_pathway",
                }
            )
            deletion_count += 1
            continue
        if _TREND_RE.search(cell):
            cells[index] = f" {_UNPROVIDED_CELL} "
            transformations.append(
                {"from": "TREND_PREDICTION", "to": "DELETE", "reason": "unsupported"}
            )
            deletion_count += 1
            continue
        if _CAUSAL_ASSERTION_RE.search(cell):
            cells[index] = f" {_UNPROVIDED_CELL} "
            transformations.append(
                {"from": "CAUSES", "to": "DELETE", "reason": "unsupported_table_claim"}
            )
            deletion_count += 1
            continue
        updated = cell
        if context.observed_count < context.requested_count and re.search(
            r"(?:모든|전체)\s*브랜드(?:를|가|는|의)?\s*(?:완전하게\s*)?비교",
            updated,
        ):
            updated = re.sub(r"(?:모든|전체)\s*브랜드", "확인된 일부 브랜드", updated)
            transformations.append(
                {
                    "from": "COMPLETE_COMPARISON",
                    "to": "PARTIAL_SUBSET_COMPARISON",
                    "reason": "partial_entity_snapshot",
                }
            )
            downgrade_count += 1
        if _GLOBAL_ABSENCE_RE.search(updated):
            updated = re.sub(
                r"(?:전\s*세계|모든|전체)",
                "이번 조회 범위에서",
                updated,
                count=1,
            )
            transformations.append(
                {
                    "from": "GLOBAL_ABSENCE",
                    "to": "NOT_FOUND_IN_THIS_QUERY",
                    "reason": "query_scoped_evidence",
                }
            )
            downgrade_count += 1
        if updated != cell:
            cells[index] = f" {updated} "
    return "|".join(cells), tuple(transformations), downgrade_count, deletion_count


def evidence_has_temporal_support(evidence_sets: Sequence[EvidenceSet]) -> bool:
    dates = {
        value
        for evidence_set in evidence_sets
        for record in evidence_set.records
        for value in _date_values(record.payload)
    }
    return len(dates) >= 2


def evidence_support_text(evidence_sets: Sequence[EvidenceSet]) -> str:
    return " ".join(
        value
        for evidence_set in evidence_sets
        for record in evidence_set.records
        for value in _scalar_values(record.payload)
    )


def evidence_temporal_support_texts(
    evidence_sets: Sequence[EvidenceSet],
) -> tuple[str, ...]:
    return tuple(
        " ".join(_scalar_values(record.payload))
        for evidence_set in evidence_sets
        for record in evidence_set.records
        if _date_values(record.payload)
    )


def _causal_clauses(sentence: str) -> tuple[str, str] | None:
    for pattern in _CAUSE_SENTENCE_PATTERNS:
        match = pattern.match(sentence)
        if match is not None:
            return match.group("left").strip(), match.group("right").strip()
    return None


def _clauses_temporally_bound(
    left: str,
    right: str,
    context: SemanticEvidenceContext,
) -> bool:
    if not context.has_temporal_support:
        return False
    normalized_left = _normalized_text(left)
    normalized_right = _normalized_text(right)
    if not normalized_left or not normalized_right:
        return False
    normalized_records = tuple(
        _normalized_text(value) for value in context.temporal_support_texts
    )
    left_records = {
        index
        for index, value in enumerate(normalized_records)
        if normalized_left in value
    }
    right_records = {
        index
        for index, value in enumerate(normalized_records)
        if normalized_right in value
    }
    return any(left_index != right_index for left_index in left_records for right_index in right_records)


def _normalized_text(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value).casefold()


def _scalar_values(value: object) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        return tuple(
            text for nested in value.values() for text in _scalar_values(nested)
        )
    if isinstance(value, (list, tuple)):
        return tuple(text for nested in value for text in _scalar_values(nested))
    return () if value in (None, "") else (str(value),)


def _date_values(value: object) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        return tuple(
            date
            for nested in value.values()
            for date in _date_values(nested)
        )
    if isinstance(value, (list, tuple)):
        return tuple(date for nested in value for date in _date_values(nested))
    text = str(value)
    return (text,) if _DATE_VALUE_RE.fullmatch(text) else ()


def _retained(
    original: str,
    downgraded: str,
    causal_level: Literal["NONE", "TEMPORAL", "ASSOCIATION"],
) -> PredicateDowngrade:
    if original == downgraded:
        return PredicateDowngrade(
            original_predicate_id=original,
            predicate_id=None,
            action="delete",
            causal_level="NONE",
            reason_code="predicate_unchanged",
        )
    return PredicateDowngrade(
        original_predicate_id=original,
        predicate_id=downgraded,
        action="retain",
        causal_level=causal_level,
        reason_code="predicate_transformed",
    )
