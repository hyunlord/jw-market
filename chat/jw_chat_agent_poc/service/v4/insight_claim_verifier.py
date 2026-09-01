from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from jw_chat_agent_poc.service.v4.fact_digest import FactDigest
from jw_chat_agent_poc.service.v4.insight_contract import (
    _normalize_internal_field_labels,
)
from jw_chat_agent_poc.service.v4.narrative_values import public_enum_value

HARD_FABRICATED_VALUE = "hard_fabricated_value"
HARD_FABRICATED_OBSERVATION = "hard_fabricated_observation"
HARD_INTERNAL_FIELD = "hard_internal_field"
_HARD_BLOCK_REASONS = frozenset(
    {
        HARD_FABRICATED_VALUE,
        HARD_FABRICATED_OBSERVATION,
        HARD_INTERNAL_FIELD,
    }
)

_DATE_RE = re.compile(
    r"\b\d{4}(?:[-/.]\d{1,2}(?:[-/.]\d{1,2})?|년\s*\d{1,2}월(?:\s*\d{1,2}일)?)"
)
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_])[-+]?\d[\d,]*(?:\.\d+)?")
_INTERNAL_FIELD_RE = re.compile(r"(?<![A-Za-z0-9_])[a-z]+_[a-z0-9_]+(?![A-Za-z0-9_])")
_POSITIVE_DIRECTION = {
    "하락": "상승",
    "감소": "증가",
    "축소": "확대",
    "낮아": "높아",
}
_NEGATIVE_DIRECTION = {value: key for key, value in _POSITIVE_DIRECTION.items()}
_RANK_IMPROVEMENT_REPLACEMENTS = {
    "하락": "상승",
    "밀려남": "상승",
    "밀려났": "상승했",
    "후퇴": "상승",
}
_RANK_DECLINE_REPLACEMENTS = {
    "상승": "하락",
    "도약": "하락",
    "올라섰": "하락했",
}
_RANK_TRANSITION_RE = re.compile(
    r"(?P<start>\d[\d,]*)\s*위에서\s*(?P<end>\d[\d,]*)\s*위로"
    r"(?P<middle>[^.!?\n]{0,32}?)(?P<verb>상승|하락|도약|밀려남|밀려났|후퇴|올라섰)"
)
_RANK_CONTEXT_RE = re.compile(
    r"(?P<prefix>(?:\d[\d,]*\s*계단\s*|\d[\d,]*\s*위로\s*|순위(?:가|는)?\s*))"
    r"(?P<verb>상승|하락|도약|밀려남|밀려났|후퇴|올라섰|개선|악화)"
)
_RANK_IMPROVEMENT_WORDS = frozenset({"상승", "도약", "올라섰", "개선"})
_RANK_DECLINE_WORDS = frozenset({"하락", "밀려남", "밀려났", "후퇴", "악화"})
_RANK_IMPROVEMENT_STYLE = {
    "하락": "상승",
    "밀려남": "도약",
    "밀려났": "올라섰",
    "후퇴": "도약",
    "악화": "개선",
}
_RANK_DECLINE_STYLE = {
    "상승": "하락",
    "도약": "후퇴",
    "올라섰": "밀려났",
    "개선": "악화",
}
_DIRECTION_METRIC_PRIORITY = {
    "gap_change": 0,
    "share_delta": 1,
    "rank_delta": 2,
    "growth_spread_vs_market": 3,
    "brand_growth_rate": 4,
    "market_growth_rate": 5,
    "patient_yoy_growth": 6,
    "yearly_growth": 7,
    "cagr": 8,
}
_SIGNED_DIRECTION_VALUE_RE = re.compile(
    r"(?<![\d.])-(?P<value>\d[\d,]*(?:\.\d+)?)"
    r"(?P<suffix>\s*(?:억원|원|%p|%|명|건|개월|년|계단)?\s*)"
    r"(?P<middle>(?:(?:로|으로|의\s*격차|이나|만큼|가량|정도)\s*)?)"
    r"(?P<verb>감소|증가|축소|확대|단축|줄어|줄이|늘어|늘리|낮아|높아|벌어|좁혀)"
)
_FABRICATED_BEHAVIOR_RE = re.compile(
    r"(?:"
    r"(?:임상적|의료진(?:의)?|환자(?:의)?|소비자(?:의)?|처방\s*현장(?:에서의)?|시장(?:의)?)?"
    r"\s*(?:처방\s*)?선호(?:도)?"
    r"|(?:소비자|환자|수요|처방|타\s*약제)[^.!?\n]{0,24}(?:전환|이탈|이동)"
    r"|(?:효능|유효성)[^.!?\n]{0,24}(?:입증|인정|확인|선호|제공)"
    r"|(?:의료진|환자|소비자|시장)[^.!?\n]{0,16}(?:만족(?:도)?|신뢰)"
    r"|복약\s*순응(?:도)?|처방\s*행태|임상\s*인과|인과\s*관계"
    r")"
)
_BEHAVIOR_ACTORS = (
    "소비자",
    "의료진",
    "환자",
    "약사",
    "처방의",
    "시장 참여자",
    "수요층",
)
_BEHAVIOR_PREDICATES = (
    "선호",
    "수용",
    "충성",
    "만족",
    "전환",
    "신뢰",
    "인식",
    "기대",
    "호응",
)
_ACTOR_BEHAVIOR_RE = re.compile(
    rf"(?:{'|'.join(map(re.escape, _BEHAVIOR_ACTORS))})"
    rf"[^.!?\n]{{0,40}}(?:{'|'.join(map(re.escape, _BEHAVIOR_PREDICATES))})"
    rf"|(?:{'|'.join(map(re.escape, _BEHAVIOR_PREDICATES))})"
    rf"[^.!?\n]{{0,40}}(?:{'|'.join(map(re.escape, _BEHAVIOR_ACTORS))})"
)
_METRIC_SURFACE_LABELS = {
    "brand_growth_rate": "매출 성장률",
    "market_growth_rate": "시장 성장률",
    "growth_spread_vs_market": "시장 대비 성장률 격차",
    "absolute_gap": "경쟁 제품과의 격차",
    "gap_change": "경쟁 제품과의 격차 변화",
    "share_delta": "점유율 변화",
    "rank_delta": "순위 변화",
    "ms_share": "시장점유율",
    "yearly_growth": "전년 대비 성장률",
    "cagr": "연평균 성장률",
}
_UNQUALIFIED_PATENT_TITLE_RE = re.compile(r"(?:대표|주요|핵심|최신)\s*특허")
_VAGUE_ENTITY_RE = re.compile(
    r"(?P<label>가장\s*최근|대표|주요|핵심|최신|해당|일부)\s*"
    r"(?P<kind>임상(?:시험)?|특허|브랜드|상병|질환|기간|파일|시트)"
)
_NCT_RE = re.compile(r"(?<![A-Za-z0-9])NCT\d{8}(?![A-Za-z0-9])", re.IGNORECASE)
_PATENT_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:10-\d{6,}|KR\d{6,}[A-Z]?|[A-Z]{2}\d{6,}[A-Z]?\d*)"
    r"(?![A-Za-z0-9])"
)
_KCD_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z]\d{2}(?:\.\d+)?(?![A-Za-z0-9])")
_IDENTIFIER_PATTERNS = (_NCT_RE, _PATENT_RE, _KCD_RE)
_PERIOD_RE = re.compile(r"\b\d{4}(?:-\d{2})?\b")
_CLAIM_VALUE_RE = re.compile(
    r"(?P<value>[-+]?\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit>억원|백만원|만원|원|%p|%|명|건|개|회|개월|분기)"
)
_KOREAN_NUMBER_WITH_UNIT_RE = re.compile(
    r"(?<![가-힣])"
    r"(?P<number>(?=[영공일이삼사오육칠팔구십백천만억조]*[영공일이삼사오육칠팔구])"
    r"[영공일이삼사오육칠팔구십백천만억조]+(?:점[영공일이삼사오육칠팔구]+)?)"
    r"(?P<unit>퍼센트포인트|퍼센트|백만원|억원|만원|원|명|건|개|회|배)"
    r"(?=$|[^가-힣]|(?:이고|이며|입니다|였습니다|였다|인|은|는|이|가|을|를|와|과|의|에|에서|으로|로|보다|마다|부터|까지))"
)
_FACTUAL_HYPO_RE = re.compile(r"\d[\d,.]*\s*(?:억원|원|%p|%|명|건|개|회|배|계단)")
_SPECULATION_RE = re.compile(
    r"(?:가능성|전망|예상|추정|가정|시사|일\s*수|할\s*수|것으로\s*보)"
)
_EXPLICIT_IRRELEVANCE_RE = re.compile(
    r"(?:질문|분석).{0,32}(?:무관|직접\s*관련(?:이)?\s*없)|"
    r"무관한.{0,24}(?:산업|정보|결과|내용)|별개(?:의)?\s*산업군|"
    r"직접(?:적(?:으로|인)?)?\s*(?:관련|연결|연관성).{0,32}(?:없|않|확인되지)"
)
_IRRELEVANT_BODY_NOTICE = (
    "본문 수치는 질문과 직접 결속된 원천만을 기준으로 정리했습니다."
)


class ClaimAction(StrEnum):
    PASSED = "passed"
    CORRECTED = "corrected"
    SOFTENED = "softened"
    LABELED = "labeled"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ClaimVerification:
    action: ClaimAction
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if self.action == ClaimAction.BLOCKED and self.reason_code not in _HARD_BLOCK_REASONS:
            raise ValueError("blocked claim requires a hard-block reason code")


@dataclass(frozen=True)
class VerifiedClaims:
    claims: tuple[Any, ...]
    manifest: dict[str, object]


@dataclass(frozen=True)
class _EvidenceValues:
    numbers: tuple[Decimal, ...]
    dates: tuple[str, ...]
    metric_values: tuple[tuple[Decimal, str], ...]


@dataclass(frozen=True)
class _PatentScopeCheck:
    text: str
    checked_number_count: int = 0
    violation_count: int = 0
    replacement_count: int = 0
    rewrite_count: int = 0
    action: str = "none"


@dataclass(frozen=True)
class _BindingCheck:
    evidence_ids: tuple[str, ...]
    violations: tuple[str, ...] = ()
    action: str = "retained"
    text: str | None = None
    diagnostics: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class _ReplacementEvidence:
    evidence_ids: tuple[str, ...] = ()
    partial_text: str | None = None
    diagnostics: tuple[tuple[str, int], ...] = ()


def _is_source_required_factual_text(text: str, digest: FactDigest) -> bool:
    """Return whether an unbound sentence makes a source-requiring assertion."""

    if _CLAIM_VALUE_RE.search(text) or _PERIOD_RE.search(text):
        return True
    if any(pattern.search(text) for pattern in _IDENTIFIER_PATTERNS):
        return True
    if re.search(r"\d", text) or _required_source_domains(text):
        return True
    entities = {
        str(card.entity).strip()
        for card in digest.cards
        if str(card.entity or "").strip()
    }
    return any(entity in text for entity in entities)


def verify_structured_claims(
    claims: Sequence[Any],
    digest: FactDigest,
) -> VerifiedClaims:
    retained: list[Any] = []
    claim_manifest: list[dict[str, object]] = []
    counters: dict[str, Counter[str]] = {
        name: Counter(
            generated=0,
            passed=0,
            corrected=0,
            softened=0,
            labeled=0,
            blocked=0,
        )
        for name in ("CITE", "CALC", "OBS", "INTERP", "HYPO")
    }
    patent_scope_counters: Counter[str] = Counter(
        checked_number_count=0,
        violation_count=0,
        replacement_count=0,
        rewrite_count=0,
    )
    final_binding_counters: Counter[str] = Counter(
        checked_claim_count=0,
        final_recheck_count=0,
        rewrite_unbound=0,
        number_mismatch=0,
        date_mismatch=0,
        identifier_mismatch=0,
        source_domain_mismatch=0,
        extraneous_evidence_removed=0,
        web_source_mismatch=0,
        replaced=0,
        unbound=0,
        retained=0,
        rebind_attempted=0,
        rebind_succeeded=0,
        rebind_failed=0,
        rebind_partial_attempted=0,
        rebind_partial_succeeded=0,
        rebind_candidate_checked=0,
        rebind_candidate_dropped=0,
        degradation_emitted=0,
        degradation_duplicate_suppressed=0,
        degradation_section_limit_exceeded=0,
        degradation_meta_preserved=0,
    )
    surface_counters: Counter[str] = Counter(
        korean_number_converted=0,
        machine_phrase_rewritten=0,
        rank_direction_checked=0,
        rank_direction_corrected=0,
        label_retyped=0,
        hypothesis_label_added=0,
        irrelevant_body_rewritten=0,
    )
    degradation_templates_seen: set[tuple[str, str]] = set()
    degradation_counts: Counter[str] = Counter()

    for index, claim in enumerate(claims, start=1):
        claim_type = _enum_value(claim.claim_type)
        counters[claim_type]["generated"] += 1
        normalized = _normalize_internal_field_labels(claim.text.strip())
        normalized = _qualify_patent_selection_title(normalized)
        patent_scope = _enforce_product_patent_scope(normalized, digest)
        patent_scope_counters.update(
            {
                "checked_number_count": patent_scope.checked_number_count,
                "violation_count": patent_scope.violation_count,
                "replacement_count": patent_scope.replacement_count,
                "rewrite_count": patent_scope.rewrite_count,
            }
        )
        scoped_claim = _updated(claim, patent_scope.text)
        if _is_irrelevant_web_body_claim(scoped_claim, digest):
            scoped_claim = _updated(
                scoped_claim,
                _IRRELEVANT_BODY_NOTICE,
                evidence_ids=(),
            )
            surface_counters["irrelevant_body_rewritten"] += 1
        scoped_claim, retype_action = _retype_surface_claim(scoped_claim)
        if retype_action == "fact_from_hypothesis":
            surface_counters["label_retyped"] += 1
        elif retype_action == "hypothesis_added":
            surface_counters["hypothesis_label_added"] += 1
        rank_before = scoped_claim.text
        rank_checked = _has_rank_metric(scoped_claim.evidence_ids, digest)
        if rank_checked:
            surface_counters["rank_direction_checked"] += 1
        verified_claim, verdict = _verify_claim(scoped_claim, digest)
        if patent_scope.text != normalized and verdict.action == ClaimAction.PASSED:
            verdict = ClaimVerification(ClaimAction.CORRECTED)
        specificity_action = "none"
        final_patent_scope = patent_scope
        if verified_claim is not None:
            specific_text, specificity_action = _qualify_entity_specificity(
                verified_claim.text,
                verified_claim.evidence_ids,
                digest,
            )
            if specific_text != verified_claim.text:
                post_specificity_scope = _enforce_product_patent_scope(
                    specific_text,
                    digest,
                )
                patent_scope_counters.update(
                    {
                        "checked_number_count": post_specificity_scope.checked_number_count,
                        "violation_count": post_specificity_scope.violation_count,
                        "replacement_count": post_specificity_scope.replacement_count,
                        "rewrite_count": post_specificity_scope.rewrite_count,
                    }
                )
                final_patent_scope = post_specificity_scope
                verified_claim = _updated(verified_claim, post_specificity_scope.text)
                if (
                    post_specificity_scope.text != specific_text
                    and verdict.action == ClaimAction.PASSED
                ):
                    verdict = ClaimVerification(ClaimAction.CORRECTED)
            final_text, final_surface_counts = normalize_final_surface_text(
                verified_claim.text
            )
            surface_counters.update(final_surface_counts)
            if final_text != verified_claim.text:
                verified_claim = _updated(verified_claim, final_text)
                if verdict.action == ClaimAction.PASSED:
                    verdict = ClaimVerification(ClaimAction.CORRECTED)
            verified_claim, final_retype_action = _retype_surface_claim(
                verified_claim
            )
            if final_retype_action == "fact_from_hypothesis":
                surface_counters["label_retyped"] += 1
                if verdict.action == ClaimAction.PASSED:
                    verdict = ClaimVerification(ClaimAction.CORRECTED)
            elif final_retype_action == "hypothesis_added":
                surface_counters["hypothesis_label_added"] += 1
            if rank_checked and verified_claim.text != rank_before:
                surface_counters["rank_direction_corrected"] += 1
            binding = _final_binding_check(verified_claim, digest)
            final_binding_counters["checked_claim_count"] += 1
            final_binding_counters["final_recheck_count"] += 1
            final_binding_counters.update(binding.violations)
            final_binding_counters.update(dict(binding.diagnostics))
            final_binding_counters[binding.action] += 1
            if (
                verified_claim.evidence_ids
                and not binding.evidence_ids
                and verified_claim.text != claim.text
            ):
                final_binding_counters["rewrite_unbound"] += 1
            if (
                binding.evidence_ids != tuple(verified_claim.evidence_ids)
                or binding.text is not None
            ):
                verified_claim = _updated(
                    verified_claim,
                    binding.text or verified_claim.text,
                    evidence_ids=binding.evidence_ids,
                )
            if _is_irrelevant_web_body_claim(verified_claim, digest):
                verified_claim = _updated(
                    verified_claim,
                    _IRRELEVANT_BODY_NOTICE,
                    evidence_ids=(),
                )
                surface_counters["irrelevant_body_rewritten"] += 1
            degradation_candidate = (
                not binding.evidence_ids
                and _enum_value(verified_claim.section) in {"answer", "facts"}
                and _enum_value(claim.claim_type) in {"CITE", "CALC", "OBS"}
                and verified_claim.text != _IRRELEVANT_BODY_NOTICE
            )
            if degradation_candidate and not _is_source_required_factual_text(
                verified_claim.text, digest
            ):
                final_binding_counters["degradation_meta_preserved"] += 1
            elif degradation_candidate:
                section = _enum_value(verified_claim.section)
                degraded_text = (
                    "이 문장의 근거를 확인하지 못해 사실로 확정하지 않습니다."
                )
                template_key = (section, degraded_text)
                if template_key in degradation_templates_seen:
                    verified_claim = None
                    final_binding_counters["degradation_duplicate_suppressed"] += 1
                elif degradation_counts[section] >= 2:
                    verified_claim = None
                    final_binding_counters["degradation_section_limit_exceeded"] += 1
                else:
                    degradation_templates_seen.add(template_key)
                    degradation_counts[section] += 1
                    verified_claim = _updated(
                        verified_claim,
                        degraded_text,
                        evidence_ids=(),
                    )
                    final_binding_counters["unbound_disclosed"] += 1
                    final_binding_counters["degradation_emitted"] += 1
                if verdict.action == ClaimAction.PASSED:
                    verdict = ClaimVerification(ClaimAction.CORRECTED)
        else:
            binding = _BindingCheck(())
        final_claim_type = (
            _enum_value(verified_claim.claim_type)
            if verified_claim is not None
            else _enum_value(scoped_claim.claim_type)
        )
        counters[claim_type][verdict.action.value] += 1
        if verified_claim is not None:
            retained.append(verified_claim)
        claim_manifest.append(
            {
                "claim_index": index,
                "claim_type": final_claim_type,
                "action": verdict.action.value,
                "reason_code": verdict.reason_code,
                "original_text": claim.text,
                "final_text": verified_claim.text if verified_claim is not None else None,
                "specificity_action": specificity_action,
                "patent_scope_action": final_patent_scope.action,
                "final_binding_action": binding.action,
                "final_binding_violations": list(binding.violations),
                "final_evidence_ids": (
                    list(verified_claim.evidence_ids)
                    if verified_claim is not None
                    else []
                ),
            }
        )

    return VerifiedClaims(
        claims=tuple(retained),
        manifest={
            "claims": claim_manifest,
            "type_counters": {
                claim_type: dict(counts) for claim_type, counts in counters.items()
            },
            "soft_deleted_count": 0,
            "hard_block_count": sum(
                counts["blocked"] for counts in counters.values()
            ),
            "patent_scope_counters": dict(patent_scope_counters),
            "final_binding_counters": dict(final_binding_counters),
            "surface_counters": dict(surface_counters),
        },
    )


def normalize_final_surface_text(
    text: str,
    *,
    template_seen: set[str] | None = None,
    sentence_seen: set[str] | None = None,
) -> tuple[str, Counter[str]]:
    """Apply deterministic wording and numeric rules at the release boundary."""

    counters: Counter[str] = Counter(
        internal_enum_localized=0,
        korean_number_converted=0,
        machine_phrase_rewritten=0,
        machine_unit_localized=0,
        numeric_identifier_preserved=0,
        hypothesis_numeric_relation=0,
        hypothesis_scope_narrowed=0,
        hypothesis_template_emitted=0,
        hypothesis_template_suppressed=0,
        hypothesis_template_reached=0,
        machine_phrase_candidate_count=0,
        machine_phrase_residual_count=0,
        embedded_digit_detected=0,
        embedded_digit_corrected=0,
        count_modifier_residual_detected=0,
        count_modifier_residual_corrected=0,
        duplicate_sentence_detected=0,
        duplicate_sentence_suppressed=0,
        escaped_bold_detected=0,
        escaped_bold_corrected=0,
        final_form_checked_text_count=1,
        final_form_checked_char_count=len(text),
    )

    text, escaped_bold_count = re.subn(r"\\\*\\\*", "**", text)
    counters["escaped_bold_detected"] = escaped_bold_count
    counters["escaped_bold_corrected"] = escaped_bold_count

    def number_replacement(match: re.Match[str]) -> str:
        if match.group("number") == "조" and match.group("unit") == "회":
            return match.group(0)
        following = match.string[match.end() :]
        if len(match.group("number")) == 1 and re.fullmatch(r"[가-힣]", following[:1]):
            return match.group(0)
        value = _parse_korean_number(match.group("number"))
        if value is None:
            return match.group(0)
        unit = match.group("unit")
        display_unit = {
            "퍼센트": "%",
            "퍼센트포인트": "%p",
        }.get(unit, unit)
        if display_unit in {"%", "%p", "배"}:
            display = format(value.quantize(Decimal("0.01")), ",f")
        else:
            display = format(value.quantize(Decimal(1)), ",f")
        counters["korean_number_converted"] += 1
        return f"{display}{display_unit}"

    updated = _KOREAN_NUMBER_WITH_UNIT_RE.sub(number_replacement, text)

    malformed_zero_count = re.compile(r"0개된")
    counters["embedded_digit_detected"] += len(
        tuple(malformed_zero_count.finditer(updated))
    )
    updated, malformed_zero_count_fixed = malformed_zero_count.subn("확인된", updated)
    counters["embedded_digit_corrected"] += malformed_zero_count_fixed

    digit_syllables = {
        "0": "영",
        "1": "일",
        "2": "이",
        "3": "삼",
        "4": "사",
        "5": "오",
        "6": "육",
        "7": "칠",
        "8": "팔",
        "9": "구",
    }
    embedded_counter = re.compile(
        r"(?P<prefix>[가-힣]?)(?P<digit>[1-9])(?P<unit>개|회)(?P<suffix>[가-힣])"
    )
    def embedded_counter_replacement(match: re.Match[str]) -> str:
        prefix = match.group("prefix")
        suffix = match.group("suffix")
        following = match.string[match.end() :]
        if not prefix and suffix == "적" and following.startswith("으로"):
            return match.group(0)
        if suffix not in {"발", "적"}:
            return match.group(0)
        counters["embedded_digit_detected"] += 1
        counters["embedded_digit_corrected"] += 1
        return (
            f"{prefix}{digit_syllables[match.group('digit')]}"
            f"{match.group('unit')}{suffix}"
        )

    updated = embedded_counter.sub(embedded_counter_replacement, updated)

    def count_modifier_replacement(match: re.Match[str]) -> str:
        kind = match.group("kind")
        particle = match.group("particle")
        if int(match.group("count").replace(",", "")) == 0:
            suffix = (
                "없이도"
                if particle == "에서도"
                else "없이는"
                if particle == "에서는"
                else "없이"
            )
            return f"확인 가능한 {kind} {suffix}"
        particle_text = {
            "를 통해": "을 통해",
            "을 통해": "을 통해",
            "에서": "에서",
            "에서는": "에서는",
            "에서도": "에서도",
            "로": "로",
            "으로": "으로",
        }[particle]
        return f"관련 {kind}{particle_text}"

    count_modifier_pattern = re.compile(
        r"(?P<count>[\d,]+)개\s*"
        r"(?P<kind>(?:[A-Za-z가-힣()·/_-]+\s*){0,6}"
        r"(?:데이터베이스|시스템|출처|원천|청크|레코드|근거|정보|문서|사이트|검색\s*결과))"
        r"(?P<particle>를\s*통해|을\s*통해|에서도|에서는|에서|으로|로)"
    )
    parenthetical_zero_pattern = re.compile(
        r"0개\s*\((?P<source>[A-Za-z0-9._-]+)\)\s*"
        r"(?P<particle>에\s*따르면|에서도|에서는|에서|으로|로)"
    )
    zero_result_pattern = re.compile(
        r"(?P<received>[\d,]+)건에서\s*확인한\s*"
        r"(?P<kind>[^.!?\n]{1,40}?)(?:은|는)\s*"
        r"0개(?:가|이)\s*(?:조회|확인)되어"
    )
    machine_count_patterns = (
        count_modifier_pattern,
        parenthetical_zero_pattern,
        zero_result_pattern,
    )
    counters["machine_phrase_candidate_count"] = sum(
        len(tuple(pattern.finditer(updated))) for pattern in machine_count_patterns
    )
    counters["count_modifier_residual_detected"] = (
        counters["machine_phrase_candidate_count"]
        + len(tuple(re.finditer(r"\d+(?:개|회)(?=(?:발|적))", updated)))
    )

    def parenthetical_zero_replacement(match: re.Match[str]) -> str:
        source = match.group("source")
        display = "OpenFDA" if source.casefold() == "openfda" else source
        particle = " ".join(match.group("particle").split())
        return f"{display}{particle}"

    def zero_result_replacement(match: re.Match[str]) -> str:
        return (
            f"{match.group('received')}건의 {match.group('kind').strip()}를 조회해"
        )

    machine_rules = (
        (parenthetical_zero_pattern, parenthetical_zero_replacement),
        (zero_result_pattern, zero_result_replacement),
        (
            re.compile(
                r"[^.!?\n]*?COMPLETED의\s*\d{4}-\d{2}-\d{2}\s*"
                r"파생 지표은\s*(?P<count>\d[\d,]*)count입니다\."
            ),
            lambda match: f"완료된 임상시험은 {match.group('count')}건입니다.",
        ),
        (
            re.compile(
                r"(?P<identifier>[A-Za-z0-9.-]+)로 식별되는 참조 근거 "
                r"(?P<kind>기간|임상시험|브랜드|상병|질환|파일|시트)"
            ),
            lambda match: (
                f"참조 {match.group('kind')}은 {match.group('identifier')}"
            ),
        ),
        (
            re.compile(
                r"참조 근거 (?P<kind>브랜드|임상시험|상병|질환|파일|시트)\s+"
            ),
            lambda match: f"{match.group('kind')} ",
        ),
        (
            count_modifier_pattern,
            count_modifier_replacement,
        ),
        (
            re.compile(
                r"(?P<count>[\d,]+)개\s+(?P<kind>정보|웹\s*출처|웹\s*원천|"
                r"임상시험|특허|레코드|근거)인\s+(?P<entity>[A-Za-z0-9가-힣._-]+)"
            ),
            lambda match: (
                match.group("entity")
                if int(match.group("count").replace(",", "")) == 0
                else (
                    f"{match.group('entity')}에서는 관련 {match.group('kind')}를 "
                    f"{match.group('count')}개 확인했습니다"
                )
            ),
        ),
        (
            re.compile(
                r"(?P<entity>[A-Za-z0-9가-힣._-]+)의\s+(?P<count>[\d,]+)개\s+"
                r"(?P<kind>정보|웹\s*출처|웹\s*원천|임상시험|특허|레코드|근거)"
            ),
            lambda match: (
                f"{match.group('entity')}에서 확인한 {match.group('kind')}는 "
                f"{match.group('count')}개"
            ),
        ),
        (
            re.compile(
                r"0개\s*(?P<kind>데이터|정보|웹\s*데이터|웹\s*문서|웹\s*출처|"
                r"웹\s*원천|근거)(?:가|이)\s*(?P<verb>수신|확인)"
                r"(?P<ending>되었습니다|됩니다)"
            ),
            lambda match: (
                f"{match.group('kind')}는 {match.group('verb')}되지 "
                f"{'않았습니다' if match.group('ending') == '되었습니다' else '않습니다'}"
            ),
        ),
        (
            re.compile(
                r"0개\s*(?P<kind>"
                r"웹\s*(?:\([^)]*\))?\s*(?:데이터베이스|데이터|문서|사이트|출처|"
                r"원천|검색\s*결과)?|정보(?:\([^)]*\))?(?:\s*(?:시스템|데이터베이스))?|"
                r"데이터베이스|데이터|문서|사이트|출처|원천|근거|시스템|청크)"
                r"(?P<particle>인|를\s*통해|을\s*통해|에서는|에서도|에서|으로|로|"
                r"에\s*따르면|가|이)?"
            ),
            lambda match: f"{match.group('kind')}{match.group('particle') or ''}",
        ),
        (
            re.compile(
                r"0개\s*\((?P<source>[A-Za-z0-9._-]+)\)\s*"
                r"(?P<kind>시스템|데이터베이스|데이터|문서|출처|원천)"
            ),
            lambda match: f"{match.group('source')} {match.group('kind')}",
        ),
    )
    for pattern, replacement in machine_rules:
        updated, count = pattern.subn(replacement, updated)
        counters["machine_phrase_rewritten"] += count

    updated, count = re.subn(
        r"(?P<count>\d[\d,]*)회적으로",
        lambda match: f"{match.group('count')}회",
        updated,
    )
    counters["machine_phrase_rewritten"] += count
    updated, count = re.subn(
        r"(?P<entity>[A-Za-z0-9가-힣 ._-]+)\s*확인 가능한 정보 없이는",
        lambda match: f"{match.group('entity').strip()}에서 확인 가능한 정보가 없어",
        updated,
    )
    counters["machine_phrase_rewritten"] += count

    empty_hypothesis = (
        "관측된 수치 변화는 경쟁 구도 변화 가능성을 보여주지만, "
        "시장 참여자의 행동 변화까지 단정할 수는 없습니다."
    )
    template_pattern = re.compile(
        r"(?:(?:\*\*)?\[가설(?:\s+\d+)?\](?:\*\*)?\s*)?"
        + re.escape(empty_hypothesis)
    )
    seen_templates = template_seen if template_seen is not None else set()

    def template_replacement(_match: re.Match[str]) -> str:
        counters["hypothesis_template_reached"] += 1
        if empty_hypothesis in seen_templates:
            counters["hypothesis_template_suppressed"] += 1
            return ""
        seen_templates.add(empty_hypothesis)
        counters["hypothesis_template_emitted"] += 1
        return empty_hypothesis

    updated = template_pattern.sub(template_replacement, updated)
    updated = re.sub(r"\n{3,}", "\n\n", updated).strip()

    seen_sentences = sentence_seen if sentence_seen is not None else set()
    sentence_parts = re.split(r"(?<=[.!?])(?=\s|$)", updated)
    retained_sentences: list[str] = []
    for part in sentence_parts:
        key = re.sub(r"\s+", " ", part).strip()
        if len(key) >= 6 and key in seen_sentences:
            counters["duplicate_sentence_detected"] += 1
            counters["duplicate_sentence_suppressed"] += 1
            continue
        if len(key) >= 6:
            seen_sentences.add(key)
        retained_sentences.append(part)
    updated = "".join(retained_sentences).strip()

    def patent_type_replacement(match: re.Match[str]) -> str:
        tokens = re.findall(r"물질\(염\)|물질|용도|기타", match.group("types"))
        return f"{' · '.join(dict.fromkeys(tokens))} 특허"

    updated, patent_type_count = re.subn(
        r"(?P<types>(?:(?:물질\(염\)|물질|용도|기타)\s*){2,})특허",
        patent_type_replacement,
        updated,
    )
    counters["patent_type_normalized"] += patent_type_count
    unit_labels = {
        "count": "건",
        "day": "일",
        "days": "일",
        "month": "개월",
        "months": "개월",
        "percent": "%",
        "percentage_point": "%p",
        "percentage_points": "%p",
        "year": "년",
        "years": "년",
    }

    def unit_replacement(match: re.Match[str]) -> str:
        prefix = match.string[: match.start()]
        if re.search(r"(?:^|\s)[A-Z][A-Z0-9_]*(?:\s+[A-Z][A-Z0-9_]*)*\s+$", prefix):
            counters["numeric_identifier_preserved"] += 1
            return match.group(0)
        counters["machine_unit_localized"] += 1
        return f"{match.group('value')}{unit_labels[match.group('unit').casefold()]}"

    updated = re.sub(
        r"(?P<value>[-+]?\d[\d,]*(?:\.\d+)?)\s*"
        r"(?P<unit>percentage_points?|percent|months?|years?|days?|count)"
        r"(?![A-Za-z_])",
        unit_replacement,
        updated,
        flags=re.IGNORECASE,
    )
    enum_localized = public_enum_value(updated)
    counters["internal_enum_localized"] += int(enum_localized != updated)
    updated = enum_localized
    counters["machine_phrase_residual_count"] = sum(
        len(tuple(pattern.finditer(updated))) for pattern in machine_count_patterns
    )
    counters["count_modifier_residual_corrected"] = max(
        0,
        counters["count_modifier_residual_detected"]
        - counters["machine_phrase_residual_count"]
        - len(tuple(re.finditer(r"\d+(?:개|회)(?=(?:발|적))", updated))),
    )
    counters["final_form_checked_char_count"] = len(updated)
    return updated, counters


def _parse_korean_number(value: str) -> Decimal | None:
    integer_text, separator, decimal_text = value.partition("점")
    integer = _parse_korean_integer(integer_text)
    if integer is None:
        return None
    if not separator:
        return Decimal(integer)
    digit_map = {char: str(index) for index, char in enumerate("영일이삼사오육칠팔구")}
    digit_map["공"] = "0"
    try:
        decimal_digits = "".join(digit_map[char] for char in decimal_text)
    except KeyError:
        return None
    return Decimal(f"{integer}.{decimal_digits}")


def _parse_korean_integer(value: str) -> int | None:
    digits = {char: index for index, char in enumerate("영일이삼사오육칠팔구")}
    digits["공"] = 0
    small_units = {"십": 10, "백": 100, "천": 1000}
    large_units = {"만": 10_000, "억": 100_000_000, "조": 1_000_000_000_000}
    total = 0
    section = 0
    number = 0
    try:
        for char in value:
            if char in digits:
                number = digits[char]
            elif char in small_units:
                section += (number or 1) * small_units[char]
                number = 0
            elif char in large_units:
                total += (section + number or 1) * large_units[char]
                section = 0
                number = 0
            else:
                return None
    except (TypeError, ValueError):
        return None
    return total + section + number


def _has_rank_metric(evidence_ids: Sequence[str], digest: FactDigest) -> bool:
    selected = frozenset(evidence_ids)
    return any(
        metric.id in selected and metric.type == "rank_delta"
        for metric in digest.derived_metrics
    )


def _retype_surface_claim(claim: Any) -> tuple[Any, str]:
    claim_type = _enum_value(claim.claim_type)
    text = re.sub(r"^\*{0,2}\[(?:가설|해석)(?:\s+\d+)?\]\*{0,2}\s*", "", claim.text)
    factual_hypothesis = (
        claim_type == "HYPO"
        and bool(claim.evidence_ids)
        and bool(_FACTUAL_HYPO_RE.search(text))
        and not _SPECULATION_RE.search(text)
    )
    if factual_hypothesis:
        return _updated(
            claim,
            text,
            hedge="none",
            claim_type="OBS",
        ), "fact_from_hypothesis"
    if claim_type == "INTERP" and _SPECULATION_RE.search(text):
        return _updated(
            claim,
            text,
            hedge="hypothesis",
            claim_type="HYPO",
        ), "hypothesis_added"
    return claim, "none"


def _is_irrelevant_web_body_claim(claim: Any, digest: FactDigest) -> bool:
    selected = frozenset(claim.evidence_ids)
    web_cards = tuple(
        card
        for card in digest.cards
        if card.source.casefold() in {"web", "web_news", "tavily"}
        and card.received_count > 0
    )
    if _EXPLICIT_IRRELEVANCE_RE.search(claim.text):
        return True
    if _enum_value(claim.section) in {"answer", "facts", "insight"} and selected:
        primary_domains = {
            "market": frozenset({"mart"}),
            "patent": frozenset({"patent", "nedrug", "web"}),
            "clinical": frozenset({"clinical", "openfda", "web"}),
            "disease": frozenset({"hira", "clinical", "openfda", "web"}),
            "file_aggregate": frozenset({"document", "mart"}),
            "document_summary": frozenset({"document", "web"}),
        }.get(digest.answer_type)
        if primary_domains is not None:
            required_domains = {
                _source_domain_name(source)
                for source in (
                    digest.answer_contract.required_sources
                    if digest.answer_contract is not None
                    else ()
                )
            }
            allowed_domains = primary_domains | frozenset(required_domains)
            selected_domains = _evidence_source_domains(tuple(selected), digest)
            mentioned_domains = _required_source_domains(claim.text)
            outside_domains = selected_domains - allowed_domains
            if outside_domains and (
                selected_domains.isdisjoint(allowed_domains)
                or bool(outside_domains & mentioned_domains)
            ):
                return True
    if not selected:
        return False
    referenced = tuple(
        card for card in web_cards if selected.intersection(card.evidence_ids)
    )
    return bool(referenced) and all(card.matched_count == 0 for card in referenced)


def _source_domain_name(source: object) -> str:
    normalized = str(source or "").casefold()
    if "patent" in normalized:
        return "patent"
    if normalized in {"clinicaltrials", "ct"} or "clinical" in normalized:
        return "clinical"
    if normalized in {"openfda", "fda"} or "openfda" in normalized:
        return "openfda"
    if normalized in {"web", "web_search", "tavily"} or "web" in normalized:
        return "web"
    if "hira" in normalized:
        return "hira"
    if normalized.startswith("document"):
        return "document"
    if normalized in {"mart", "market"} or "mart" in normalized:
        return "mart"
    return normalized


def _final_binding_check(claim: Any, digest: FactDigest) -> _BindingCheck:
    original_evidence_ids = tuple(dict.fromkeys(claim.evidence_ids))
    if claim.text == _IRRELEVANT_BODY_NOTICE and not original_evidence_ids:
        return _BindingCheck((), (), "retained")
    evidence_ids = _remove_extraneous_evidence(
        claim.text,
        original_evidence_ids,
        digest,
    )
    violations = _binding_violations(claim.text, evidence_ids, digest)
    removed_extraneous = evidence_ids != original_evidence_ids
    if removed_extraneous:
        violations = tuple(dict.fromkeys(("extraneous_evidence_removed", *violations)))
    substantive_violations = tuple(
        violation
        for violation in violations
        if violation != "extraneous_evidence_removed"
    )
    if not substantive_violations:
        return _BindingCheck(
            evidence_ids,
            violations,
            "replaced" if removed_extraneous else "retained",
        )

    replacement = _replacement_evidence_ids(claim.text, digest)
    if replacement.evidence_ids and (
        replacement.evidence_ids != original_evidence_ids
        or replacement.partial_text is not None
    ):
        return _BindingCheck(
            replacement.evidence_ids,
            violations,
            "replaced",
            replacement.partial_text,
            replacement.diagnostics,
        )
    return _BindingCheck(
        (),
        violations,
        "unbound",
        diagnostics=replacement.diagnostics,
    )


def _remove_extraneous_evidence(
    text: str,
    evidence_ids: Sequence[str],
    digest: FactDigest,
) -> tuple[str, ...]:
    required_domains = _required_source_domains(text)
    if not required_domains:
        return tuple(evidence_ids)
    return tuple(
        evidence_id
        for evidence_id in evidence_ids
        if (domains := _evidence_source_domains((evidence_id,), digest))
        and bool(domains & required_domains)
    )


def _binding_violations(
    text: str,
    evidence_ids: Sequence[str],
    digest: FactDigest,
) -> tuple[str, ...]:
    violations: list[str] = []
    values = _evidence_values(evidence_ids, digest)
    text_numbers = tuple(
        value
        for match in _CLAIM_VALUE_RE.finditer(_DATE_RE.sub("", text))
        if (value := _decimal(match.group("value"))) is not None
    )
    if text_numbers and any(
        not _number_present(value, values.numbers) for value in text_numbers
    ):
        violations.append("number_mismatch")

    text_dates = _date_tokens(text)
    if text_dates and any(value not in values.dates for value in text_dates):
        violations.append("date_mismatch")

    evidence_text = _evidence_text(evidence_ids, digest)
    for pattern in _IDENTIFIER_PATTERNS:
        text_identifiers = _identifier_tokens(pattern, text)
        if not text_identifiers:
            continue
        direct_identifiers = _identifier_tokens(pattern, " ".join(evidence_ids))
        if direct_identifiers:
            identifiers_match = set(text_identifiers).issubset(direct_identifiers)
        else:
            identifiers_match = all(
                identifier in evidence_text.upper() for identifier in text_identifiers
            )
        if not identifiers_match:
            violations.append("identifier_mismatch")

    required_domains = _required_source_domains(text)
    mentioned_entities = tuple(
        entity
        for card in digest.cards
        if (
            not required_domains
            or _evidence_source_domains(card.evidence_ids, digest) & required_domains
        )
        if (entity := str(card.entity or "").strip()) and entity in text
    )
    selected_entities = {
        str(card.entity or "").strip()
        for card in _selected_cards(evidence_ids, digest)
    }
    if mentioned_entities and any(
        entity not in selected_entities for entity in mentioned_entities
    ):
        violations.append("identifier_mismatch")

    selected_domains = _evidence_source_domains(evidence_ids, digest)
    missing_domains = required_domains - selected_domains
    if missing_domains:
        violations.append("source_domain_mismatch")
        if "web" in missing_domains:
            violations.append("web_source_mismatch")
    return tuple(dict.fromkeys(violations))


def _replacement_evidence_ids(text: str, digest: FactDigest) -> _ReplacementEvidence:
    candidates = tuple(
        dict.fromkeys(
            evidence_id
            for card in digest.cards
            for evidence_id in card.evidence_ids
            if evidence_id
        )
    )
    required_domains = _required_source_domains(text)
    selected, missing_identifiers = _matching_identifier_evidence_ids(
        text,
        candidates,
        digest,
    )
    selected = list(selected)
    if not selected and not missing_identifiers:
        for evidence_id in candidates:
            if not _binding_violations(text, (evidence_id,), digest):
                return _replacement_result(
                    text,
                    candidates,
                    (evidence_id,),
                    digest,
                    succeeded=True,
                )
    selected_domains = _evidence_source_domains(selected, digest)
    for domain in sorted(required_domains - selected_domains):
        domain_candidates = tuple(
            evidence_id
            for evidence_id in candidates
            if domain in _evidence_source_domains((evidence_id,), digest)
        )
        if not domain_candidates:
            break
        candidate = min(
            domain_candidates,
            key=lambda evidence_id: len(
                _binding_violations(
                    text,
                    (*selected, evidence_id),
                    digest,
                )
            ),
        )
        selected.append(candidate)
    replacement = tuple(dict.fromkeys(selected))
    if replacement and not missing_identifiers and not _binding_violations(
        text,
        replacement,
        digest,
    ):
        return _replacement_result(text, candidates, replacement, digest, succeeded=True)

    if replacement and missing_identifiers:
        partial_text = _remove_unsupported_identifiers(text, missing_identifiers)
        if partial_text != text and not _binding_violations(
            partial_text,
            replacement,
            digest,
        ):
            return _replacement_result(
                partial_text,
                candidates,
                replacement,
                digest,
                succeeded=True,
                partial=True,
            )

    return _replacement_result(text, candidates, (), digest, succeeded=False)


def _matching_identifier_evidence_ids(
    text: str,
    candidates: Sequence[str],
    digest: FactDigest,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    selected: list[str] = []
    missing: list[str] = []
    for pattern in _IDENTIFIER_PATTERNS:
        for identifier in _identifier_tokens(pattern, text):
            matched = tuple(
                evidence_id
                for evidence_id in candidates
                if identifier in _identifier_tokens(pattern, evidence_id)
            )
            if not matched:
                matched = tuple(
                    card.evidence_ids[0]
                    for card in digest.cards
                    if len(card.evidence_ids) == 1
                    and card.evidence_ids[0] in candidates
                    and identifier
                    in " ".join(_scalar_strings(card.representative)).upper()
                )
            if matched:
                selected.extend(matched)
            else:
                missing.append(identifier)
    return tuple(dict.fromkeys(selected)), tuple(dict.fromkeys(missing))


def _remove_unsupported_identifiers(text: str, identifiers: Sequence[str]) -> str:
    narrowed = text
    for identifier in identifiers:
        escaped = re.escape(identifier)
        updated = re.sub(
            rf"{escaped}\s*(?:와|과|및|,)\s*",
            "",
            narrowed,
            flags=re.IGNORECASE,
        )
        if updated == narrowed:
            updated = re.sub(
                rf"\s*(?:와|과|및|,)\s*{escaped}",
                "",
                narrowed,
                flags=re.IGNORECASE,
            )
        narrowed = updated
    return re.sub(r"\s{2,}", " ", narrowed).strip()


def _replacement_result(
    text: str,
    candidates: Sequence[str],
    evidence_ids: tuple[str, ...],
    digest: FactDigest,
    *,
    succeeded: bool,
    partial: bool = False,
) -> _ReplacementEvidence:
    diagnostics: Counter[str] = Counter(
        rebind_attempted=1,
        rebind_succeeded=int(succeeded),
        rebind_failed=int(not succeeded),
        rebind_partial_attempted=int(partial),
        rebind_partial_succeeded=int(partial and succeeded),
        rebind_candidate_checked=len(candidates),
        rebind_candidate_dropped=max(0, len(candidates) - len(evidence_ids)),
    )
    retained = set(evidence_ids)
    for evidence_id in candidates:
        if evidence_id in retained:
            continue
        reasons = _binding_violations(text, (evidence_id,), digest) or (
            "not_required_by_claim",
        )
        for reason in reasons:
            diagnostics[f"rebind_drop_{reason}"] += 1
    return _ReplacementEvidence(
        evidence_ids=evidence_ids,
        partial_text=text if partial else None,
        diagnostics=tuple(diagnostics.items()),
    )


def filter_group_evidence_ids(
    text: str,
    evidence_ids: Sequence[str],
    digest: FactDigest,
) -> tuple[tuple[str, ...], dict[str, int]]:
    required_domains = _required_source_domains(text)
    unique_ids = tuple(dict.fromkeys(evidence_ids))
    evidence_domains = {
        evidence_id: _evidence_source_domains((evidence_id,), digest)
        for evidence_id in unique_ids
    }
    present_domains = frozenset().union(*evidence_domains.values())
    explicit_domains = _explicit_source_domains(text)
    retained: list[str] = []
    counts = Counter(
        checked=0,
        retained=0,
        mismatched_removed=0,
        unknown_removed=0,
    )
    for evidence_id in unique_ids:
        counts["checked"] += 1
        domains = evidence_domains[evidence_id]
        if not domains:
            counts["unknown_removed"] += 1
        elif (
            required_domains and not domains.intersection(required_domains)
        ) or (
            len(present_domains) > 1
            and explicit_domains
            and not domains.intersection(explicit_domains)
            and not _evidence_directly_supports_text(text, evidence_id, digest)
        ):
            counts["mismatched_removed"] += 1
        else:
            retained.append(evidence_id)
            counts["retained"] += 1
    return tuple(retained), dict(counts)


def _evidence_directly_supports_text(
    text: str,
    evidence_id: str,
    digest: FactDigest,
) -> bool:
    evidence_text = _evidence_text((evidence_id,), digest).upper()
    for pattern in _IDENTIFIER_PATTERNS:
        identifiers = _identifier_tokens(pattern, text)
        if identifiers and any(identifier in evidence_text for identifier in identifiers):
            return True

    values = _evidence_values((evidence_id,), digest)
    text_numbers = tuple(
        value
        for match in _CLAIM_VALUE_RE.finditer(_DATE_RE.sub("", text))
        if (value := _decimal(match.group("value"))) is not None
    )
    if any(_number_present(value, values.numbers) for value in text_numbers):
        return True
    return bool(set(_date_tokens(text)) & set(values.dates))


def _explicit_source_domains(text: str) -> frozenset[str]:
    normalized = text.casefold()
    domains: set[str] = set()
    if "clinicaltrials.gov" in normalized:
        domains.add("clinical")
    if "식약처 의약품 특허목록" in text or "의약품 특허목록" in text:
        domains.add("patent")
    if (
        "openfda" in normalized
        or re.search(r"(?<![a-z])fda(?![a-z])", normalized)
        or "미국 식품의약국" in text
    ):
        domains.add("openfda")
    if "웹 검색" in text:
        domains.add("web")
    if any(term in text for term in ("내부 데이터마트", "데이터마트 자료")):
        domains.add("mart")
    if any(term in text for term in ("건강보험심사평가원", "심평원")):
        domains.add("hira")
    if any(term in text for term in ("업로드 문서", "업로드 파일")):
        domains.add("document")
    return frozenset(domains)


def _identifier_tokens(pattern: re.Pattern[str], text: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(match.group(0).upper() for match in pattern.finditer(text))
    )


def _selected_cards(
    evidence_ids: Sequence[str],
    digest: FactDigest,
) -> tuple[Any, ...]:
    selected = set(evidence_ids)
    metric_entities: set[str] = set()
    for metric in digest.derived_metrics:
        if metric.id in selected:
            selected.update(metric.inputs)
            entity = " ".join(str(metric.entity or "").casefold().split())
            if entity:
                metric_entities.add(entity)
    return tuple(
        card
        for card in digest.cards
        if selected.intersection(card.evidence_ids)
        or " ".join(str(card.entity or "").casefold().split()) in metric_entities
    )


def _evidence_text(evidence_ids: Sequence[str], digest: FactDigest) -> str:
    values: list[str] = list(evidence_ids)
    for card in _selected_cards(evidence_ids, digest):
        values.extend(_scalar_strings(card.model_dump(mode="json")))
    return " ".join(values)


def _evidence_source_domains(
    evidence_ids: Sequence[str],
    digest: FactDigest,
) -> frozenset[str]:
    domains: set[str] = set()
    for card in _selected_cards(evidence_ids, digest):
        source = str(card.source).casefold()
        if source.startswith("patent") or "patent" in source:
            domains.add("patent")
        elif source in {"clinicaltrials", "ct"} or "clinical" in source:
            domains.add("clinical")
        elif source in {"openfda", "fda"} or "openfda" in source:
            domains.add("openfda")
        elif source in {"web", "web_search", "tavily"} or "web" in source:
            domains.add("web")
        elif source == "hira" or "hira" in source:
            domains.add("hira")
        elif source.startswith("document"):
            domains.add("document")
        elif source in {"mart", "market"} or "mart" in source:
            domains.add("mart")
        elif source == "nedrug":
            domains.add("nedrug")
    return frozenset(domains)


def _required_source_domains(text: str) -> frozenset[str]:
    normalized = text.casefold()
    domains: set[str] = set()
    if _NCT_RE.search(text) or "임상" in text:
        domains.add("clinical")
    if _PATENT_RE.search(text) or "특허" in text:
        domains.add("patent")
    if any(
        term in normalized
        for term in ("openfda", "fda", "미국 식품의약국", "이상사례", "부작용")
    ):
        domains.add("openfda")
    if any(term in text for term in ("뉴스", "기사", "언론", "보도", "웹")):
        domains.add("web")
    if _KCD_RE.search(text) or any(
        term in text for term in ("HIRA", "건강보험", "심사평가원", "환자수", "상병")
    ):
        domains.add("hira")
    if any(term in text for term in ("업로드", "문서", "파일", "시트")) or re.search(
        r"\.(?:pdf|xlsx?|csv)(?=$|[^a-z0-9])",
        normalized,
    ):
        domains.add("document")
    if "document" not in domains and any(
        term in text
        for term in (
            "매출",
            "시장점유율",
            "점유율",
            "성장률",
            "순위",
            "경쟁 구도",
            "시장 참여자",
        )
    ):
        domains.add("mart")
    return frozenset(domains)


def _verify_claim(claim: Any, digest: FactDigest) -> tuple[Any | None, ClaimVerification]:
    claim_type = _enum_value(claim.claim_type)
    normalized = _normalize_internal_field_labels(claim.text.strip())
    normalized = _qualify_patent_selection_title(normalized)
    if _INTERNAL_FIELD_RE.search(normalized):
        return None, ClaimVerification(
            ClaimAction.BLOCKED,
            HARD_INTERNAL_FIELD,
        )

    if claim_type == "CITE":
        return _verify_cite(claim, normalized, digest)
    if claim_type == "CALC":
        return _verify_calc(claim, normalized, digest)
    if claim_type == "OBS":
        return _verify_observation(claim, normalized, digest)
    if claim_type == "INTERP":
        return _verify_interpretation(claim, normalized, digest)
    if claim_type == "HYPO":
        if _has_unobserved_behavior(normalized, claim.evidence_ids, digest):
            text = _prefix_once(
                _grounded_metric_interpretation(claim.evidence_ids, digest),
                "[가설]",
            )
            return _updated(claim, text, hedge="hypothesis"), ClaimVerification(
                ClaimAction.SOFTENED,
                HARD_FABRICATED_OBSERVATION,
            )
        text = _prefix_once(normalized, "[가설]")
        return _updated(claim, text), ClaimVerification(ClaimAction.LABELED)
    return _updated(claim, normalized), ClaimVerification(ClaimAction.PASSED)


def _qualify_patent_selection_title(text: str) -> str:
    if "기준" in text or not _UNQUALIFIED_PATENT_TITLE_RE.search(text):
        return text
    return _UNQUALIFIED_PATENT_TITLE_RE.sub(
        "등록 상태 중 만료일이 가장 늦은 제품특허",
        text,
    )


def _product_patent_scope(
    digest: FactDigest,
) -> tuple[bool, frozenset[str], str | None]:
    has_patent_card = False
    product_numbers: set[str] = set()
    representative_number: str | None = None
    for card in digest.cards:
        if card.card_type not in {"patent", "patent_core"}:
            continue
        has_patent_card = True
        product_numbers.update(
            str(value).strip()
            for value in card.full_stats.get("product_patent_numbers", ())
            if str(value).strip()
        )
        candidate = str(card.representative.get("patent_no") or "").strip()
        if candidate:
            representative_number = candidate
    if representative_number not in product_numbers:
        representative_number = min(product_numbers) if product_numbers else None
    return has_patent_card, frozenset(product_numbers), representative_number


def _enforce_product_patent_scope(
    text: str,
    digest: FactDigest,
) -> _PatentScopeCheck:
    has_patent_card, product_numbers, replacement = _product_patent_scope(digest)
    numbers = tuple(match.group(0) for match in _PATENT_RE.finditer(text))
    if not has_patent_card:
        return _PatentScopeCheck(text=text, checked_number_count=len(numbers))
    if not numbers:
        if product_numbers or "제품특허" not in text:
            return _PatentScopeCheck(text=text)
        updated = text.replace(
            "등록 상태 중 만료일이 가장 늦은 제품특허",
            "제품특허 해당 없음",
        )
        return _PatentScopeCheck(
            text=updated,
            rewrite_count=int(updated != text),
            action="rewritten" if updated != text else "none",
        )

    invalid_numbers = tuple(number for number in numbers if number not in product_numbers)
    if not invalid_numbers:
        return _PatentScopeCheck(text=text, checked_number_count=len(numbers))

    updated = text
    if replacement:
        for invalid_number in dict.fromkeys(invalid_numbers):
            updated = updated.replace(invalid_number, replacement)
        return _PatentScopeCheck(
            text=updated,
            checked_number_count=len(numbers),
            violation_count=len(invalid_numbers),
            replacement_count=len(invalid_numbers),
            action="replaced",
        )

    for invalid_number in dict.fromkeys(invalid_numbers):
        updated = updated.replace(invalid_number, "제품특허 해당 없음")
    updated = updated.replace(
        "등록 상태 중 만료일이 가장 늦은 제품특허",
        "제품특허 해당 없음",
    )
    updated = re.sub(
        r"(?:제품특허 해당 없음)(?:으로 식별되는\s*)?(?:제품특허 해당 없음)",
        "제품특허 해당 없음",
        updated,
    )
    return _PatentScopeCheck(
        text=updated,
        checked_number_count=len(numbers),
        violation_count=len(invalid_numbers),
        rewrite_count=1,
        action="rewritten",
    )


def _qualify_entity_specificity(
    text: str,
    evidence_ids: Sequence[str],
    digest: FactDigest,
) -> tuple[str, str]:
    qualified_patent = "등록 상태 중 만료일이 가장 늦은 제품특허"
    if qualified_patent in text:
        patent_id = claim_identifiers(evidence_ids, digest).get("특허")
        if patent_id and patent_id not in text:
            return (
                text.replace(
                    qualified_patent,
                    f"{qualified_patent} {patent_id}",
                ),
                "supplemented",
            )
    if not _VAGUE_ENTITY_RE.search(text):
        return text, "none"

    identifiers = claim_identifiers(evidence_ids, digest)

    def replace(match: re.Match[str]) -> str:
        label = match.group("label")
        kind = match.group("kind")
        category = "clinical" if kind.startswith("임상") else kind
        identifier = identifiers.get(category)
        if identifier:
            basis = "최신 갱신" if label.replace(" ", "") in {"가장최근", "최신"} else "참조 근거"
            if kind == "기간":
                return f"참조 기간 {identifier}"
            if kind.startswith("임상"):
                return f"{basis} 임상시험 {identifier}"
            return f"{basis} {kind} {identifier}"
        if kind.startswith("임상"):
            return "직접 관련 임상 중 최신 갱신 건" if label.replace(" ", "") in {"가장최근", "최신"} else "직접 관련 임상"
        if kind == "특허":
            return "직접 관련 제품특허 중 선정 기준에 맞는 건"
        return f"참조 근거에 포함된 {kind}"

    updated = _VAGUE_ENTITY_RE.sub(replace, text)
    return updated, "supplemented" if any(identifiers.values()) else "rewritten"


def claim_identifiers(
    evidence_ids: Sequence[str],
    digest: FactDigest,
) -> dict[str, str]:
    requested = set(evidence_ids)
    for metric in digest.derived_metrics:
        if metric.id in requested:
            requested.update(metric.inputs)
    values: list[str] = []
    entities: list[str] = []
    for card in digest.cards:
        if not requested.intersection(card.evidence_ids):
            continue
        entities.append(card.entity)
        values.extend(_scalar_strings(card.model_dump(mode="json")))
    direct = text_identifiers(" ".join(requested))
    joined = " ".join(values)
    identifiers = {
        "clinical": direct["clinical"] or _first_match(_NCT_RE, joined),
        "특허": direct["특허"] or _first_match(_PATENT_RE, joined),
        "상병": direct["상병"] or _first_match(_KCD_RE, joined),
        "질환": direct["질환"] or _first_match(_KCD_RE, joined),
        "기간": direct["기간"] or _first_match(_PERIOD_RE, joined),
        "브랜드": next((entity for entity in entities if entity), ""),
        "파일": _mapping_value(values, (".pdf", ".xlsx", ".xls", ".csv")),
        "시트": "",
    }
    return identifiers


def text_identifiers(text: str) -> dict[str, str]:
    """Extract public identifiers with the same policy used by claim specificity."""
    disease = _first_match(_KCD_RE, text)
    return {
        "clinical": _first_match(_NCT_RE, text),
        "특허": _first_match(_PATENT_RE, text),
        "상병": disease,
        "질환": disease,
        "기간": _first_match(_PERIOD_RE, text),
    }


def _scalar_strings(value: Any) -> list[str]:
    if isinstance(value, Mapping):
        return [item for nested in value.values() for item in _scalar_strings(nested)]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [item for nested in value for item in _scalar_strings(nested)]
    return [str(value)] if value not in (None, "") else []


def _first_match(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(0) if match else ""


def _mapping_value(values: Sequence[str], suffixes: Sequence[str]) -> str:
    return next(
        (value for value in values if value.casefold().endswith(tuple(suffixes))),
        "",
    )


def _verify_cite(
    claim: Any,
    text: str,
    digest: FactDigest,
) -> tuple[Any | None, ClaimVerification]:
    evidence = _evidence_values(claim.evidence_ids, digest)
    if any(date not in evidence.dates for date in _date_tokens(text)):
        return None, ClaimVerification(ClaimAction.BLOCKED, HARD_FABRICATED_VALUE)

    unmatched = [
        (start, end, raw, value)
        for start, end, raw, value in _number_token_spans_without_dates(text)
        if not _number_present(value, evidence.numbers)
    ]
    if not unmatched:
        action = ClaimAction.CORRECTED if text != claim.text else ClaimAction.PASSED
        return _updated(claim, text), ClaimVerification(action)

    replacements: list[tuple[int, int, str]] = []
    for start, end, _raw, value in unmatched:
        replacement = _rounding_equivalent(value, evidence.numbers)
        if replacement is None:
            return None, ClaimVerification(ClaimAction.BLOCKED, HARD_FABRICATED_VALUE)
        replacements.append((start, end, _display_decimal(replacement)))
    corrected = text
    for start, end, replacement in sorted(replacements, reverse=True):
        corrected = f"{corrected[:start]}{replacement}{corrected[end:]}"
    return _updated(claim, corrected), ClaimVerification(ClaimAction.CORRECTED)


def _verify_calc(
    claim: Any,
    text: str,
    digest: FactDigest,
) -> tuple[Any | None, ClaimVerification]:
    evidence = _evidence_values(claim.evidence_ids, digest)
    if not evidence.metric_values:
        return _verify_cite(claim, text, digest)

    corrected = text
    changed = text != claim.text
    units = dict.fromkeys(unit for _value, unit in evidence.metric_values if unit)
    for unit in units:
        expected = [
            value for value, metric_unit in evidence.metric_values if metric_unit == unit
        ]
        matches = _numeric_tokens_before_unit(corrected, unit)
        remaining = list(expected)
        unmatched: list[tuple[int, int, Decimal]] = []
        replacements: list[tuple[int, int, str]] = []
        for start, end, actual in matches:
            matched = _pop_matching_number(actual, remaining)
            if matched is None:
                unmatched.append((start, end, actual))
                continue
            display = _display_decimal(matched)
            if corrected[start:end] != display:
                replacements.append((start, end, display))
        if unmatched and len(unmatched) != len(remaining):
            return _verify_cite(claim, text, digest)
        if unmatched:
            for (start, end, _actual), replacement in zip(unmatched, remaining, strict=True):
                replacements.append((start, end, _display_decimal(replacement)))
        for start, end, replacement in sorted(replacements, reverse=True):
            corrected = f"{corrected[:start]}{replacement}{corrected[end:]}"
            changed = True
    corrected = _normalize_direction_magnitude(corrected)
    changed = changed or corrected != text
    action = ClaimAction.CORRECTED if changed else ClaimAction.PASSED
    return _updated(claim, corrected), ClaimVerification(action)


def _verify_observation(
    claim: Any,
    text: str,
    digest: FactDigest,
) -> tuple[Any | None, ClaimVerification]:
    direction_delta = _direction_metric_value(claim.evidence_ids, digest)
    corrected = text
    if direction_delta is not None and direction_delta != 0:
        if _has_rank_metric(claim.evidence_ids, digest):
            corrected, transition_corrected = _normalize_rank_transition(
                corrected,
                claim.evidence_ids,
                digest,
            )
            replacements = (
                {}
                if transition_corrected
                else (
                    _RANK_IMPROVEMENT_REPLACEMENTS
                    if direction_delta > 0
                    else _RANK_DECLINE_REPLACEMENTS
                )
            )
        else:
            replacements = (
                _POSITIVE_DIRECTION if direction_delta > 0 else _NEGATIVE_DIRECTION
            )
        for wrong, right in replacements.items():
            corrected = corrected.replace(wrong, right)
    corrected = _normalize_direction_magnitude(corrected)
    if corrected != text or text != claim.text:
        return _updated(claim, corrected), ClaimVerification(ClaimAction.CORRECTED)
    return _updated(claim, text), ClaimVerification(ClaimAction.PASSED)


def _normalize_rank_transition(
    text: str,
    evidence_ids: Sequence[str],
    digest: FactDigest,
) -> tuple[str, bool]:
    selected = frozenset(evidence_ids)
    metric = next(
        (
            candidate
            for candidate in digest.derived_metrics
            if candidate.id in selected and candidate.type == "rank_delta"
        ),
        None,
    )
    if metric is None:
        return text, False
    formula_match = re.fullmatch(
        r"\s*(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)\s*",
        str(metric.formula or ""),
    )
    if formula_match is None:
        return text, False
    end_rank = int(Decimal(formula_match.group(1)))
    start_rank = int(Decimal(formula_match.group(2)))
    direction = "상승" if end_rank < start_rank else "하락"
    matching_verbs = (
        {"상승", "도약", "올라섰"}
        if direction == "상승"
        else {"하락", "밀려남", "밀려났", "후퇴"}
    )

    def replace(match: re.Match[str]) -> str:
        verb = match.group("verb")
        return (
            f"{start_rank}위에서 {end_rank}위로"
            f"{match.group('middle')}{verb if verb in matching_verbs else direction}"
        )

    corrected, transition_count = _RANK_TRANSITION_RE.subn(replace, text)

    expected_words = (
        _RANK_IMPROVEMENT_WORDS if direction == "상승" else _RANK_DECLINE_WORDS
    )
    style_replacements = (
        _RANK_IMPROVEMENT_STYLE if direction == "상승" else _RANK_DECLINE_STYLE
    )

    def replace_context(match: re.Match[str]) -> str:
        verb = match.group("verb")
        replacement = verb if verb in expected_words else style_replacements.get(verb, verb)
        return f"{match.group('prefix')}{replacement}"

    corrected, context_count = _RANK_CONTEXT_RE.subn(replace_context, corrected)
    return corrected, transition_count > 0 or context_count > 0


def _direction_metric_value(
    evidence_ids: Sequence[str],
    digest: FactDigest,
) -> Decimal | None:
    selected = frozenset(evidence_ids)
    selected_metrics = tuple(
        metric for metric in digest.derived_metrics if metric.id in selected
    )
    share_series = sorted(
        (
            metric
            for metric in selected_metrics
            if metric.type == "ms_share" and _decimal(metric.value) is not None
        ),
        key=lambda metric: str(metric.period or ""),
    )
    if len(share_series) >= 2:
        first = _decimal(share_series[0].value)
        last = _decimal(share_series[-1].value)
        if first is not None and last is not None:
            return last - first
    candidates = sorted(
        (
            metric
            for metric in selected_metrics
            if metric.type in _DIRECTION_METRIC_PRIORITY
        ),
        key=lambda metric: _DIRECTION_METRIC_PRIORITY[metric.type],
    )
    if not candidates:
        return None
    values = tuple(
        -value if metric.type == "rank_delta" else value
        for metric in candidates
        if (value := _decimal(metric.value)) is not None
    )
    directions = {1 if value > 0 else -1 for value in values if value != 0}
    if len(directions) > 1:
        return None
    return values[0] if values else None


def _normalize_direction_magnitude(text: str) -> str:
    return _SIGNED_DIRECTION_VALUE_RE.sub(
        lambda match: (
            f"{match.group('value')}{match.group('suffix')}"
            f"{match.group('middle')}{match.group('verb')}"
        ),
        text,
    )


def _verify_interpretation(
    claim: Any,
    text: str,
    digest: FactDigest,
) -> tuple[Any | None, ClaimVerification]:
    if _has_unobserved_behavior(text, claim.evidence_ids, digest):
        softened = _grounded_metric_interpretation(claim.evidence_ids, digest)
        return _updated(claim, softened, hedge="softened"), ClaimVerification(
            ClaimAction.SOFTENED,
            HARD_FABRICATED_OBSERVATION,
        )
    if not claim.evidence_ids:
        return _updated(claim, _prefix_once(text, "[해석]")), ClaimVerification(
            ClaimAction.LABELED
        )
    action = ClaimAction.CORRECTED if text != claim.text else ClaimAction.PASSED
    return _updated(claim, text), ClaimVerification(action)


def _has_unobserved_behavior(
    text: str,
    evidence_ids: Sequence[str],
    digest: FactDigest,
) -> bool:
    if not (_FABRICATED_BEHAVIOR_RE.search(text) or _ACTOR_BEHAVIOR_RE.search(text)):
        return False
    return not _evidence_contains_behavior_observation(evidence_ids, digest)


def _evidence_contains_behavior_observation(
    evidence_ids: Sequence[str],
    digest: FactDigest,
) -> bool:
    selected = frozenset(evidence_ids)
    referenced_cards = (
        card for card in digest.cards if selected.intersection(card.evidence_ids)
    )
    for card in referenced_cards:
        payload = card.model_dump_json()
        if any(actor in payload for actor in _BEHAVIOR_ACTORS) and any(
            predicate in payload for predicate in _BEHAVIOR_PREDICATES
        ):
            return True
    return False


def _grounded_metric_interpretation(
    evidence_ids: Sequence[str],
    digest: FactDigest,
) -> str:
    selected = frozenset(evidence_ids)
    metric = next(
        (item for item in digest.derived_metrics if item.id in selected),
        None,
    )
    if metric is None:
        return (
            "관측된 수치 변화는 경쟁 구도 변화 가능성을 보여주지만, "
            "시장 참여자의 행동 변화까지 단정할 수는 없습니다."
        )
    value = _decimal(metric.value)
    display_value = _display_decimal(value) if value is not None else str(metric.value)
    label = _METRIC_SURFACE_LABELS.get(metric.type, "관측값")
    entity = metric.entity or "해당 대상"
    period = f"{metric.period} " if metric.period else ""
    signed = value is not None and value < 0
    magnitude = _display_decimal(abs(value)) if value is not None else display_value
    if metric.type == "growth_spread_vs_market":
        direction = "밑돌았습니다" if signed else "웃돌았습니다"
        return f"{entity}의 {period}성장률은 시장보다 {magnitude}{metric.unit} {direction}."
    if metric.type in {"gap_change", "share_delta", "rank_delta"}:
        direction = "축소됐습니다" if signed else "확대됐습니다"
        return f"{entity}의 {period}{label}은 {magnitude}{metric.unit} {direction}."
    if metric.type in {"brand_growth_rate", "market_growth_rate", "yearly_growth", "cagr"}:
        direction = "감소했습니다" if signed else "증가했습니다"
        return f"{entity}의 {period}{label}은 {magnitude}{metric.unit}로 {direction}."
    if metric.type.startswith("count_by_status") or metric.unit == "count":
        status = str(metric.entity or "").split()[-1]
        status_label = {
            "COMPLETED": "완료된 임상시험",
            "RECRUITING": "모집 중인 임상시험",
            "ACTIVE_NOT_RECRUITING": "진행 중인 임상시험",
        }.get(status, f"{entity} 관련 임상시험")
        return f"{period}{status_label}은 {display_value}건입니다."
    return f"{entity}의 {period}{label}은 {display_value}{metric.unit}입니다."


def _evidence_values(evidence_ids: Sequence[str], digest: FactDigest) -> _EvidenceValues:
    selected = frozenset(evidence_ids)
    values: list[Any] = []
    metric_values: list[tuple[Decimal, str]] = []
    for card in digest.cards:
        if selected.intersection(card.evidence_ids):
            values.extend(_flatten_scalars(card.model_dump(mode="python")))
    for metric in digest.derived_metrics:
        if metric.id not in selected:
            continue
        values.extend((metric.value, metric.period))
        number = _decimal(metric.value)
        if number is not None:
            metric_values.append((number, metric.unit))

    numbers: dict[Decimal, None] = {}
    dates: dict[str, None] = {}
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        for date in _date_tokens(str(value)):
            dates.setdefault(date, None)
        for _raw, number in _number_tokens_without_dates(str(value)):
            numbers.setdefault(number, None)
        direct = _decimal(value)
        if direct is not None:
            numbers.setdefault(direct, None)
    return _EvidenceValues(tuple(numbers), tuple(dates), tuple(metric_values))


def _flatten_scalars(value: Any) -> list[Any]:
    if isinstance(value, Mapping):
        output: list[Any] = []
        for key, nested in value.items():
            if key in {"evidence_ids", "derived_fields"}:
                continue
            output.extend(_flatten_scalars(nested))
        return output
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [item for nested in value for item in _flatten_scalars(nested)]
    return [value]


def _date_tokens(text: str) -> tuple[str, ...]:
    return tuple(_normalize_date(match.group(0)) for match in _DATE_RE.finditer(text))


def _normalize_date(value: str) -> str:
    parts = re.findall(r"\d+", value)
    if len(parts) == 2:
        return f"{int(parts[0]):04d}-{int(parts[1]):02d}"
    if len(parts) >= 3:
        return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    return value


def _number_tokens_without_dates(text: str) -> tuple[tuple[str, Decimal], ...]:
    return tuple(
        (raw, value)
        for _start, _end, raw, value in _number_token_spans_without_dates(text)
    )


def _number_token_spans_without_dates(
    text: str,
) -> tuple[tuple[int, int, str, Decimal], ...]:
    without_dates = _DATE_RE.sub(lambda match: " " * len(match.group(0)), text)
    duration_or_count = re.compile(r"\s*(?:년|개월|회)(?![가-힣])")
    output: list[tuple[int, int, str, Decimal]] = []
    for match in _NUMBER_RE.finditer(without_dates):
        if duration_or_count.match(without_dates, match.end()):
            continue
        value = _decimal(match.group(0))
        if value is not None:
            output.append((match.start(), match.end(), match.group(0), value))
    return tuple(output)


def _numeric_tokens_before_unit(
    text: str,
    unit: str,
) -> tuple[tuple[int, int, Decimal], ...]:
    if not unit:
        return ()
    pattern = re.compile(
        rf"(?P<value>[-+]?\d[\d,]*(?:\.\d+)?)\s*{re.escape(unit)}"
    )
    output: list[tuple[int, int, Decimal]] = []
    for match in pattern.finditer(text):
        value = _decimal(match.group("value"))
        if value is not None:
            output.append((match.start("value"), match.end("value"), value))
    return tuple(output)


def _pop_matching_number(value: Decimal, candidates: list[Decimal]) -> Decimal | None:
    for index, candidate in enumerate(candidates):
        if value == candidate or _rounding_equivalent(value, (candidate,)) is not None:
            return candidates.pop(index)
    return None


def _number_present(value: Decimal, candidates: Sequence[Decimal]) -> bool:
    scale = Decimal(100000000)
    return any(
        value == candidate or value == candidate * scale or value * scale == candidate
        for candidate in candidates
    )


def _rounding_equivalent(
    value: Decimal,
    candidates: Sequence[Decimal],
) -> Decimal | None:
    decimal_places = max(0, -value.as_tuple().exponent)
    scale = Decimal(1).scaleb(-decimal_places)
    for candidate in candidates:
        if value == candidate.quantize(scale):
            return candidate
    return None


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _display_decimal(value: Decimal) -> str:
    if value == value.to_integral_value():
        return format(value.quantize(Decimal(1)), ",f")
    return format(value.quantize(Decimal("0.01")), ",f")


def _updated(
    claim: Any,
    text: str,
    *,
    hedge: str | None = None,
    evidence_ids: Sequence[str] | None = None,
    claim_type: str | None = None,
) -> Any:
    updates: dict[str, object] = {"text": text}
    if hedge is not None:
        updates["hedge"] = hedge
    if evidence_ids is not None:
        updates["evidence_ids"] = tuple(evidence_ids)
    if claim_type is not None:
        updates["claim_type"] = type(claim.claim_type)(claim_type)
    return claim.model_copy(update=updates)


def _prefix_once(text: str, prefix: str) -> str:
    return text if text.startswith(prefix) else f"{prefix} {text}"


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


__all__ = [
    "HARD_FABRICATED_OBSERVATION",
    "HARD_FABRICATED_VALUE",
    "HARD_INTERNAL_FIELD",
    "ClaimAction",
    "ClaimVerification",
    "VerifiedClaims",
    "claim_identifiers",
    "text_identifiers",
    "verify_structured_claims",
]
