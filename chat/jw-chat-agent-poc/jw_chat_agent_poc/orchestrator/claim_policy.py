from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Final

from jw_chat_agent_poc.service.markdown_cleanup import cleanup_markdown_answer


FORBIDDEN_BY_FACT_TYPE: Final[dict[str, tuple[str, ...]]] = {
    "channel_cross_section": (
        "clinical_evidence",
        "clinical_preference",
        "brand_loyalty",
        "standard_of_care",
        "prescription_transfer",
        "trickle_down",
        "premium_positioning",
        "patient_severity_causal",
        "cash_cow_unverified",
    )
}

_FORBIDDEN_PATTERNS_BY_CLAIM: Final[dict[str, re.Pattern[str]]] = {
    "clinical_evidence": re.compile(r"(임상(?:적)?\s*근거|임상\s*신뢰|임상\w*\s*입증|입증)"),
    "clinical_preference": re.compile(r"(임상\w*\s*선호|의료진\w*\s*선호|처방\w*\s*선호)"),
    "brand_loyalty": re.compile(r"(로열티|충성도|충성\s*고객|brand\s*loyalty)", re.IGNORECASE),
    "standard_of_care": re.compile(r"(표준\s*치료제|standard\s*of\s*care)", re.IGNORECASE),
    "prescription_transfer": re.compile(r"(처방\s*전이|처방\s*이동|환자\s*이동|\b전이\b|전이[가-힣]*)"),
    "trickle_down": re.compile(r"(낙수\s*효과|trickle[- ]?down|top[- ]?down)", re.IGNORECASE),
    "premium_positioning": re.compile(r"(프리미엄|premium|quality\s+vs\s+quantity)", re.IGNORECASE),
    "patient_severity_causal": re.compile(r"(중증도|중증\s*환자|고위험\s*환자|환자\s*구성\w*\s*(?:원인|기인|때문))"),
    "cash_cow_unverified": re.compile(r"(cash\s*cow|캐시\s*카우)", re.IGNORECASE),
}

_CHANNEL_FACT_RE: Final = re.compile(r"(?m)^\|\s*channel\s+상위\s*\|")
_CHANNEL_ROW_RE: Final = re.compile(
    r"(?P<rank>\d+)위\s+"
    r"(?P<name>.+?)\s+"
    r"시장점유율\s+(?P<share>[+-]?\d+(?:\.\d+)?%)\s+"
    r"매출\s+(?P<sales>[+-]?\d+(?:,\d{3})*(?:\.\d+)?억원)"
)
_PERIOD_RE: Final = re.compile(r"[12]\d{3}-\d{2}")
_SENTENCE_RE: Final = re.compile(r"[^.!?\n。]+(?:[.!?。]|$)")
_SOURCE_HEADING_RE: Final = re.compile(r"(?m)^#{1,6}\s*출처\b")


@dataclass(frozen=True, slots=True)
class ChannelFact:
    rank: int
    name: str
    share: str
    sales: str


def apply_claim_policy(question: str, answer: str, fact_md: str) -> str:
    """Remove interpretation claims that are not supported by the supplied fact types."""

    body, sources = _split_sources(answer)
    active_fact_types = tuple(
        fact_type for fact_type, detector in _FACT_TYPE_DETECTORS.items() if detector(fact_md)
    )
    revised = body
    for fact_type in active_fact_types:
        claims = FORBIDDEN_BY_FACT_TYPE.get(fact_type, ())
        revised, removed = _drop_forbidden_claim_sentences(revised, claims)
        if removed:
            replacement = _SAFE_REPLACEMENTS[fact_type](question, fact_md)
            if replacement and replacement not in revised:
                revised = "\n\n".join(part for part in (replacement, revised.strip()) if part)
    revised = cleanup_markdown_answer(revised.strip())
    if sources:
        return cleanup_markdown_answer("\n\n".join((revised, sources.strip())))
    return revised


def _is_channel_cross_section(fact_md: str) -> bool:
    if _CHANNEL_FACT_RE.search(fact_md):
        return True
    return "channel 상위" in fact_md and "시장점유율" in fact_md and "매출" in fact_md


def _drop_forbidden_claim_sentences(body: str, claim_types: tuple[str, ...]) -> tuple[str, bool]:
    patterns = tuple(_FORBIDDEN_PATTERNS_BY_CLAIM[claim] for claim in claim_types)
    kept_lines: list[str] = []
    removed_any = False
    for raw_line in body.splitlines():
        if _is_non_analysis_line(raw_line):
            kept_lines.append(raw_line)
            continue
        kept_sentences: list[str] = []
        for sentence in _sentence_parts(raw_line):
            if any(pattern.search(sentence) for pattern in patterns):
                removed_any = True
                continue
            kept_sentences.append(sentence.strip())
        revised = " ".join(part for part in kept_sentences if part).strip()
        if revised:
            kept_lines.append(revised)
    return "\n".join(kept_lines), removed_any


def _channel_safe_summary(question: str, fact_md: str) -> str:
    facts = _channel_facts(fact_md)
    if not facts:
        return ""
    brand = _brand_from_fact_md(fact_md) or _brand_from_question(question)
    period = _period_from_fact_md(fact_md)
    sales_top = facts[:3]
    share_top = sorted(facts, key=lambda item: _numeric_pct(item.share), reverse=True)[:3]
    sales_phrase = ", ".join(f"{item.name} {item.sales}" for item in sales_top)
    share_phrase = ", ".join(f"{item.name} {item.share}" for item in share_top)
    prefix = f"{period} 기준 " if period else ""
    return cleanup_markdown_answer(
        " ".join(
            (
                f"{prefix}{brand} 채널별 매출은 {sales_phrase} 순입니다.",
                f"채널 내 시장점유율은 {share_phrase} 순입니다.",
                f"따라서 매출 볼륨은 {sales_top[0].name}, 상대 점유율 우위는 {share_top[0].name}에서 나타납니다.",
                "이 차이의 원인은 현재 데이터만으로 확인할 수 없으며, 환자 구성·경쟁 제품·처방기관 수·영업 커버리지 데이터를 추가 확인해야 합니다.",
            )
        )
    )


def _channel_facts(fact_md: str) -> tuple[ChannelFact, ...]:
    facts: list[ChannelFact] = []
    for match in _CHANNEL_ROW_RE.finditer(fact_md):
        facts.append(
            ChannelFact(
                rank=int(match.group("rank")),
                name=match.group("name").strip(),
                share=match.group("share"),
                sales=match.group("sales"),
            )
        )
    return tuple(sorted(facts, key=lambda item: item.rank))


def _numeric_pct(value: str) -> float:
    try:
        return float(value.replace("%", "").replace(",", ""))
    except ValueError:
        return 0.0


def _brand_from_fact_md(fact_md: str) -> str:
    match = re.search(r"###\s+(.+?)\s+channel별", fact_md)
    if match:
        return match.group(1).strip()
    return ""


def _brand_from_question(question: str) -> str:
    match = re.search(r"([가-힣A-Za-z0-9+._/-]+)\s*채널", question)
    if match:
        return match.group(1).strip()
    return "해당 브랜드"


def _period_from_fact_md(fact_md: str) -> str:
    matches = _PERIOD_RE.findall(fact_md)
    return matches[-1] if matches else ""


def _sentence_parts(line: str) -> tuple[str, ...]:
    decimal_dot = "__CLAIM_POLICY_DECIMAL_DOT__"
    protected = re.sub(r"(?<=\d)\.(?=\d)", decimal_dot, line)
    parts = tuple(
        match.group(0).replace(decimal_dot, ".").strip()
        for match in _SENTENCE_RE.finditer(protected)
        if match.group(0).strip()
    )
    return parts or ((line.strip(),) if line.strip() else ())


def _is_non_analysis_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if stripped.startswith(("#", "|", "-", "*", ">")):
        return True
    return bool(re.search(r"https?://", stripped))


def _split_sources(answer: str) -> tuple[str, str]:
    match = _SOURCE_HEADING_RE.search(answer)
    if not match:
        return answer, ""
    return answer[: match.start()], answer[match.start() :]


_FACT_TYPE_DETECTORS: Final[dict[str, Callable[[str], bool]]] = {
    "channel_cross_section": _is_channel_cross_section,
}

_SAFE_REPLACEMENTS: Final[dict[str, Callable[[str, str], str]]] = {
    "channel_cross_section": _channel_safe_summary,
}
