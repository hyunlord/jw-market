from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal
from itertools import pairwise
from typing import Any

from jw_chat_agent_poc.service.v4.fact_digest import DerivedCoreCard, FactDigest

_INSIGHT_SECTION_RE = re.compile(
    r"(?ms)^##\s+종합 인사이트\s*\n(?P<body>.*?)(?=^##\s+|\Z)"
)
_CONTEXT_SECTION_RE = re.compile(
    r"(?ms)^##\s+근거와 맥락\s*\n(?P<body>.*?)(?=^##\s+|\Z)"
)
_SECTION_HEADING_NAME = (
    r"종합 인사이트|조회 제한|조사 범위와 완전성|출처(?:별\s+조회\s+결과)?"
)
_INLINE_SECTION_HEADING_RE = re.compile(
    rf"(?<=\S)(?:#{{2,6}}\s*)+(?P<name>{_SECTION_HEADING_NAME})\s*"
)
_REPEATED_SECTION_HEADING_RE = re.compile(
    rf"(?m)^\s*(?:#{{2,6}}\s*)+(?P<name>{_SECTION_HEADING_NAME})\s*$"
)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_COMPANY_SUFFIX_RE = re.compile(r"^Ltd\.(?:[가-힣]|\s|$)", re.IGNORECASE)
_NUMERIC_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9가-힣])(?:\d{4}-Q[1-4]|\d{4}-\d{2}-\d{2}|\d{1,2}-\d{5,}|\d[\d,]*(?:\.\d+)?)"
)
_ORPHAN_CONJUNCTION_RE = re.compile(r"^(?:그러나|따라서)\s*[,，]?\s*")
_DERIVED_FIELD_METADATA_RE = re.compile(
    r"(?:\bderived(?:\s+|_)fields?\b|"
    r"\b(?:dimension|evidence(?:\s+|_)ids?)\s*:|"
    r"환자수\s+(?:남|여)\s*:\s*\d[\d,]*명)",
    re.IGNORECASE,
)
_NEGATIVE_ELAPSED_RE = re.compile(
    r"-\s*(?P<months>\d[\d,]*)\s*개월(?P<suffix>(?:이)?\s*경과)"
)
_ENUMERATION_FALLBACK_RE = re.compile(
    r"(?:명|건|원|%)으로\s+확인(?:되었|됐)습니다[.!]?\s*$"
)
_UNTYPED_ABSENCE_RE = re.compile(
    r"(?:확인되지\s*않|확인하지\s*못|확인할\s*수\s*없|반환되지\s*않)"
)
_BLACKLIST = (
    "나란히 확인됩니다",
    "비교의 기준이 됩니다",
    "고정하면 구체화",
    "비교 단위를 구성",
    "직접 근거입니다",
    "확인이 필수적",
    "동일 기준으로 비교할 수",
    "아래 표에 정리",
    "자료를 함께 보면",
)
_READER_DELEGATION_BLACKLIST = (
    "가늠해 볼 수 있습니다",
    "추론할 수 있습니다",
    "의미합니다",
    "확인됩니다",
)
_INTERPRETATION_MARKERS = (
    "로 해석됩니다",
    "로 볼 수 있습니다",
    "을 보여줍니다",
    "를 보여줍니다",
    "을 시사합니다",
    "를 시사합니다",
    "을 의미합니다",
    "를 의미합니다",
)
_L3_INFERENCE_MARKERS = (
    "로 해석될 수 있습니다",
    "추정됩니다",
    "가능성이 있습니다",
)
_EXPANSION_COMPARISON_MARKERS = (
    "비교",
    "차이",
    "격차",
    "대비",
    "비율",
    "비중",
    "집중",
    "높",
    "낮",
    "구성",
)
_EXPANSION_TREND_MARKERS = (
    "증가",
    "감소",
    "성장",
    "하락",
    "추세",
    "변화",
    "전환",
    "흐름",
)
_EXPANSION_COMPOSITION_MARKERS = (
    "구성",
    "비중",
    "집중",
    "분포",
)
_SOURCE_ALIASES: dict[str, tuple[str, ...]] = {
    "hira": ("hira", "건강보험심사평가원"),
    "mart": ("mart", "데이터마트", "ubist", "iqvia"),
    "patent": ("patent", "특허목록", "kipris"),
    "clinicaltrials": ("clinicaltrials", "clinicaltrials.gov", "임상시험"),
    "nedrug": ("nedrug", "의약품안전나라", "식약처"),
    "document": ("document", "업로드 문서", "업로드 파일"),
    "document_sql": ("document_sql", "file_sql", "엑셀", "sql"),
    "document_vdb": ("document_vdb", "file_vdb", "pdf", "문서 검색"),
}
_EXPANSION_SENTENCE_TARGET = 12
_RICH_EXPANSION_SENTENCE_TARGET = 15
_EXPANSION_SENTENCE_MAXIMUM = 20
_EXPANSION_PARAGRAPH_TARGET = 3
_EXPANSION_AXIS_TARGET = 2
_RICH_MATERIAL_MINIMUM = 6
_RICH_EXPANSION_MATERIAL_MINIMUM = 15
_UNUSED_MATERIAL_FAILURE_THRESHOLD = 5
_PROHIBITED_INFERENCE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "clinical_count_to_efficacy",
        re.compile(
            r"(?:"
            r"임상(?:시험)?\s*\d*[\d,.]*\s*건|임상\s*건수|"
            r"\d[\d,.]*\s*건\s*(?:의\s*)?(?:\d+\s*상\s*)?임상(?:시험)?"
            r")"
            r".*(?:효능|유효성|안전성|처방\s*신뢰).*"
            r"(?:입증|확인|보여|시사|뜻|의미|뒷받침|방어|기전|작용)"
        ),
    ),
    (
        "sales_to_prescribing_preference",
        re.compile(
            r"매출.*처방\s*선호.*(?:입증|확인|보여|시사|뜻|의미)"
        ),
    ),
    (
        "change_date_to_management_capability",
        re.compile(
            r"변경일.*관리\s*역량.*(?:입증|확인|보여|시사|뜻|의미)?"
        ),
    ),
    (
        "expiry_to_generic_certainty",
        re.compile(r"만료.*제네릭.*(?:확정|보장|입증|확실|단정)"),
    ),
    (
        "absence_to_development_stagnation",
        re.compile(
            r"(?:부재|없(?:음|습니다|으므로)).*개발.*정체.*"
            r"(?:입증|확인|보여|시사|뜻|의미)?"
        ),
    ),
    (
        "applied_rows_to_transactions",
        re.compile(
            r"(?:applied_rows|적용\s*[\d,.]*\s*행).*거래(?:\s*데이터)?\s*"
            r"(?:건수|수|규모|분포)"
        ),
    ),
    (
        "patient_count_to_treatment_effect",
        re.compile(r"환자수.*치료\s*효과.*입증"),
    ),
)
_L3_ACTOR_MARKERS = (
    "MI팀",
    "시장 참여자",
    "제약사",
    "경쟁사",
    "의사결정자",
)
_L3_CHOICE_MARKERS = (
    "전략",
    "우선순위",
    "대응",
    "자원 배분",
    "분석 대상",
    "검토 범위",
)
_STATUS_TERMS = (
    "등록",
    "소멸",
    "RECRUITING",
    "NOT_YET_RECRUITING",
    "ACTIVE_NOT_RECRUITING",
    "ENROLLING_BY_INVITATION",
    "COMPLETED",
    "TERMINATED",
    "WITHDRAWN",
)
_CLINICAL_ONLY_TERMS = (
    "RECRUITING",
    "NOT_YET_RECRUITING",
    "ACTIVE_NOT_RECRUITING",
    "ENROLLING_BY_INVITATION",
    "COMPLETED",
    "TERMINATED",
    "WITHDRAWN",
    "모집중",
    "모집 중",
    "모집 전",
)
_INSIGHT_FIELD_LABELS = {
    "patent_no": "특허번호",
    "expiration_date": "존속기간 만료일",
    "product_combination_count": "특허번호·품목 조합 수",
    "status_distribution": "상태별 분포",
    "active_count": "유효 건수",
    "expired_count": "소멸 건수",
    "elapsed_months": "경과 개월 수",
    "patent_type": "특허구분",
    "approval_elapsed_years": "허가 후 경과 연수",
    "approval_date": "허가일",
    "approval date": "허가일",
    "change_date": "변경일",
    "change date": "변경일",
    "reexamination_date": "재심사기간",
    "reexamination date": "재심사기간",
    "reexamination_remaining_months": "재심사기간 잔여 개월 수",
    "latest_period": "최신 기간",
    "applied_rows": "집계 적용 행 수",
    "product": "품목",
    "status": "상태",
    "completed_count": "완료 건수",
    "code_count": "상병코드 수",
    "late_phase_count": "후기 임상 건수",
    "late_phase_ratio_pct": "후기 임상 비율",
}

_NON_SUBSTANTIVE_MATERIAL_PATHS = frozenset(
    {"entity", "metric", "unit", "representative.source"}
)
_RAW_DOCUMENT_MATERIAL_PREFIXES = (
    "representative.content",
    "file_facts.chunks",
    "file_facts.targeted_facts",
)


def insight_expansion_metrics(
    answer: str,
    digest: FactDigest,
) -> dict[str, Any]:
    """Measure whether a grounded insight has exhausted the available material."""
    normalized = _normalize_section_headings(answer)
    matches = tuple(_INSIGHT_SECTION_RE.finditer(normalized))
    body = matches[-1].group("body").strip() if matches else ""
    paragraphs = tuple(
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", body)
        if paragraph.strip()
    )
    sentences = tuple(
        sentence.strip()
        for paragraph in paragraphs
        for sentence in _split_sentences(paragraph)
        if sentence.strip()
    )
    axes: set[str] = set()
    if any(marker in body for marker in _EXPANSION_COMPARISON_MARKERS):
        axes.add("comparison")
    if any(marker in body for marker in _EXPANSION_TREND_MARKERS):
        axes.add("trend")
    if any(marker in body for marker in _EXPANSION_COMPOSITION_MARKERS):
        axes.add("composition")
    if _has_cross_source_binding(sentences, digest):
        axes.add("fusion")

    material_count = len(_digest_material_entries(digest))
    eligible_material_count = len(_expansion_material_entries(digest))
    unused_material_count = len(unused_fact_digest_materials(answer, digest))
    data_rich = material_count >= _RICH_MATERIAL_MINIMUM
    rich_material = material_count >= _RICH_EXPANSION_MATERIAL_MINIMUM
    required_sentence_count = (
        _RICH_EXPANSION_SENTENCE_TARGET
        if rich_material
        else _EXPANSION_SENTENCE_TARGET
        if data_rich
        else 1
        if _source_data_count(digest) > 0
        else 0
    )
    target_met = bool(
        required_sentence_count == 0
        or (
            len(sentences) >= required_sentence_count
            and len(sentences) <= _EXPANSION_SENTENCE_MAXIMUM
            and (
                not data_rich
                or (
                    len(paragraphs) >= _EXPANSION_PARAGRAPH_TARGET
                    and len(axes) >= _EXPANSION_AXIS_TARGET
                )
            )
            and (
                not rich_material
                or unused_material_count < _UNUSED_MATERIAL_FAILURE_THRESHOLD
            )
        )
    )
    return {
        "paragraph_count": len(paragraphs),
        "sentence_count": len(sentences),
        "available_material_count": material_count,
        "eligible_material_count": eligible_material_count,
        "unused_material_count": unused_material_count,
        "data_rich": data_rich,
        "rich_material": rich_material,
        "required_sentence_count": required_sentence_count,
        "maximum_sentence_count": _EXPANSION_SENTENCE_MAXIMUM,
        "expansion_axes": sorted(axes),
        "expansion_axis_count": len(axes),
        "expansion_target_met": target_met,
        "retry_reason": None if target_met else "확장 부족",
    }


def unused_fact_digest_materials(
    answer: str,
    digest: FactDigest,
    *,
    limit: int = 24,
) -> list[dict[str, Any]]:
    """Return a bounded, code-owned list of digest material absent from the prose."""
    normalized_answer = _normalize_section_headings(answer)
    matches = tuple(_INSIGHT_SECTION_RE.finditer(normalized_answer))
    normalized = (
        matches[-1].group("body") if matches else ""
    ).casefold()
    unused: list[dict[str, Any]] = []
    for entry in _expansion_material_entries(digest):
        if _material_is_used(normalized, entry["value"]):
            continue
        unused.append(entry)
        if len(unused) >= max(1, limit):
            break
    return unused


def sanitize_s17_insight(
    answer: str,
    digest: FactDigest,
) -> tuple[str, dict[str, Any]]:
    """Replace unsupported insight prose with evidence-bound statements."""
    answer = _normalize_section_headings(answer)
    matches = tuple(_INSIGHT_SECTION_RE.finditer(answer))
    source_data_count = _source_data_count(digest)
    if source_data_count == 0:
        sanitized = _INSIGHT_SECTION_RE.sub("", answer).strip()
        expansion = insight_expansion_metrics(sanitized, digest)
        return sanitized, {
            **_trace(
                (),
                (),
                omitted=True,
                section_found=bool(matches),
                candidate_sentence_count=0,
                reject_reason_counts={"no_source_data": int(bool(matches))},
            ),
            "reason_code": "NO_SOURCE_DATA",
            "source_data_count": 0,
            **_expansion_trace_fields(expansion),
        }
    if not matches:
        repaired = answer.strip()
        expansion = insight_expansion_metrics(repaired, digest)
        return repaired, {
            **_trace(
                (),
                (),
                omitted=True,
                section_found=False,
                candidate_sentence_count=0,
                reject_reason_counts={},
            ),
            "source_data_count": source_data_count,
            "eligibility_replacement_sentence_count": 0,
            "supporting_fact_ids": [],
            "claim_levels": [],
            "claims": [],
            "claim_eligibility_met": False,
            "contract_met": False,
            "reason_code": "MISSING_REQUIRED_ROLE",
            **_expansion_trace_fields(expansion),
        }
    matched = matches[-1]
    insertion_index = matches[0].start()
    answer_without_insight = _INSIGHT_SECTION_RE.sub("", answer)
    duplicate_sections_collapsed = len(matches) - 1

    allowed_values = _digest_value_variants(digest)
    paragraphs: list[str] = []
    counts: list[int] = []
    removed = 0
    soft_degraded_sentence_count = 0
    candidate_sentence_count = 0
    reject_reason_counts: Counter[str] = Counter()
    raw_paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", matched.group("body"))
        if paragraph.strip()
    ]
    layer_counts = {"L1": 0, "L2": 0, "L3": 0}
    interpretation_sentence_count = 0
    l3_inference_sentence_count = 0
    shared_anchor = _has_digest_numeric_anchor(matched.group("body"), allowed_values)
    source_count = len({card.source for card in digest.cards if card.source})
    source_contract_met = source_count >= 1
    numeric_recitations: Counter[str] = Counter()
    supporting_fact_ids: list[tuple[str, ...]] = []
    prohibited_inference_counts: Counter[str] = Counter()
    for paragraph_index, paragraph in enumerate(raw_paragraphs):
        layer = ("L1", "L2", "L3")[min(paragraph_index, 2)]
        retained: list[str] = []
        raw_sentences = _split_sentences(paragraph.strip())
        legacy_summary = "연도별:" in paragraph
        for raw_sentence in raw_sentences:
            sentence = _ORPHAN_CONJUNCTION_RE.sub("", raw_sentence.strip())
            sentence = _NEGATIVE_ELAPSED_RE.sub(
                lambda match: (
                    f"{match.group('months')}개월{match.group('suffix')}"
                ),
                sentence,
            )
            sentence = _normalize_internal_field_labels(sentence)
            internal_metadata = bool(_DERIVED_FIELD_METADATA_RE.search(sentence))
            if not sentence:
                continue
            candidate_sentence_count += 1
            concrete_count = _concrete_value_count(sentence, allowed_values)
            is_interpretation = any(
                marker in sentence for marker in _INTERPRETATION_MARKERS
            )
            is_l3_inference = any(
                marker in sentence for marker in _L3_INFERENCE_MARKERS
            )
            reject_reason = (
                "legacy_summary_block"
                if legacy_summary
                else (
                    "reader_delegation"
                    if any(
                        term in sentence for term in _READER_DELEGATION_BLACKLIST
                    )
                    else (
                        "blacklist"
                        if any(term in sentence for term in _BLACKLIST)
                        else (
                            "enumeration_fallback"
                            if (len(raw_paragraphs) < 3 or layer != "L1")
                            and _ENUMERATION_FALLBACK_RE.search(sentence)
                            else (
                                "untyped_absence"
                                if _UNTYPED_ABSENCE_RE.search(sentence)
                                else (
                                    "unbound_clinical_field"
                                    if _has_unbound_clinical_field(sentence, digest)
                                    else (
                                        "unbound_status"
                                        if _has_unbound_status(sentence, allowed_values)
                                        else (
                                            "missing_l3_inference"
                                            if layer == "L3" and not is_l3_inference
                                            else (
                                                "missing_shared_anchor"
                                                if layer == "L3" and not shared_anchor
                                                else (
                                                    "missing_interpretation"
                                                    if layer == "L2" and not is_interpretation
                                                    else (
                                                        "missing_evidence"
                                                        if layer != "L3" and concrete_count < 2
                                                        else None
                                                    )
                                                )
                                            )
                                        )
                                    )
                                )
                            )
                        )
                    )
                )
            )
            prohibited_reason = _prohibited_inference_reason(sentence)
            if prohibited_reason is not None:
                reject_reason = prohibited_reason
                prohibited_inference_counts[prohibited_reason] += 1
            if _has_unbound_numeric_token(sentence, allowed_values):
                reject_reason = "unbound_numeric"
            recitation_keys = _numeric_recitation_keys(sentence)
            if reject_reason not in {"unbound_numeric", "reader_delegation"} and any(
                numeric_recitations[key] + count > 2
                for key, count in recitation_keys.items()
            ):
                reject_reason = "numeric_repetition"
            if internal_metadata:
                reject_reason = "internal_field_metadata"
            if reject_reason is not None:
                reject_reason_counts[reject_reason] += 1
                hard_reject_reasons = {
                    "legacy_summary_block",
                    "unbound_numeric",
                    "unbound_clinical_field",
                    "unbound_status",
                    "untyped_absence",
                    "reader_delegation",
                    "numeric_repetition",
                    "internal_field_metadata",
                    *tuple(
                        name for name, _pattern in _PROHIBITED_INFERENCE_PATTERNS
                    ),
                }
                if reject_reason in hard_reject_reasons or (
                    reject_reason == "blacklist" and concrete_count < 2
                ):
                    removed += 1
                    continue
                soft_degraded_sentence_count += 1
            retained.append(sentence)
            supporting_fact_ids.append(_supporting_fact_ids(sentence, digest))
            numeric_recitations.update(recitation_keys)
            counts.append(concrete_count)
            layer_counts[layer] += 1
            interpretation_sentence_count += int(is_interpretation)
            l3_inference_sentence_count += int(layer == "L3" and is_l3_inference)
        if retained:
            paragraphs.append(" ".join(retained))

    paragraph_sentences = [
        [sentence.strip() for sentence in _split_sentences(paragraph) if sentence.strip()]
        for paragraph in paragraphs
    ]
    sentence_count = sum(len(sentences) for sentences in paragraph_sentences)
    truncated_sentence_count = max(
        0, sentence_count - _EXPANSION_SENTENCE_MAXIMUM
    )
    excess = truncated_sentence_count
    while excess:
        removed_one = False
        for index, minimum in ((0, 1), (1, 1), (2, 2)):
            if index < len(paragraph_sentences) and len(paragraph_sentences[index]) > minimum:
                paragraph_sentences[index].pop()
                excess -= 1
                removed_one = True
                break
        if not removed_one:
            break
    paragraphs = [" ".join(sentences) for sentences in paragraph_sentences if sentences]
    while len(paragraph_sentences) < _EXPANSION_PARAGRAPH_TARGET:
        paragraph_sentences.append([])
    flattened_sentences = [sentence for sentences in paragraph_sentences for sentence in sentences]
    counts = [_concrete_value_count(sentence, allowed_values) for sentence in flattened_sentences]
    sentence_count = len(flattened_sentences)
    layer_counts = {
        layer: len(paragraph_sentences[index]) if index < len(paragraph_sentences) else 0
        for index, layer in enumerate(("L1", "L2", "L3"))
    }
    interpretation_sentence_count = sum(
        any(marker in sentence for marker in _INTERPRETATION_MARKERS)
        for sentence in (paragraph_sentences[1] if len(paragraph_sentences) > 1 else ())
    )
    l3_inference_sentence_count = sum(
        any(marker in sentence for marker in _L3_INFERENCE_MARKERS)
        for sentence in (paragraph_sentences[2] if len(paragraph_sentences) > 2 else ())
    )
    material_count = len(_digest_material_entries(digest))
    required_sentence_count = (
        _RICH_EXPANSION_SENTENCE_TARGET
        if material_count >= _RICH_EXPANSION_MATERIAL_MINIMUM
        else _EXPANSION_SENTENCE_TARGET
        if material_count >= _RICH_MATERIAL_MINIMUM
        else 1
    )
    replacement_sentence_count = 0
    preliminary_claim_levels = [
        layer
        for layer, sentences in zip(
            ("L1", "L2", "L3"),
            paragraph_sentences,
            strict=False,
        )
        for _sentence in sentences
    ]
    preliminary_claims = [
        _claim_trace(level, sentence, fact_ids, digest=digest)
        for level, sentence, fact_ids in zip(
            preliminary_claim_levels,
            flattened_sentences,
            supporting_fact_ids,
            strict=False,
        )
    ]
    ineligible_indexes = {
        index
        for index, claim in enumerate(preliminary_claims)
        if not claim["eligible"]
    }
    # Numeric fabrication, internal metadata, and prohibited inference were
    # already removed above.  Eligibility failures at this point mean the
    # replacement layer could not bind a surviving sentence more narrowly;
    # retaining that sentence is preferable to deleting the entire insight.
    claim_eligibility_failure_count = len(ineligible_indexes)

    # An unmet model insight remains unmet here so runtime can spend its one
    # semantic repair attempt. Deterministic supplementation is strictly the
    # final post-repair fallback in expand_s17_insight_from_digest().

    paragraphs = [
        " ".join(sentences) for sentences in paragraph_sentences if sentences
    ]
    flattened_sentences = [
        sentence
        for sentences in paragraph_sentences
        for sentence in sentences
        if sentence
    ]
    counts = [
        _concrete_value_count(sentence, allowed_values)
        for sentence in flattened_sentences
    ]
    sentence_count = len(flattened_sentences)
    layer_counts = {
        layer: len(paragraph_sentences[index])
        if index < len(paragraph_sentences)
        else 0
        for index, layer in enumerate(("L1", "L2", "L3"))
    }
    interpretation_sentence_count = sum(
        any(marker in sentence for marker in _INTERPRETATION_MARKERS)
        for sentence in paragraph_sentences[1]
    )
    l3_inference_sentence_count = sum(
        any(marker in sentence for marker in _L3_INFERENCE_MARKERS)
        for sentence in paragraph_sentences[2]
    )
    shared_anchor = _has_digest_numeric_anchor(
        " ".join(flattened_sentences), allowed_values
    )
    contract_met = bool(
        required_sentence_count
        <= sentence_count
        <= _EXPANSION_SENTENCE_MAXIMUM
        and len(paragraphs) == 3
        and layer_counts["L1"] >= 1
        and layer_counts["L2"] >= 1
        and layer_counts["L3"] >= (2 if required_sentence_count >= 10 else 0)
        and interpretation_sentence_count >= 1
        and l3_inference_sentence_count
        >= (2 if required_sentence_count >= 10 else 0)
        and shared_anchor
        and source_contract_met
    )
    reason_code = (
        "융합 추론 누락"
        if l3_inference_sentence_count < 2
        else "MISSING_EVIDENCE"
    )
    if not contract_met and not paragraphs:
        sanitized = answer_without_insight.strip()
        expansion = insight_expansion_metrics(sanitized, digest)
        return sanitized, {
            **_trace(paragraphs, counts, omitted=True),
            "removed_sentence_count": removed,
            "section_found": True,
            "candidate_sentence_count": candidate_sentence_count,
            "retained_sentence_count": sentence_count,
            "truncated_sentence_count": truncated_sentence_count,
            "duplicate_sections_collapsed": duplicate_sections_collapsed,
            "reject_reason_counts": dict(sorted(reject_reason_counts.items())),
            "layer_sentence_counts": layer_counts,
            "interpretation_sentence_count": interpretation_sentence_count,
            "l3_inference_sentence_count": l3_inference_sentence_count,
            "l3_shared_anchor": shared_anchor,
            "source_count": source_count,
            "source_contract_met": source_contract_met,
            "source_data_count": source_data_count,
            "reason_code": reason_code,
            "soft_degraded": False,
            "soft_degraded_sentence_count": soft_degraded_sentence_count,
            "claim_eligibility_failure_count": claim_eligibility_failure_count,
            **_expansion_trace_fields(expansion),
        }

    body = "\n\n".join(paragraphs)
    prefix = answer_without_insight[:insertion_index].rstrip()
    suffix = answer_without_insight[insertion_index:].lstrip()
    sanitized = "\n\n".join(
        part
        for part in (prefix, f"## 종합 인사이트\n{body}", suffix)
        if part
    ).strip()
    expansion = insight_expansion_metrics(sanitized, digest)
    if contract_met and expansion["expansion_target_met"] is not True:
        contract_met = False
        reason_code = "EXPANSION_TARGET_UNMET"
    claim_levels = [
        layer
        for layer, paragraph in zip(("L1", "L2", "L3"), paragraph_sentences)
        for _sentence in paragraph
    ]
    claims = [
        _claim_trace(level, sentence, fact_ids, digest=digest)
        for level, sentence, fact_ids in zip(
            claim_levels,
            flattened_sentences,
            supporting_fact_ids,
            strict=False,
        )
    ]
    return sanitized, {
        **_trace(paragraphs, counts, omitted=False),
        "contract_met": contract_met,
        "omitted": False,
        "soft_degraded": not contract_met,
        "soft_degraded_sentence_count": soft_degraded_sentence_count,
        "removed_sentence_count": removed,
        "section_found": True,
        "candidate_sentence_count": candidate_sentence_count,
        "retained_sentence_count": sentence_count,
        "truncated_sentence_count": truncated_sentence_count,
        "duplicate_sections_collapsed": duplicate_sections_collapsed,
        "reject_reason_counts": dict(sorted(reject_reason_counts.items())),
        "layer_sentence_counts": layer_counts,
        "interpretation_sentence_count": interpretation_sentence_count,
        "l3_inference_sentence_count": l3_inference_sentence_count,
        "l3_shared_anchor": shared_anchor,
        "source_count": source_count,
        "source_contract_met": source_contract_met,
        "source_data_count": source_data_count,
        "reason_code": (
            None
            if contract_met
            else "FACT_DIGEST_BINDING_INCOMPLETE"
            if not digest.cards
            else reason_code
        ),
        "eligibility_replacement_sentence_count": replacement_sentence_count,
        "supporting_fact_ids": supporting_fact_ids,
        "claim_levels": claim_levels,
        "claims": claims,
        "claim_eligibility_met": bool(
            supporting_fact_ids
            and all(claim["eligible"] for claim in claims)
        ),
        "claim_eligibility_failure_count": claim_eligibility_failure_count,
        "prohibited_inference_counts": dict(
            sorted(prohibited_inference_counts.items())
        ),
        **_expansion_trace_fields(expansion),
    }


def replace_s17_insight_section(
    base_answer: str,
    repaired_answer: str,
    digest: FactDigest,
    *,
    require_expansion_target: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Replace the insight with every grounded sentence that survives S17."""
    sanitized_repair, trace = sanitize_s17_insight(repaired_answer, digest)
    repaired_match = _INSIGHT_SECTION_RE.search(sanitized_repair)
    if trace.get("omitted", True) or repaired_match is None:
        return base_answer, {**trace, "replacement_applied": False}
    if require_expansion_target and trace.get("expansion_target_met") is not True:
        return base_answer, {
            **trace,
            "replacement_applied": False,
            "rejection_reason": "expansion_target_not_met",
        }

    normalized_base = _normalize_section_headings(base_answer)
    base_matches = tuple(_INSIGHT_SECTION_RE.finditer(normalized_base))
    insertion_index = base_matches[0].start() if base_matches else len(normalized_base)
    without_insight = _INSIGHT_SECTION_RE.sub("", normalized_base)
    insertion_index = min(insertion_index, len(without_insight))
    repaired_section = repaired_match.group(0).strip()
    prefix = without_insight[:insertion_index].rstrip()
    suffix = without_insight[insertion_index:].lstrip()
    merged = "\n\n".join(
        part for part in (prefix, repaired_section, suffix) if part
    )
    return merged.strip(), {
        **trace,
        "replacement_applied": True,
        "base_sections_removed": len(base_matches),
    }


def promote_context_to_s17_insight(
    answer: str,
    digest: FactDigest,
) -> tuple[str, dict[str, Any]]:
    """Guarantee an insight surface by promoting already-rendered grounded context."""

    normalized = _normalize_section_headings(answer)
    if _source_data_count(digest) <= 0:
        return normalized, {
            "applied": False,
            "reason": "no_source_data",
            "source_data_count": 0,
        }
    if _INSIGHT_SECTION_RE.search(normalized) is not None:
        return normalized, {
            "applied": False,
            "reason": "insight_already_present",
            "source_data_count": _source_data_count(digest),
        }
    context_match = _CONTEXT_SECTION_RE.search(normalized)
    if context_match is None:
        paragraphs, _fact_ids = _grounded_replacement_paragraphs(
            digest,
            target_sentence_count=3,
        )
        if not paragraphs:
            source_labels = tuple(
                dict.fromkeys(
                    _source_display(source)
                    for source, count in digest.source_received_counts.items()
                    if count > 0
                )
            )
            paragraphs = (
                (
                    f"{', '.join(source_labels)} 조회 결과를 수신했지만 "
                    "구조화된 해석 근거로 연결하지 못했습니다."
                ),
            ) if source_labels else ()
        if not paragraphs:
            return normalized, {
                "applied": False,
                "reason": "context_and_deterministic_fallback_missing",
                "source_data_count": _source_data_count(digest),
            }
        insight = "## 종합 인사이트\n" + "\n\n".join(paragraphs)
        return f"{normalized.rstrip()}\n\n{insight}".strip(), {
            "applied": True,
            "reason": "bounded_deterministic_fallback_after_missing_context",
            "source_data_count": _source_data_count(digest),
            "promoted_paragraph_count": len(paragraphs),
            "promoted_sentence_count": sum(
                len(_split_sentences(paragraph)) for paragraph in paragraphs
            ),
        }

    retained: list[str] = []
    seen: set[str] = set()
    for paragraph in re.split(r"\n\s*\n", context_match.group("body")):
        sentences: list[str] = []
        for raw in _split_sentences(paragraph.strip()):
            sentence = _normalize_internal_field_labels(
                _ORPHAN_CONJUNCTION_RE.sub("", raw.strip())
            )
            normalized_sentence = _normalize_candidate_sentence(sentence)
            if (
                not sentence
                or normalized_sentence in seen
                or _DERIVED_FIELD_METADATA_RE.search(sentence)
                or "문서 요약 chunks" in sentence.casefold()
                or _looks_like_raw_english_chunk(sentence)
            ):
                continue
            seen.add(normalized_sentence)
            sentences.append(sentence)
        if sentences:
            retained.append(" ".join(sentences))
    if not retained:
        return normalized, {
            "applied": False,
            "reason": "context_has_no_promotable_sentences",
            "source_data_count": _source_data_count(digest),
        }

    insight = "## 종합 인사이트\n" + "\n\n".join(retained)
    return f"{normalized.rstrip()}\n\n{insight}".strip(), {
        "applied": True,
        "reason": "grounded_context_promoted_without_revalidation",
        "source_data_count": _source_data_count(digest),
        "promoted_paragraph_count": len(retained),
        "promoted_sentence_count": sum(
            len(_split_sentences(paragraph)) for paragraph in retained
        ),
    }


def _looks_like_raw_english_chunk(sentence: str) -> bool:
    letters = re.findall(r"[A-Za-z]", sentence)
    korean = re.findall(r"[가-힣]", sentence)
    return len(letters) >= 80 and len(letters) > max(1, len(korean) * 3)


def expand_s17_insight_from_digest(
    answer: str,
    digest: FactDigest,
    *,
    target_sentence_count: int | None = None,
    repair_failed: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Append a bounded, final deterministic fallback after model repair."""

    input_metrics = insight_expansion_metrics(answer, digest)
    sanitized, _initial_trace = sanitize_s17_insight(answer, digest)
    promotion_trace: dict[str, Any] = {
        "applied": False,
        "reason": "not_needed",
    }
    if _INSIGHT_SECTION_RE.search(sanitized) is None and _source_data_count(digest) > 0:
        sanitized, promotion_trace = promote_context_to_s17_insight(
            sanitized,
            digest,
        )
    before_metrics = insight_expansion_metrics(sanitized, digest)
    if target_sentence_count is None:
        target_sentence_count = max(
            _EXPANSION_SENTENCE_TARGET,
            int(before_metrics["required_sentence_count"]),
        )
    input_sentence_count = input_metrics["sentence_count"]
    pre_fallback_sentence_count = before_metrics["sentence_count"]
    base_trace = {
        "attempted": False,
        "applied": False,
        "before_sentence_count": pre_fallback_sentence_count,
        "after_sentence_count": pre_fallback_sentence_count,
        "input_sentence_count": input_sentence_count,
        "pre_fallback_sentence_count": input_sentence_count,
        "post_validation_sentence_count": pre_fallback_sentence_count,
        "added_sentence_count": 0,
        "deterministic_sentence_count": 0,
        "deterministic_sentence_ratio": 0.0,
        "deterministic_sentences": (),
        "deterministic_sentence_limit": 3,
        "context_promotion": promotion_trace,
        "candidate_count": 0,
        "accepted_candidate_count": 0,
        "rejected_candidate_count": 0,
        "expansion_target_met": before_metrics["expansion_target_met"],
    }
    if not before_metrics["data_rich"]:
        return sanitized, {**base_trace, "reason": "digest_not_data_rich"}
    if before_metrics["sentence_count"] >= target_sentence_count:
        reason = (
            "target_already_met"
            if before_metrics["expansion_target_met"] is True
            else "expansion_target_unmet"
        )
        return sanitized, {**base_trace, "reason": reason}

    required_sentence_count = max(
        0,
        target_sentence_count - pre_fallback_sentence_count,
    )
    if required_sentence_count <= 0:
        return sanitized, {
            **base_trace,
            "attempted": True,
            "reason": "target_already_met",
        }
    deterministic_budget = min(
        required_sentence_count,
        max(1, (target_sentence_count * 3) // 10),
        3,
    )

    inference_candidates = _deterministic_inference_candidates(digest)
    sparse_timeout_fallback = repair_failed and pre_fallback_sentence_count <= 3
    candidates = tuple(
        dict.fromkeys(
            (
                *(
                    _layer_material_candidates(digest, 2)
                    if sparse_timeout_fallback
                    else ()
                ),
                *inference_candidates,
                *_fallback_inference_candidates(digest),
            )
        )
    )
    current = sanitized
    survivors = _insight_sentences(current)
    paragraphs = _normalized_layer_sentences(current)
    numeric_counts: Counter[str] = Counter()
    for sentence in survivors:
        numeric_counts.update(_numeric_recitation_keys(sentence))
    seen = {_normalize_candidate_sentence(sentence) for sentence in survivors}
    used_material_keys = {
        key
        for sentence in survivors
        for key in _substantive_material_keys(sentence, digest)
    }
    accepted_sentences: list[str] = []
    rejected = 0

    for candidate in candidates:
        if len(accepted_sentences) >= deterministic_budget:
            break
        normalized_candidate = _normalize_candidate_sentence(candidate)
        if not normalized_candidate or normalized_candidate in seen:
            continue
        recitation_keys = _numeric_recitation_keys(candidate)
        material_keys = _substantive_material_keys(candidate, digest)
        new_material_keys = set(material_keys).difference(used_material_keys)
        if (
            not material_keys
            or (sparse_timeout_fallback and not new_material_keys)
            or (
                not sparse_timeout_fallback
                and bool(used_material_keys.intersection(material_keys))
            )
        ):
            rejected += 1
            continue
        if any(
            numeric_counts[key] + count > 2
            for key, count in recitation_keys.items()
        ):
            rejected += 1
            continue
        proposed_paragraphs = [list(layer) for layer in paragraphs]
        proposed_paragraphs[-1].append(candidate)
        proposed = _replace_insight_body(
            current,
            tuple(" ".join(layer) for layer in proposed_paragraphs),
        )
        validated, validation_trace = sanitize_s17_insight(proposed, digest)
        if (
            all(sentence in validated for sentence in survivors)
            and candidate in validated
            and not validation_trace.get("reject_reason_counts", {}).get(
                "unbound_numeric", 0
            )
        ):
            current = validated
            paragraphs = _normalized_layer_sentences(current)
            accepted_sentences.append(candidate)
            seen.add(normalized_candidate)
            numeric_counts.update(recitation_keys)
            used_material_keys.update(material_keys)
        else:
            rejected += 1

    after_metrics = insight_expansion_metrics(current, digest)
    accepted = len(accepted_sentences)
    final_sentence_count = after_metrics["sentence_count"]
    deterministic_ratio = (
        accepted / final_sentence_count if final_sentence_count else 0.0
    )
    return current, {
        **base_trace,
        "attempted": True,
        "applied": accepted > 0,
        "reason": (
            "target_met"
            if after_metrics["sentence_count"] >= target_sentence_count
            else "bounded_fallback_exhausted"
        ),
        "after_sentence_count": after_metrics["sentence_count"],
        "added_sentence_count": accepted,
        "deterministic_sentence_count": accepted,
        "deterministic_sentence_ratio": round(deterministic_ratio, 4),
        "deterministic_sentences": tuple(accepted_sentences),
        "candidate_count": len(candidates),
        "accepted_candidate_count": accepted,
        "rejected_candidate_count": rejected,
        "expansion_target_met": after_metrics["expansion_target_met"],
    }


def _prohibited_inference_reason(sentence: str) -> str | None:
    normalized = re.sub(r"\s+", " ", sentence).strip()
    for name, pattern in _PROHIBITED_INFERENCE_PATTERNS:
        if pattern.search(normalized):
            return name
    return None


def _supporting_fact_ids(
    sentence: str,
    digest: FactDigest,
) -> tuple[str, ...]:
    normalized = sentence.casefold()
    fact_ids = {
        f"{entry['source']}:{entry['path']}"
        for entry in _digest_material_entries(digest)
        if _material_is_used(normalized, entry["value"])
    }
    return tuple(sorted(fact_ids))


def _supporting_material_keys(
    sentence: str,
    digest: FactDigest,
) -> tuple[tuple[str, str, str], ...]:
    """Return the exact source, field, and value tuples used by a sentence."""
    normalized = sentence.casefold()
    return tuple(
        sorted(
            {
                (
                    str(entry["source"]),
                    str(entry["path"]),
                    str(entry["value"]).casefold(),
                )
                for entry in _digest_material_entries(digest)
                if _material_is_used(normalized, entry["value"])
            }
        )
    )


def _claim_eligibility_reason(
    claim_level: str,
    sentence: str,
    supporting_fact_ids: Sequence[str],
    *,
    digest: FactDigest | None = None,
) -> str | None:
    """Return why a claim is ineligible, keeping every level evidence-bound."""
    prohibited = _prohibited_inference_reason(sentence)
    if prohibited is not None:
        return prohibited
    if not supporting_fact_ids:
        return "missing_supporting_fact"
    scope_reason = _scope_lock_reason(supporting_fact_ids, digest)
    if scope_reason is not None:
        return scope_reason
    if not any(
        _is_substantive_fact_id(fact_id) for fact_id in supporting_fact_ids
    ):
        return "missing_substantive_evidence"
    if claim_level == "L2":
        comparison_anchors = {
            match.group(0)
            for match in _NUMERIC_TOKEN_RE.finditer(sentence)
        }
        comparison_anchors.update(
            term for term in _STATUS_TERMS if term in sentence
        )
        received_sources = {
            _scope_source(source)
            for source, count in (digest.source_received_counts.items() if digest else ())
            if int(count) > 0
        }
        supporting_sources = {
            _scope_source(fact_id.split(":", 1)[0])
            for fact_id in supporting_fact_ids
            if ":" in fact_id
        }
        minimum_anchors = (
            1
            if supporting_sources and supporting_sources <= received_sources
            else 2
        )
        if len(comparison_anchors) < minimum_anchors:
            return "missing_comparison_basis"
        if not any(marker in sentence for marker in _INTERPRETATION_MARKERS):
            return "missing_interpretation"
    if claim_level == "L3":
        if not any(marker in sentence for marker in _L3_INFERENCE_MARKERS):
            return "missing_inference_marker"
        if not any(marker in sentence for marker in _L3_ACTOR_MARKERS):
            return "missing_inference_actor"
        if not any(marker in sentence for marker in _L3_CHOICE_MARKERS):
            return "missing_inference_choice"
    return None


def _is_substantive_fact_id(fact_id: str) -> bool:
    path = fact_id.split(":", 1)[-1]
    if not _is_public_material_path(path):
        return False
    return path not in {
        "entity",
        "metric",
        "unit",
        "representative.source",
    }


def _is_public_material_path(path: str) -> bool:
    return not any(
        component in {"evidence_id", "evidence_ids"}
        for component in path.split(".")
    )


def _scope_lock_reason(
    supporting_fact_ids: Sequence[str],
    digest: FactDigest | None,
) -> str | None:
    """Keep every received lane eligible; relevance is decided at synthesis time."""
    if digest is not None:
        received_sources = {
            _scope_source(source)
            for source, count in digest.source_received_counts.items()
            if int(count) > 0
        }
        supporting_sources = {
            _scope_source(fact_id.split(":", 1)[0])
            for fact_id in supporting_fact_ids
            if ":" in fact_id
        }
        if supporting_sources and supporting_sources <= received_sources:
            return None
    if digest is None or digest.answer_contract is None:
        return None
    required_sources = {
        _scope_source(source) for source in digest.answer_contract.required_sources
    }
    if not required_sources:
        return None
    supporting_sources = {
        _scope_source(fact_id.split(":", 1)[0])
        for fact_id in supporting_fact_ids
        if ":" in fact_id
    }
    if supporting_sources.isdisjoint(required_sources):
        return "scope_lock_missing_required_source"

    required_entities = tuple(
        key
        for entity in digest.answer_contract.required_entities
        if (key := _scope_entity_key(entity))
    )
    if not required_entities:
        return None
    optional_sources = supporting_sources - required_sources
    for source in optional_sources:
        source_cards = tuple(
            card for card in digest.cards if _scope_source(card.source) == source
        )
        if not source_cards or not any(
            _scope_entities_match(required_entities, card.entity)
            for card in source_cards
        ):
            return "scope_lock_entity_mismatch"
    return None


def _scope_source(source: str) -> str:
    if source in {"document_sql", "document_vdb", "file_sql", "file_vdb"}:
        return "document"
    return source


def _scope_entities_match(required_entities: Sequence[str], observed: str | None) -> bool:
    observed_key = _scope_entity_key(observed)
    return bool(observed_key and observed_key in required_entities)


def _scope_entity_key(value: str | None) -> str:
    normalized = re.sub(r"[^0-9a-z가-힣]+", "", str(value or "").casefold())
    normalized = re.sub(
        r"(?:구강붕해정|서방정|캡슐|시럽|크림|연고|패치|주사|정|주|액)\d.*$",
        "",
        normalized,
    )
    normalized = re.sub(r"시장$", "", normalized)
    return normalized


def _claim_trace(
    claim_level: str,
    sentence: str,
    supporting_fact_ids: Sequence[str],
    *,
    digest: FactDigest | None = None,
) -> dict[str, Any]:
    reason = _claim_eligibility_reason(
        claim_level,
        sentence,
        supporting_fact_ids,
        digest=digest,
    )
    return {
        "claim_level": claim_level,
        "supporting_fact_ids": tuple(supporting_fact_ids),
        "eligible": reason is None,
        "ineligibility_reason": reason,
    }


def _grounded_replacement_paragraphs(
    digest: FactDigest,
    *,
    target_sentence_count: int,
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Replace rejected prose with digest-bound facts, comparisons, and inferences."""
    if target_sentence_count <= 0:
        return (), ()
    if not digest.cards:
        source_counts = tuple(
            (source, max(0, int(count)))
            for source, count in digest.source_received_counts.items()
            if int(count) > 0
        )
        if not source_counts:
            return (), ()
        received = ", ".join(
            f"{_source_display(source)} {count}건"
            for source, count in source_counts
        )
        sentence = (
            f"{received}을 수신했지만 답변 근거 결속이 완료되지 않아 "
            "비교·추세 해석 재료를 구성하지 못했습니다."
        )
        fact_ids = tuple(
            f"{source}:source_received_counts" for source, _count in source_counts
        )
        return (sentence,), (fact_ids,)

    sentence_target = min(3, max(1, (target_sentence_count * 3) // 10))
    candidate_layers = (
        _interleave_candidate_groups(
            tuple(_card_fact_candidates(card) for card in digest.cards)
        ),
        (
            *_interleave_candidate_groups(
                tuple(
                    tuple(_card_expansion_candidates(card))
                    for card in digest.cards
                )
            ),
            *_cross_source_expansion_candidates(digest.cards),
        ),
        (
            *_cross_source_inference_candidates(digest.cards),
            *_interleave_candidate_groups(
                tuple(
                    tuple(_card_inference_candidates(card))
                    for card in digest.cards
                )
            ),
        ),
    )
    eligible_l2_count = len(
        {
            _normalize_candidate_sentence(candidate)
            for candidate in (
                *candidate_layers[1],
                *_layer_material_candidates(digest, 1),
            )
            if (
                support := _supporting_fact_ids(candidate, digest)
            )
            and _claim_eligibility_reason(
                "L2", candidate, support, digest=digest
            )
            is None
        }
    )
    l1_target = 1
    l2_target = min(1, eligible_l2_count) if sentence_target >= 2 else 0
    layer_targets = (
        l1_target,
        l2_target,
        max(0, sentence_target - l1_target - l2_target),
    )
    paragraphs = ["", "", ""]
    all_fact_ids: list[tuple[str, ...]] = []
    seen: set[str] = set()
    used_material_keys: set[tuple[str, str, str]] = set()
    numeric_recitations: Counter[str] = Counter()
    for layer_index, (candidates, layer_target) in enumerate(
        zip(candidate_layers, layer_targets, strict=True)
    ):
        claim_level = ("L1", "L2", "L3")[layer_index]
        accepted: list[str] = []
        for candidate in candidates:
            normalized = _normalize_candidate_sentence(candidate)
            support = _supporting_fact_ids(candidate, digest)
            material_keys = _substantive_material_keys(candidate, digest)
            recitation_keys = _numeric_recitation_keys(candidate)
            if (
                not support
                or not material_keys
                or bool(used_material_keys.intersection(material_keys))
                or normalized in seen
                or any(
                    numeric_recitations[key] + count > 2
                    for key, count in recitation_keys.items()
                )
                or _claim_eligibility_reason(
                    claim_level,
                    candidate,
                    support,
                    digest=digest,
                )
                is not None
            ):
                continue
            accepted.append(candidate)
            all_fact_ids.append(support)
            seen.add(normalized)
            numeric_recitations.update(recitation_keys)
            used_material_keys.update(material_keys)
            if len(accepted) >= layer_target:
                break
        if len(accepted) < layer_target:
            fillers = _layer_material_candidates(digest, layer_index)
            for candidate in fillers:
                normalized = _normalize_candidate_sentence(candidate)
                support = _supporting_fact_ids(candidate, digest)
                material_keys = _substantive_material_keys(candidate, digest)
                recitation_keys = _numeric_recitation_keys(candidate)
                if (
                    not support
                    or not material_keys
                    or bool(used_material_keys.intersection(material_keys))
                    or normalized in seen
                    or any(
                        numeric_recitations[key] + count > 2
                        for key, count in recitation_keys.items()
                    )
                    or _claim_eligibility_reason(
                        claim_level,
                        candidate,
                        support,
                        digest=digest,
                    )
                    is not None
                ):
                    continue
                accepted.append(candidate)
                all_fact_ids.append(support)
                seen.add(normalized)
                numeric_recitations.update(recitation_keys)
                used_material_keys.update(material_keys)
                if len(accepted) >= layer_target:
                    break
        if accepted:
            paragraphs[layer_index] = " ".join(accepted)

    if not any(paragraphs):
        return (), ()
    return tuple(paragraphs), tuple(all_fact_ids)


def _interleave_candidate_groups(
    groups: Sequence[Sequence[str]],
) -> tuple[str, ...]:
    if not groups:
        return ()
    maximum = max((len(group) for group in groups), default=0)
    return tuple(
        group[index]
        for index in range(maximum)
        for group in groups
        if index < len(group)
    )


def _card_fact_candidates(card: DerivedCoreCard) -> tuple[str, ...]:
    source = _source_display(card.source)
    representative_source = card.representative.get("source")
    if isinstance(representative_source, str) and representative_source.strip():
        source = f"{source}({representative_source.strip()})"
    entity = card.entity or "조회 대상"
    metric = card.metric or "지표"
    unit = card.unit or ""
    candidates: list[str] = []
    if card.card_type == "patent":
        patent_no = card.representative.get("patent_no")
        status = card.representative.get("status")
        expiration = card.representative.get("expiration_date")
        if patent_no and status and expiration:
            candidates.append(
                f"{source}의 {entity} 특허번호 {patent_no}는 {status} 상태이며 "
                f"존속기간 만료일은 {expiration}입니다."
            )
    anchor = _card_anchor(card)
    if anchor is not None:
        candidates.append(f"{source} 기준 질문의 직접 근거는 {anchor[1]}입니다.")
    for entry in sorted(
        _card_material_entries(card),
        key=lambda item: _material_priority(str(item["path"])),
    ):
        path = str(entry["path"])
        if path in _NON_SUBSTANTIVE_MATERIAL_PATHS or _is_raw_document_path(path):
            continue
        value = _material_value(entry["value"])
        label = _material_label(path)
        suffix = unit if _is_number(entry["value"]) else ""
        if card.card_type == "file_aggregate" and label == "값":
            candidates.append(f"{source}의 {entity} {metric}은 {value}{suffix}입니다.")
        else:
            candidates.append(
                f"{source}의 {entity} {metric} {label} 값은 {value}{suffix}입니다."
            )
    return tuple(dict.fromkeys(candidates))


def _material_priority(path: str) -> tuple[int, str]:
    priorities = {
        "representative.value": 0,
        "representative.code": 1,
        "period": 2,
        "full_stats.latest_value": 3,
        "full_stats.first_value": 4,
        "full_stats.change_value": 5,
        "full_stats.female_outpatient": 6,
        "full_stats.male_outpatient": 7,
        "full_stats.female_inpatient": 8,
        "full_stats.male_inpatient": 9,
    }
    return priorities.get(path, 20), path


def _layer_material_candidates(
    digest: FactDigest,
    layer_index: int,
) -> tuple[str, ...]:
    candidate_groups: list[tuple[str, ...]] = []
    for card in digest.cards:
        candidates: list[str] = []
        source = _source_display(card.source)
        entity = card.entity or "조회 대상"
        metric = card.metric or "지표"
        for entry in _card_material_entries(card):
            path = str(entry["path"])
            if path in _NON_SUBSTANTIVE_MATERIAL_PATHS or _is_raw_document_path(path):
                continue
            fact = f"{_material_label(path)} {_material_value(entry['value'])}"
            if layer_index == 2:
                candidates.append(
                    f"{source}의 {entity} {metric} {fact} 근거에 따라 MI팀의 "
                    "후속 검토 범위가 달라질 가능성이 있습니다."
                )
            elif layer_index == 1:
                candidates.append(
                    f"{source}의 {entity} {metric} {fact}는 같은 자료원 안에서 "
                    "후속 비교에 사용할 기준을 보여줍니다."
                )
            elif layer_index == 0:
                candidates.append(
                    f"{source}의 {entity} {metric}에서 {fact}가 기록됐습니다."
                )
        candidate_groups.append(tuple(dict.fromkeys(candidates)))
    return _interleave_candidate_groups(candidate_groups)


def _is_raw_document_path(path: str) -> bool:
    return path.startswith(_RAW_DOCUMENT_MATERIAL_PREFIXES)


def _substantive_material_keys(
    sentence: str,
    digest: FactDigest,
) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        key
        for key in _supporting_material_keys(sentence, digest)
        if key[1] not in _NON_SUBSTANTIVE_MATERIAL_PATHS
        and not _is_raw_document_path(key[1])
    )


def _card_material_entries(card: DerivedCoreCard) -> list[dict[str, Any]]:
    digest = FactDigest(
        question="",
        answer_type=card.card_type,
        cards=(card,),
        source_received_counts={card.source: max(1, card.received_count)},
        source_visible_counts={card.source: max(1, card.visible_count)},
    )
    return _digest_material_entries(digest)


def _material_label(path: str) -> str:
    token = path.rsplit(".", 1)[-1]
    labels = {
        "entity": "대상",
        "metric": "지표",
        "period": "기간",
        "unit": "단위",
        "value": "값",
        "source": "자료원",
        "periods": "기간 목록",
        "values": "기간별 값",
        "first_value": "시작값",
        "latest_value": "최신값",
        "change_value": "변화값",
        "female_outpatient": "외래 여성",
        "male_outpatient": "외래 남성",
        "female_inpatient": "입원 여성",
        "male_inpatient": "입원 남성",
        "disease_name": "상병명",
        **_INSIGHT_FIELD_LABELS,
    }
    return labels.get(token, token.replace("_", " "))


def _normalize_internal_field_labels(sentence: str) -> str:
    normalized = sentence
    for field, label in _INSIGHT_FIELD_LABELS.items():
        normalized = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(field)}(?![A-Za-z0-9_])",
            label,
            normalized,
            flags=re.IGNORECASE,
        )
    return normalized


def _material_value(value: Any) -> str:
    if _is_number(value):
        return _display_number(value)
    return str(value).strip()


def _normalized_layer_sentences(answer: str) -> list[list[str]]:
    layers = [
        [sentence.strip() for sentence in _split_sentences(paragraph) if sentence.strip()]
        for paragraph in _insight_paragraphs(answer)
    ]
    if len(layers) > _EXPANSION_PARAGRAPH_TARGET:
        layers = [*layers[:2], [sentence for layer in layers[2:] for sentence in layer]]
    while len(layers) < _EXPANSION_PARAGRAPH_TARGET:
        layers.append([])
    return layers


def _normalize_candidate_sentence(sentence: str) -> str:
    return re.sub(r"\s+", " ", sentence).strip().casefold()


def _deterministic_inference_candidates(digest: FactDigest) -> tuple[str, ...]:
    inferences: list[str] = _cross_source_inference_candidates(digest.cards)
    for card in digest.cards:
        inferences.extend(_card_inference_candidates(card))
    return tuple(dict.fromkeys(candidate for candidate in inferences if candidate))


def _card_inference_candidates(card: DerivedCoreCard) -> list[str]:
    source = _source_display(card.source)
    anchor = _card_anchor(card)
    if anchor is None:
        return []
    choices = {
        "disease": (
            "환자 구성에 맞춘 치료 접근과 채널 전략",
            "질환 부담을 반영한 처방 수요 분석 전략",
            "환자군별 격차에 대응하는 시장 세분화 전략",
        ),
        "market": (
            "기간별 실적을 반영한 경쟁 대응 전략",
            "매출 구조에 따른 브랜드별 자원 배분 전략",
            "시장 흐름을 반영한 후속 성장 전략",
        ),
        "patent": (
            "권리 상태와 만료 지형에 따른 경쟁 대응 전략",
            "등재 특허 구성에 따른 제품 방어 전략",
            "특허별 상태 차이를 반영한 진입 전략",
        ),
        "clinical": (
            "개발 단계와 모집 상태를 반영한 경쟁 대응 전략",
            "스폰서 구성에 따른 임상 개발 분석 전략",
            "후기 단계 비중을 반영한 상용화 대응 전략",
        ),
        "file_aggregate": (
            "집계 구조에 따른 제품·채널별 자원 배분 전략",
            "상위 항목 집중도를 반영한 포트폴리오 전략",
            "기간과 구성 차이를 반영한 후속 매출 전략",
        ),
        "document_summary": (
            "문서의 대상 범위와 지표 정의를 반영한 시장 분석 전략",
            "문서 사실과 후속 경쟁 자료를 잇는 비교 전략",
            "질문과 직접 맞닿은 문서 근거 중심의 분석 전략",
        ),
    }.get(card.card_type, ())
    statements = (
        "{choice}을 우선 검토할 가능성이 있습니다",
        "{choice}의 우선순위를 높일 것으로 추정됩니다",
        "{choice}을 앞세울 구도로 해석될 수 있습니다",
    )
    candidates = [
        f"{source}의 {anchor[1]} 근거를 바탕으로 MI팀은 "
        f"{statement.format(choice=choice)}."
        for choice, statement in zip(choices, statements, strict=False)
    ]
    periods = card.full_stats.get("periods")
    if (
        card.card_type == "market"
        and isinstance(periods, Sequence)
        and not isinstance(periods, (str, bytes))
        and len(periods) >= 2
    ):
        candidates.append(
            f"{source}의 {periods[0]}와 {periods[-1]} 기간 차이에 따라 MI팀의 "
            "후속 검토 범위가 달라질 가능성이 있습니다."
        )
        representative_source = card.representative.get("source")
        if isinstance(representative_source, str) and representative_source.strip():
            candidates.append(
                f"{source}({representative_source.strip()})의 {periods[-1]} 기준을 "
                "바탕으로 MI팀은 다음 분기 비교 시점의 우선순위를 높일 가능성이 있습니다."
            )
    return candidates


def _cross_source_inference_candidates(
    cards: Sequence[DerivedCoreCard],
) -> list[str]:
    anchors = [(card, anchor) for card in cards if (anchor := _card_anchor(card))]
    candidates: list[str] = []
    for index, (first_card, first) in enumerate(anchors):
        for second_card, second in anchors[index + 1 :]:
            if first[0] == second[0]:
                continue
            first_source = _source_display(first[0])
            second_source = _source_display(second[0])
            candidates.extend(
                (
                    (
                        f"{first[1]}, {second[1]} 두 관찰값을 함께 보면 MI팀은 "
                        f"{first_source}·{second_source} 근거를 잇는 시장 대응 전략을 "
                        "우선 검토할 구도로 해석될 수 있습니다."
                    ),
                    (
                        f"{first_card.entity or first_card.metric}·"
                        f"{second_card.entity or second_card.metric} 결합은 서로 다른 "
                        "원천의 변화가 MI팀의 경쟁 대응 우선순위에 영향을 줄 가능성이 있습니다."
                    ),
                    (
                        f"{first_source}의 {first_card.metric or '지표'} 지표와 "
                        f"{second_source}의 {second_card.metric or '지표'} 지표를 종합하면 "
                        "MI팀의 후속 전략과 분석 대상 우선순위가 달라질 것으로 추정됩니다."
                    ),
                )
            )
    return candidates


def _fallback_inference_candidates(digest: FactDigest) -> tuple[str, ...]:
    card = digest.cards[0]
    entity = card.entity or "조회 대상"
    metric = card.metric or "지표"
    source = _source_display(card.source)
    return (
        f"{source}의 {entity} {metric} 근거를 바탕으로 MI팀은 후속 시장 전략의 우선순위를 바꿀 가능성이 있습니다.",
        f"{entity}의 {metric} 구조가 이어지면 MI팀의 경쟁 대응 전략도 달라질 것으로 추정됩니다.",
        f"{source} 근거와 질문 축을 종합하면 MI팀의 {entity} 분석 대상 우선순위가 구체화될 구도로 해석될 수 있습니다.",
    )


def _source_display(source: str) -> str:
    return {
        "hira": "건강보험심사평가원",
        "mart": "내부 데이터마트",
        "patent": "식약처 의약품 특허목록",
        "clinicaltrials": "ClinicalTrials.gov",
        "nedrug": "의약품안전나라",
        "document": "업로드 문서",
        "document_sql": "업로드 파일 표 집계",
        "document_vdb": "업로드 문서 검색",
    }.get(source, source)


def _insight_paragraphs(answer: str) -> tuple[str, ...]:
    match = _INSIGHT_SECTION_RE.search(_normalize_section_headings(answer))
    if match is None:
        return ()
    return tuple(
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n", match.group("body"))
        if paragraph.strip()
    )


def _insight_sentences(answer: str) -> tuple[str, ...]:
    return tuple(
        sentence.strip()
        for paragraph in _insight_paragraphs(answer)
        for sentence in _split_sentences(paragraph)
        if sentence.strip()
    )


def _split_sentences(value: str) -> tuple[str, ...]:
    """Split prose while preserving common English abbreviations."""

    raw = tuple(part.strip() for part in _SENTENCE_RE.split(value) if part.strip())
    merged: list[str] = []
    for part in raw:
        if merged and merged[-1].casefold().endswith((" u.s.", " u.k.", " e.u.")):
            merged[-1] = f"{merged[-1]} {part}"
            continue
        if (
            merged
            and merged[-1].casefold().endswith(" co.")
            and _COMPANY_SUFFIX_RE.match(part)
        ):
            merged[-1] = f"{merged[-1]} {part}"
            continue
        merged.append(part)
    return tuple(merged)


def _replace_insight_body(answer: str, paragraphs: Sequence[str]) -> str:
    normalized = _normalize_section_headings(answer)
    match = _INSIGHT_SECTION_RE.search(normalized)
    if match is None:
        return normalized
    section = "## 종합 인사이트\n" + "\n\n".join(paragraphs)
    prefix = normalized[: match.start()].rstrip()
    suffix = normalized[match.end() :].lstrip()
    return "\n\n".join(part for part in (prefix, section, suffix) if part).strip()


def _digest_expansion_candidates(digest: FactDigest) -> tuple[str, ...]:
    candidates: list[str] = []
    for card in digest.cards:
        candidates.extend(_card_expansion_candidates(card))
    candidates.extend(_cross_source_expansion_candidates(digest.cards))
    return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))


def _card_expansion_candidates(card: DerivedCoreCard) -> list[str]:
    if card.card_type == "disease":
        return _disease_expansion_candidates(card)
    if card.card_type == "clinical":
        return _clinical_expansion_candidates(card)
    if card.card_type == "patent":
        return _patent_expansion_candidates(card)
    if card.card_type == "file_aggregate":
        return _file_expansion_candidates(card)
    if card.card_type == "market":
        return _market_expansion_candidates(card)
    if card.source == "nedrug":
        return _nedrug_expansion_candidates(card)
    return []


def _market_expansion_candidates(card: DerivedCoreCard) -> list[str]:
    periods = card.full_stats.get("periods")
    values = card.full_stats.get("values")
    if not isinstance(periods, Sequence) or isinstance(periods, (str, bytes)):
        return []
    normalized_periods = tuple(str(period) for period in periods if str(period).strip())
    entity = card.entity or "시장"
    metric = card.metric or "지표"
    period_scope_candidates = (
        [
            (
                f"{entity} {metric}은 {normalized_periods[0]}와 "
                f"{normalized_periods[-1]} 두 기간을 포함해 시계열 비교 범위를 보여줍니다."
            )
        ]
        if len(normalized_periods) >= 2
        else []
    )
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return period_scope_candidates
    observations = [
        (str(period), value)
        for period, value in zip(periods, values, strict=False)
        if _is_number(value)
    ]
    unit = card.unit or ""
    candidates = list(period_scope_candidates)
    first_value = card.full_stats.get("first_value")
    latest_value = card.full_stats.get("latest_value")
    change_value = card.full_stats.get("change_value")
    if all(_is_number(value) for value in (first_value, latest_value, change_value)):
        candidates.append(
            f"{entity} {metric}의 시작값 {_display_number(first_value)}{unit}, 최신값 "
            f"{_display_number(latest_value)}{unit}, 변화값 {_display_number(change_value)}{unit}의 "
            "시계열 비교는 같은 범위 안의 추세를 보여줍니다."
        )
    for first, second in pairwise(observations):
        candidates.append(
            f"{entity} {metric}은 {first[0]} {_display_number(first[1])}{unit} → "
            f"{second[0]} {_display_number(second[1])}{unit}까지 이어져 기간별 변화를 "
            "보여줍니다."
        )
    return candidates


def _disease_expansion_candidates(card: DerivedCoreCard) -> list[str]:
    if card.unit not in {None, "명"}:
        return []
    observations: list[tuple[str, int | float]] = []
    flat_labels = {
        "female_outpatient": "외래 여성",
        "male_outpatient": "외래 남성",
        "female_inpatient": "입원 여성",
        "male_inpatient": "입원 남성",
    }
    for key, label in flat_labels.items():
        value = card.full_stats.get(key)
        if _is_number(value):
            observations.append((label, value))
    for field in ("care_representatives", "sex_representatives"):
        values = card.full_stats.get(field)
        if not isinstance(values, Mapping):
            continue
        for label, payload in values.items():
            if not isinstance(payload, Mapping):
                continue
            value = payload.get("value")
            if _is_number(value):
                observations.append((str(label), value))
    observations = list(dict.fromkeys(observations))
    entity = card.entity or "질환"
    unit = card.unit or "명"
    candidates: list[str] = []
    code_representatives = card.full_stats.get("code_representatives")
    if isinstance(code_representatives, Mapping):
        code_values = [
            (str(code), payload.get("value"), payload.get("disease_name"))
            for code, payload in sorted(code_representatives.items())
            if isinstance(payload, Mapping) and _is_number(payload.get("value"))
        ]
        for first, second in pairwise(code_values):
            candidates.append(
                f"{first[0]} {first[2] or ''} {_display_number(first[1])}{unit}, "
                f"{second[0]} {second[2] or ''} {_display_number(second[1])}{unit} 사이에는 "
                "확장 상병코드별 확인값의 차이를 보여줍니다."
            )
    if len(observations) >= 2:
        for first, second in pairwise(observations):
            candidates.append(
                f"{entity}의 {first[0]} {_display_number(first[1])}{unit}, "
                f"{second[0]} {_display_number(second[1])}{unit} 사이 격차는 "
                "환자 구성의 차이를 보여줍니다."
            )
    return candidates


def _clinical_expansion_candidates(card: DerivedCoreCard) -> list[str]:
    candidates: list[str] = []
    split = card.full_stats.get("query_breakdown")
    if isinstance(split, Mapping):
        by_query = split.get("by_query")
        global_stats = split.get("global")
        if isinstance(by_query, Sequence) and len(by_query) >= 2:
            query_counts = [
                (
                    str(item.get("query") or "").strip(),
                    item.get("records_received"),
                    item.get("records_direct_related"),
                )
                for item in by_query
                if isinstance(item, Mapping) and item.get("query")
            ]
            if len(query_counts) >= 2:
                details = ", ".join(
                    f"{query} 수신 {_display_number(received)}건·직접 관련 "
                    f"{_display_number(direct)}건"
                    for query, received, direct in query_counts
                )
                duplicates = (
                    int(global_stats.get("cross_query_duplicates_removed") or 0)
                    if isinstance(global_stats, Mapping)
                    else 0
                )
                candidates.append(
                    f"질의별로 {details}이며 질의 간 중복 {duplicates}건을 제거한 "
                    "차이는 검색 표현별 임상 포착 범위를 보여줍니다."
                )
    sponsors = card.full_stats.get("top_sponsors")
    if isinstance(sponsors, Sequence) and not isinstance(sponsors, (str, bytes)):
        pairs = [
            (str(item[0]), item[1])
            for item in sponsors
            if isinstance(item, Sequence)
            and not isinstance(item, (str, bytes))
            and len(item) >= 2
            and _is_number(item[1])
        ]
        if len(pairs) >= 2:
            first, second = pairs[:2]
            candidates.append(
                f"{first[0]} {_display_number(first[1])}건과 "
                f"{second[0]} {_display_number(second[1])}건의 임상시험 분포는 "
                "상위 스폰서 활동이 집중된 구조로 해석됩니다."
            )
    completed = card.full_stats.get("completed_count")
    active = card.full_stats.get("active_count")
    if _is_number(completed) and _is_number(active):
        candidates.append(
            f"완료 {_display_number(completed)}건과 활성 {_display_number(active)}건의 "
            "구성은 개발 단계가 병행되는 흐름을 보여줍니다."
        )
    late_count = card.full_stats.get("late_phase_count")
    late_ratio = card.full_stats.get("late_phase_ratio_pct")
    if _is_number(late_count) and _is_number(late_ratio):
        candidates.append(
            f"후기 임상 {_display_number(late_count)}건과 비율 "
            f"{_display_number(late_ratio)}%는 상용화에 가까운 파이프라인의 "
            "구성을 보여줍니다."
        )
    time_axis = card.temporal_stats
    reference_date = time_axis.get("reference_date")
    completed_total = time_axis.get("completed_total")
    recent_count = time_axis.get("recent_completed_count")
    recent_ratio = time_axis.get("recent_completed_ratio_pct")
    if (
        reference_date
        and _is_number(completed_total)
        and _is_number(recent_count)
        and _is_number(recent_ratio)
    ):
        candidates.append(
            f"{reference_date} 기준 완료 {completed_total}건 중 최근 3년 내 "
            f"{recent_count}건({recent_ratio}%)이 완료돼 최근 결과 축적 속도를 보여줍니다."
        )
    active_progress = time_axis.get("active_progress")
    if isinstance(active_progress, Sequence) and active_progress:
        progress = active_progress[0]
        if isinstance(progress, Mapping):
            candidates.append(
                f"{progress.get('title')}은 {str(progress.get('start_date', ''))[:7]} 시작 후 "
                f"{str(progress.get('primary_completion_date', ''))[:7]} 1차 완료 예정이며 "
                f"현재 경과율은 {progress.get('progress_pct')}%입니다."
            )
    milestones = time_axis.get("future_milestones")
    if isinstance(milestones, Sequence) and milestones:
        first = milestones[0]
        if isinstance(first, Mapping):
            candidates.append(
                f"가장 가까운 향후 이정표는 {str(first.get('primary_completion_date', ''))[:7]} "
                f"{first.get('title')}의 1차 완료 예정입니다."
            )
        years = Counter(
            str(item.get("primary_completion_date", ""))[:4]
            for item in milestones
            if isinstance(item, Mapping) and item.get("primary_completion_date")
        )
        if years:
            start_year = min(years)
            end_year = max(years)
            candidates.append(
                f"향후 1차 완료 이정표 {sum(years.values())}건이 "
                f"{start_year}~{end_year}년에 분포해 해당 기간에 경쟁 데이터 발표가 "
                "이어질 가능성이 있습니다."
            )
    return candidates


def _patent_expansion_candidates(card: DerivedCoreCard) -> list[str]:
    statuses = card.distributions.get("status", {})
    pairs = [
        (str(status), count)
        for status, count in statuses.items()
        if _is_number(count)
    ]
    entity = card.entity or "해당 제품"
    candidates = []
    if len(pairs) >= 2:
        first, second = pairs[:2]
        candidates.append(
            f"{entity}의 {first[0]} {_display_number(first[1])}건과 "
            f"{second[0]} {_display_number(second[1])}건의 구성은 "
            "권리 상태가 갈리는 구조로 해석됩니다."
        )
    material = card.temporal_stats.get("material_expiration")
    if isinstance(material, Mapping) and material:
        material_remaining = material.get("remaining_months")
        material_remaining_text = (
            f"만료 후 {material.get('elapsed_months')}개월 경과"
            if material.get("is_expired")
            else f"잔여 {material_remaining}개월"
            if isinstance(material_remaining, int)
            else "잔여기간 판독 불가"
        )
        candidates.append(
            f"{entity}의 물질특허 만료일은 {material.get('expiration_date')}이며 "
            f"기준일 대비 {material_remaining_text}입니다."
        )
    longest = card.temporal_stats.get("longest_expiration")
    if isinstance(longest, Mapping) and longest:
        remaining = longest.get("remaining_months")
        remaining_text = (
            f"만료 후 {longest.get('elapsed_months')}개월 경과"
            if longest.get("is_expired")
            else f"잔여 {remaining}개월"
            if isinstance(remaining, int)
            else "잔여기간 판독 불가"
        )
        candidates.append(
            f"{card.temporal_stats.get('reference_date')} 기준 {entity}의 최장 등재 만료일은 "
            f"{longest.get('expiration_date')}이고 현재 유효 "
            f"{card.temporal_stats.get('active_count', 0)}건이며 {remaining_text}입니다."
        )
        candidates.append(
            f"{longest.get('patent_type')}특허의 {remaining_text}은 후속 경쟁 진입 시점을 "
            "가르는 시간축으로 작용할 가능성이 있습니다."
        )
    return candidates


def _nedrug_expansion_candidates(card: DerivedCoreCard) -> list[str]:
    candidates: list[str] = []
    approvals = card.temporal_stats.get("approvals")
    if isinstance(approvals, Sequence) and approvals:
        item = approvals[0]
        if isinstance(item, Mapping):
            candidates.append(
                f"{item.get('item_name')}은 {item.get('approval_date')} 허가 후 "
                f"{item.get('elapsed_years')}년이 경과했습니다."
            )
    reexaminations = card.temporal_stats.get("reexaminations")
    if isinstance(reexaminations, Sequence) and reexaminations:
        item = reexaminations[0]
        if isinstance(item, Mapping):
            remaining_text = (
                f"만료 후 {item.get('elapsed_months')}개월 경과"
                if item.get("is_expired")
                else f"잔여 {item.get('remaining_months')}개월"
            )
            candidates.append(
                f"재심사 만료일 {item.get('reexam_end_date')}의 {remaining_text}은 "
                "허가 후 보호 구간의 시간축입니다."
            )
    return candidates


def _file_expansion_candidates(card: DerivedCoreCard) -> list[str]:
    value = card.representative.get("value")
    rows = card.representative.get("applied_rows")
    if not _is_number(value) or not _is_number(rows):
        return []
    entity = card.entity or "업로드 파일"
    return [
        (
            f"{entity}의 집계값 {_display_number(value)}{card.unit or ''}과 "
            f"적용 {_display_number(rows)}행은 동일 집계 계산에 사용된 값과 행 수입니다."
        )
    ]


def _cross_source_expansion_candidates(
    cards: Sequence[DerivedCoreCard],
) -> list[str]:
    anchors = [
        (card.card_type, anchor)
        for card in cards
        if (anchor := _card_anchor(card))
    ]
    candidates: list[str] = []
    relation_text = {
        frozenset(("disease", "market")): "환자 수요와 시장 실적",
        frozenset(("clinical", "market")): "임상 개발과 시장 실적",
        frozenset(("patent", "market")): "권리 상태와 시장 실적",
        frozenset(("file_aggregate", "market")): "파일 집계와 시장 실적",
    }
    for index, (first_type, first) in enumerate(anchors):
        for second_type, second in anchors[index + 1 :]:
            if first[0] == second[0]:
                continue
            relation = relation_text.get(frozenset((first_type, second_type)))
            if relation is None:
                continue
            candidates.append(
                f"{first[1]}와 {second[1]}는 서로 다른 원천에서 {relation}이 "
                "연결되는 구조를 보여줍니다."
            )
    return candidates


def _card_anchor(card: DerivedCoreCard) -> tuple[str, str] | None:
    if card.card_type == "disease":
        value = card.representative.get("value")
        code = str(card.representative.get("code") or "").strip()
        if _is_number(value):
            return (
                card.source,
                f"{card.entity or '질환'} {code} {_display_number(value)}{card.unit or '명'}".strip(),
            )
    if card.card_type == "market":
        value = card.representative.get("value")
        if value is None:
            value = card.representative.get("sales")
        unit = card.unit or str(card.representative.get("unit") or "")
        if _is_number(value):
            return (
                card.source,
                f"{card.period or ''} {card.entity or '시장'} {_display_number(value)}{unit}".strip(),
            )
    if card.card_type == "clinical" and card.received_count > 0:
        return (
            card.source,
            f"{card.entity or '임상시험'} {_display_number(card.received_count)}건",
        )
    if card.card_type == "patent" and card.matched_count > 0:
        return (
            card.source,
            f"{card.entity or '특허'} {_display_number(card.matched_count)}건",
        )
    return None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _display_number(value: Any) -> str:
    decimal_value = Decimal(str(value))
    if decimal_value == decimal_value.to_integral_value():
        return f"{int(decimal_value):,}"
    return format(decimal_value.normalize(), "f")


def _normalize_section_headings(answer: str) -> str:
    normalized = _INLINE_SECTION_HEADING_RE.sub(
        lambda match: f"\n\n## {match.group('name')}\n",
        answer,
    )
    return _REPEATED_SECTION_HEADING_RE.sub(
        lambda match: f"## {match.group('name')}",
        normalized,
    )


def _trace(
    paragraphs: Sequence[str],
    counts: Sequence[int],
    *,
    omitted: bool,
    section_found: bool = True,
    candidate_sentence_count: int | None = None,
    reject_reason_counts: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    sentence_count = len(counts)
    return {
        "contract_met": (
            not omitted
            and 1 <= sentence_count <= _EXPANSION_SENTENCE_MAXIMUM
        ),
        "omitted": omitted,
        "fallback_applied": False,
        "paragraph_count": len(paragraphs),
        "sentence_count": sentence_count,
        "concrete_values_per_sentence": list(counts),
        "concrete_value_total": sum(counts),
        "minimum_concrete_values": min(counts, default=0),
        "reason_code": None if not omitted else "MISSING_EVIDENCE",
        "section_found": section_found,
        "candidate_sentence_count": (
            sentence_count
            if candidate_sentence_count is None
            else candidate_sentence_count
        ),
        "retained_sentence_count": sentence_count,
        "truncated_sentence_count": 0,
        "duplicate_sections_collapsed": 0,
        "reject_reason_counts": dict(reject_reason_counts or {}),
    }


def _source_data_count(digest: FactDigest) -> int:
    received = sum(
        max(0, int(count)) for count in digest.source_received_counts.values()
    )
    if digest.source_received_counts:
        return received
    card_data = sum(
        max(
            card.received_count,
            card.matched_count,
            card.visible_count,
            len(card.evidence_ids),
            int(bool(card.representative)),
            int(bool(card.file_facts)),
        )
        for card in digest.cards
    )
    return max(received, card_data)


def _expansion_trace_fields(metrics: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "available_material_count": metrics["available_material_count"],
        "eligible_material_count": metrics["eligible_material_count"],
        "unused_material_count": metrics["unused_material_count"],
        "data_rich": metrics["data_rich"],
        "rich_material": metrics["rich_material"],
        "required_sentence_count": metrics["required_sentence_count"],
        "maximum_sentence_count": metrics["maximum_sentence_count"],
        "expansion_axes": metrics["expansion_axes"],
        "expansion_axis_count": metrics["expansion_axis_count"],
        "expansion_target_met": metrics["expansion_target_met"],
        "expansion_retry_reason": metrics["retry_reason"],
    }


def _digest_value_variants(digest: FactDigest) -> frozenset[str]:
    values: set[str] = set()
    payload = digest.prompt_payload()
    derived_metrics = payload.pop("derived_metrics", ())
    payload.pop("derived_metrics_manifest", None)
    _collect_values(payload, values)
    for metric in derived_metrics:
        if not isinstance(metric, Mapping):
            continue
        value = metric.get("value")
        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            _collect_values(value, values)
    return frozenset(value.casefold() for value in values if len(value.strip()) >= 1)


def _has_unbound_clinical_field(sentence: str, digest: FactDigest) -> bool:
    normalized = sentence.casefold()
    if not any(term.casefold() in normalized for term in _CLINICAL_ONLY_TERMS):
        return False
    clinical_values: set[str] = set()
    for card in digest.cards:
        if card.source == "clinicaltrials" or card.card_type == "clinical":
            _collect_values(card.model_dump(mode="json"), clinical_values)
    allowed = frozenset(
        value.casefold() for value in clinical_values if len(value.strip()) >= 1
    )
    return _concrete_value_count(sentence, allowed) < 2


def _collect_values(value: Any, output: set[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"status", "phase"} and isinstance(item, Mapping):
                output.update(str(entry) for entry in item)
            _collect_values(item, output)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _collect_values(item, output)
        return
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float, Decimal)):
        output.update(_number_variants(value))
        return
    text = str(value).strip()
    if len(text) >= 2 and text not in {"patent", "clinical", "disease", "market"}:
        output.add(text)


def _digest_material_entries(digest: FactDigest) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    material_fields = (
        "entity",
        "metric",
        "period",
        "unit",
        "representative",
        "distributions",
        "full_stats",
        "file_facts",
    )
    for card in digest.cards:
        payload = card.model_dump(mode="json")
        for field in material_fields:
            for path, value in _flatten_material(payload.get(field), field):
                if not _is_public_material_path(path) or (
                    path.rsplit(".", 1)[-1] == "dimension"
                ) or (
                    card.card_type == "disease"
                    and path.startswith("distributions.")
                ):
                    continue
                key = (card.source, path, str(value).casefold())
                if key in seen:
                    continue
                seen.add(key)
                entries.append(
                    {
                        "source": card.source,
                        "path": path,
                        "value": value,
                    }
                )
    return entries


def _expansion_material_entries(digest: FactDigest) -> list[dict[str, Any]]:
    """Return material from every lane that actually received records."""
    return _digest_material_entries(digest)


def _flatten_material(value: Any, path: str) -> list[tuple[str, Any]]:
    if isinstance(value, Mapping):
        flattened: list[tuple[str, Any]] = []
        for key, item in value.items():
            flattened.extend(_flatten_material(item, f"{path}.{key}"))
        return flattened
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        flattened = []
        for item in value:
            flattened.extend(_flatten_material(item, path))
        return flattened
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, (int, float, Decimal)):
        return [(path, value)]
    text = str(value).strip()
    if len(text) < 2 or text in {"patent", "clinical", "disease", "market"}:
        return []
    return [(path, text)]


def _material_is_used(normalized_answer: str, value: Any) -> bool:
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return any(
            variant.casefold() in normalized_answer
            for variant in _number_variants(value)
        )
    return str(value).strip().casefold() in normalized_answer


def _has_cross_source_binding(
    sentences: Sequence[str],
    digest: FactDigest,
) -> bool:
    by_source: dict[str, set[str]] = {}
    for card in digest.cards:
        values: set[str] = set()
        for field in (
            card.representative,
            card.distributions,
            card.full_stats,
            card.file_facts,
        ):
            _collect_values(field, values)
        by_source.setdefault(card.source, set()).update(
            value.casefold() for value in values if value
        )
    if len(by_source) < 2:
        return False

    value_sources: dict[str, set[str]] = {}
    for source, values in by_source.items():
        for value in values:
            value_sources.setdefault(value, set()).add(source)
    unique_values = {
        source: {
            value
            for value in values
            if len(value_sources.get(value, ())) == 1
        }
        for source, values in by_source.items()
    }
    for sentence in sentences:
        normalized = sentence.casefold()
        matched_sources = {
            source
            for source, values in unique_values.items()
            if any(value in normalized for value in values)
            or any(
                alias in normalized
                for alias in _SOURCE_ALIASES.get(source, (source.casefold(),))
            )
        }
        if len(matched_sources) >= 2:
            return True
    return False


def _number_variants(value: float | Decimal) -> set[str]:
    text = str(value)
    variants = {text}
    try:
        decimal_value = Decimal(text)
    except ArithmeticError:
        return variants
    if decimal_value == decimal_value.to_integral_value():
        integer = int(decimal_value)
        variants.add(f"{integer:,}")
    return variants


def _concrete_value_count(sentence: str, allowed_values: frozenset[str]) -> int:
    normalized = sentence.casefold()
    matched = {
        value
        for value in allowed_values
        if value and value in normalized
    }
    return len(matched)


def _has_digest_numeric_anchor(
    insight: str,
    allowed_values: frozenset[str],
) -> bool:
    return any(
        any(token.casefold() in value for value in allowed_values)
        for token in _NUMERIC_TOKEN_RE.findall(insight)
    )


def _has_unbound_numeric_token(
    sentence: str,
    allowed_values: frozenset[str],
) -> bool:
    return any(
        not any(token.casefold() in value for value in allowed_values)
        for token in _NUMERIC_TOKEN_RE.findall(sentence)
    )


def _numeric_recitation_keys(sentence: str) -> Counter[str]:
    keys: Counter[str] = Counter()
    for token in _NUMERIC_TOKEN_RE.findall(sentence):
        if re.fullmatch(
            r"(?:19|20)\d{2}(?:-Q[1-4]|-\d{2}-\d{2})?|\d{1,2}-\d{5,}",
            token,
        ):
            continue
        normalized = token.replace(",", "")
        try:
            decimal_value = Decimal(normalized)
        except ArithmeticError:
            continue
        keys[format(decimal_value.normalize(), "f")] += 1
    return keys


def _has_unbound_status(
    sentence: str,
    allowed_values: frozenset[str],
) -> bool:
    normalized = sentence.casefold()
    return any(
        status.casefold() in normalized
        and not any(status.casefold() in value for value in allowed_values)
        for status in _STATUS_TERMS
    )
