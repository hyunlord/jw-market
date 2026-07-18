from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Final


FORBIDDEN_BY_FACT_TYPE: Final[dict[str, tuple[str, ...]]] = {
    "channel_cross_section": (
        "causal_analysis_unverified",
        "clinical_evidence",
        "clinical_preference",
        "brand_loyalty",
        "standard_of_care",
        "prescription_transfer",
        "trickle_down",
        "premium_positioning",
        "patient_severity_causal",
        "cash_cow_unverified",
    ),
    "brand_share_delta": (
        "direct_switching",
        "cannibalization",
        "absorption_replacement",
        "causal_competition_win",
    ),
    "news_context": (
        "quantified_sales_impact",
        "causal_market_impact_without_metric",
        "news_claim_elevation",
    ),
    "external_clinical_registry": (
        "clinical_evidence",
        "registry_outcome_elevation",
        "registry_market_inference",
    ),
}

_FORBIDDEN_PATTERNS_BY_CLAIM: Final[dict[str, re.Pattern[str]]] = {
    "causal_analysis_unverified": re.compile(r"(인과\s*분석|causal\s+analysis)", re.IGNORECASE),
    "clinical_evidence": re.compile(r"(임상(?:적)?\s*근거|임상\s*신뢰|임상\w*\s*입증|입증)"),
    "clinical_preference": re.compile(r"(임상\w*\s*선호|의료진\w*\s*선호|처방\w*\s*선호)"),
    "brand_loyalty": re.compile(r"(로열티|충성도|충성\s*고객|brand\s*loyalty)", re.IGNORECASE),
    "standard_of_care": re.compile(r"(표준\s*치료제|standard\s*of\s*care)", re.IGNORECASE),
    "prescription_transfer": re.compile(r"(처방\s*전이|처방\s*이동|환자\s*이동|\b전이\b|전이[가-힣]*)"),
    "trickle_down": re.compile(r"(낙수\s*효과|trickle[- ]?down|top[- ]?down)", re.IGNORECASE),
    "premium_positioning": re.compile(r"(프리미엄|premium|quality\s+vs\s+quantity)", re.IGNORECASE),
    "patient_severity_causal": re.compile(r"(중증도|중증\s*환자|고위험\s*환자|환자\s*구성\w*\s*(?:원인|기인|때문))"),
    "cash_cow_unverified": re.compile(r"(cash\s*cow|캐시\s*카우)", re.IGNORECASE),
    "direct_switching": re.compile(r"([가-힣A-Za-z0-9+._/-]+에서\s*[가-힣A-Za-z0-9+._/-]+로\s*(?:직접\s*)?(?:처방\s*)?(?:전환|이동)(?:이\s*)?(?:발생|확인|이어|됐|되었|했다)|직접\s*(?:처방\s*)?(?:전환|이동)(?:이\s*)?(?:발생|이어|됐|되었|했다))"),
    "cannibalization": re.compile(r"(자기\s*잠식|카니발리[제제]이션|cannibali[sz]ation|잠식(?:했|한|한다|했다|효과))", re.IGNORECASE),
    "absorption_replacement": re.compile(r"(흡수(?:했|한|한다|했다|됐다|되었다)|대체(?:했|한|한다|했다|됐다|되었다)|흡수[/·]\s*대체)"),
    "causal_competition_win": re.compile(r"(경쟁(?:에서)?\s*(?:이겨|승리|우위).{0,30}점유율.{0,20}(?:가져|확보)|점유율을\s*(?:가져왔|빼앗|탈환))"),
    "quantified_sales_impact": re.compile(r"(뉴스|이슈|기사).{0,40}(?:때문에|영향으로|기인해).{0,40}(?:매출|점유율).{0,20}\d[\d,.]*(?:억원|%|%p).{0,20}(?:증가|감소|상승|하락)"),
    "causal_market_impact_without_metric": re.compile(r"(뉴스|이슈|기사).{0,40}(?:때문에|기인|유발|견인|주도).{0,40}(?:매출|점유율|시장|처방)"),
    "news_claim_elevation": re.compile(r"(뉴스|이슈|기사).{0,80}(?:입증|증명|확인됨|확인됐|달성)"),
    "registry_outcome_elevation": re.compile(
        r"(혈관\s*보호\s*효과|안전성\s*프로파일|임상(?:적)?\s*이점|"
        r"보조적\s*치료\s*가능성|처방\s*영역.{0,20}확장|"
        r"안전성(?:을)?\s*확보|약제\s*특성.{0,8}검증|"
        r"임상(?:적)?\s*유용성.{0,8}(?:확인|입증)|혈관\s*건강\s*개선\s*가능성|"
        r"적응증\s*확대|신뢰할\s*수\s*있는\s*치료\s*옵션|최적의\s*치료\s*옵션|"
        r"안전성.{0,8}유효성.{0,16}검증)"
    ),
    "registry_market_inference": re.compile(
        r"(시장\s*선점|복약\s*편의성.{0,20}(?:확대|증가|높)|"
        r"신약.{0,20}(?:등장|출시).{0,20}시사|경쟁(?:이|은)?\s*(?:심화|격화)|"
        r"가능성(?:을)?\s*시사|효율성(?:을)?\s*극대화|방향으로\s*진화|"
        r"폭넓은\s*임상\s*포트폴리오)"
    ),
}

_CHANNEL_FACT_RE: Final = re.compile(r"(?m)^\|\s*channel\s+상위\s*\|")
_CHANNEL_ROW_RE: Final = re.compile(
    r"(?P<rank>\d+)위\s+"
    r"(?P<name>.+?)\s+"
    r"시장점유율\s+(?P<share>[+-]?\d+(?:\.\d+)?%)\s+"
    r"매출\s+(?P<sales>[+-]?\d+(?:,\d{3})*(?:\.\d+)?억원)"
)
_CHANNEL_TABLE_HEADER_RE: Final = re.compile(r"\|\s*채널\s*\|\s*시장점유율\s*\|\s*매출\s*\|")
_CHANNEL_TABLE_ROW_RE: Final = re.compile(
    r"\|\s*(?P<name>[^|]+?)\s*\|\s*"
    r"(?P<share>[+-]?\d+(?:\.\d+)?%)\s*\|\s*"
    r"(?P<sales>[+-]?\d+(?:,\d{3})*(?:\.\d+)?억원)\s*\|"
)
_PERIOD_RE: Final = re.compile(r"[12]\d{3}-\d{2}")
_SENTENCE_RE: Final = re.compile(r"[^.!?\n。]+(?:[.!?。]|$)")
_SOURCE_HEADING_RE: Final = re.compile(r"(?m)^#{1,6}\s*출처\b")
_TIMING_HEADING_RE: Final = re.compile(r"(?m)^#{1,6}\s*처리\s*시간\b")
_URL_RE: Final = re.compile(r"https?://[^\s<>\])]+")
_PURE_SOURCE_LINE_RE: Final = re.compile(
    r"^(?:[-*•]\s*)?(?:(?:출처|참고(?:\s*링크)?|링크)\s*:?\s*)?"
    r"(?:\[[^\]]+\]\(https?://[^)]+\)|https?://\S+)$"
)
_BRAND_SHARE_DELTA_RE: Final = re.compile(
    r"(brand_share_delta_pctp|comparison_share_delta_pctp|브랜드\s+MS\s+변화|비교\s+브랜드\s+MS\s+변화|competitive_insight_signals|brand_trend_comparison)"
)
_NEWS_CONTEXT_RE: Final = re.compile(r"(인사이트\s+근거\s+fact\s*-\s*뉴스/이슈|deep_analysis_related_news|background_news_context|search_news)")


@dataclass(frozen=True, slots=True)
class ChannelFact:
    rank: int
    name: str
    share: str
    sales: str


@dataclass(frozen=True, slots=True)
class AdverseEventFact:
    subject: str
    report_id: str
    report_date: str
    reactions: str


def apply_claim_policy(question: str, answer: str, fact_md: str) -> str:
    """Remove interpretation claims that are not supported by the supplied fact types."""

    body, sources = _split_sources(answer)
    active_fact_types = _active_fact_types(body, fact_md)
    revised = body
    for fact_type in active_fact_types:
        if fact_type == "channel_cross_section":
            support_md = "\n\n".join(part for part in (fact_md.strip(), revised.strip()) if part)
            revised = _rewrite_channel_cross_section(question, revised, support_md)
            continue
        if fact_type == "external_adverse_event":
            revised = _rewrite_external_adverse_event(revised, fact_md)
            continue
        claims = FORBIDDEN_BY_FACT_TYPE.get(fact_type, ())
        revised, removed = _drop_forbidden_claim_sentences(revised, claims)
        if removed:
            replacement_builder = _SAFE_REPLACEMENTS.get(fact_type)
            replacement = replacement_builder(question, fact_md) if replacement_builder else ""
            if replacement and replacement not in revised:
                revised = "\n\n".join(part for part in (replacement, revised.strip()) if part)
    revised = _cleanup_policy_markdown(revised.strip())
    if sources:
        return _cleanup_policy_markdown("\n\n".join((revised, sources.strip())))
    return revised


def claim_policy_report(answer: str, fact_md: str) -> dict[str, tuple[str, ...]]:
    """Report active fact types and forbidden claims still present after policy application."""

    body, _sources = _split_sources(answer)
    active_fact_types = _active_fact_types(body, fact_md)
    remaining: list[str] = []
    for fact_type in active_fact_types:
        for claim_type in FORBIDDEN_BY_FACT_TYPE.get(fact_type, ()):
            pattern = _FORBIDDEN_PATTERNS_BY_CLAIM[claim_type]
            if _has_forbidden_analysis_claim(body, pattern):
                remaining.append(claim_type)
    return {
        "active_fact_types": active_fact_types,
        "forbidden_claims_remaining": tuple(dict.fromkeys(remaining)),
    }


def _is_channel_cross_section(fact_md: str) -> bool:
    if _CHANNEL_FACT_RE.search(fact_md):
        return True
    return "channel 상위" in fact_md and "시장점유율" in fact_md and "매출" in fact_md


def _is_brand_share_delta(fact_md: str) -> bool:
    return bool(_BRAND_SHARE_DELTA_RE.search(fact_md))


def _is_news_context(fact_md: str) -> bool:
    return bool(_NEWS_CONTEXT_RE.search(fact_md))


def _is_external_clinical_registry(fact_md: str) -> bool:
    legacy_registry = "[ClinicalTrials.gov 임상시험 정보]" in fact_md or bool(
        re.search(r"국내\s*임상시험\s*=.*\[식약처\s*의약품\s*정보\]", fact_md)
    )
    current_registry_table = (
        "### 임상시험 fact" in fact_md
        and (
            (
                "clinicaltrials_v2_search" in fact_md
                and bool(re.search(r"\bNCT\d+\b", fact_md, flags=re.IGNORECASE))
            )
            or "mfds_clinical_trial_kr" in fact_md
        )
    )
    return legacy_registry or current_registry_table


def _is_external_adverse_event(fact_md: str) -> bool:
    return "[FDA 이상반응 보고 정보]" in fact_md and "FAERS 자발보고" in fact_md


def _active_fact_types(body: str, fact_md: str) -> tuple[str, ...]:
    active: list[str] = []
    for fact_type, detector in _FACT_TYPE_DETECTORS.items():
        if detector(fact_md):
            active.append(fact_type)
    if "channel_cross_section" not in active and _answer_has_channel_table(body):
        active.append("channel_cross_section")
    return tuple(active)


def _answer_has_channel_table(markdown: str) -> bool:
    return bool(_CHANNEL_TABLE_HEADER_RE.search(markdown))


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


def _has_forbidden_analysis_claim(body: str, pattern: re.Pattern[str]) -> bool:
    return any(
        pattern.search(sentence)
        for raw_line in body.splitlines()
        if not _is_non_analysis_line(raw_line)
        for sentence in _sentence_parts(raw_line)
    )


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
    return _cleanup_policy_markdown(
        " ".join(
            (
                f"{prefix}{brand} 채널별 매출은 {sales_phrase} 순입니다.",
                f"채널 내 시장점유율은 {share_phrase} 순입니다.",
                f"따라서 매출 볼륨은 {sales_top[0].name}, 상대 점유율 우위는 {share_top[0].name}에서 나타납니다.",
                "이 차이의 원인은 현재 데이터만으로 확인할 수 없으며, 환자 구성·경쟁 제품·처방기관 수·영업 커버리지 데이터를 추가 확인해야 합니다.",
            )
        )
    )


def _rewrite_channel_cross_section(question: str, body: str, fact_md: str) -> str:
    summary = _channel_safe_summary(question, fact_md)
    table = _channel_fact_table(fact_md)
    timing = _timing_block(body)
    if not summary:
        claims = FORBIDDEN_BY_FACT_TYPE.get("channel_cross_section", ())
        revised, _ = _drop_forbidden_claim_sentences(body, claims)
        return revised
    return "\n\n".join(part for part in (summary, table, timing) if part)


def _channel_fact_table(fact_md: str) -> str:
    facts = _channel_facts(fact_md)
    if not facts:
        return ""
    rows = ["| 채널 | 시장점유율 | 매출 |", "| --- | --- | --- |"]
    rows.extend(f"| {item.name} | {item.share} | {item.sales} |" for item in facts)
    return "\n".join(rows)


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
    if facts:
        return tuple(sorted(facts, key=lambda item: item.rank))
    for index, match in enumerate(_CHANNEL_TABLE_ROW_RE.finditer(fact_md), start=1):
        name = match.group("name").strip()
        if name in {"---", "채널"}:
            continue
        facts.append(
            ChannelFact(
                rank=index,
                name=name,
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
    protected_urls: dict[str, str] = {}

    def protect_url(match: re.Match[str]) -> str:
        raw_url = match.group(0)
        url = raw_url.rstrip(".!?。")
        trailing_punctuation = raw_url[len(url) :]
        token = f"__CLAIM_POLICY_URL_{len(protected_urls)}__"
        protected_urls[token] = url
        return f"{token}{trailing_punctuation}"

    protected = _URL_RE.sub(protect_url, line)
    protected = re.sub(r"(?<=\d)\.(?=\d)", decimal_dot, protected)
    parts = tuple(
        _restore_protected_urls(
            match.group(0).replace(decimal_dot, "."),
            protected_urls,
        ).strip()
        for match in _SENTENCE_RE.finditer(protected)
        if match.group(0).strip()
    )
    return parts or ((line.strip(),) if line.strip() else ())


def _restore_protected_urls(text: str, protected_urls: dict[str, str]) -> str:
    restored = text
    for token, url in protected_urls.items():
        restored = restored.replace(token, url)
    return restored


def _is_non_analysis_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if stripped.startswith(("|", ">")):
        return True
    return bool(_PURE_SOURCE_LINE_RE.fullmatch(stripped))


def _split_sources(answer: str) -> tuple[str, str]:
    match = _SOURCE_HEADING_RE.search(answer)
    if not match:
        return answer, ""
    return answer[: match.start()], answer[match.start() :]


def _timing_block(body: str) -> str:
    match = _TIMING_HEADING_RE.search(body)
    if not match:
        return ""
    return body[match.start() :].strip()


_ADVERSE_EVENT_FACT_RE: Final = re.compile(
    r"(?m)^-\s*(?P<subject>.+?)\s*\((?P<date>\d{4}-\d{2}-\d{2})\):\s*"
    r"FAERS\s*자발보고\s*내\s*이상반응\s*=\s*FAERS\s*보고\s*"
    r"(?P<report_id>\d+)\s*·\s*(?P=date)\s*·\s*보고\s*반응:\s*"
    r"(?P<reactions>.+?)\s*\[FDA\s*이상반응\s*보고\s*정보\]\s*$"
)


def _adverse_event_facts(fact_md: str) -> tuple[AdverseEventFact, ...]:
    return tuple(
        AdverseEventFact(
            subject=match.group("subject").strip(),
            report_id=match.group("report_id"),
            report_date=match.group("date"),
            reactions=match.group("reactions").strip(),
        )
        for match in _ADVERSE_EVENT_FACT_RE.finditer(fact_md)
    )


def _rewrite_external_adverse_event(body: str, fact_md: str) -> str:
    facts = _adverse_event_facts(fact_md)
    if not facts:
        return body
    subjects = ", ".join(dict.fromkeys(fact.subject for fact in facts))
    rows = [
        "| 대상 성분 | FAERS 보고 ID | 보고일 | 보고 반응 |",
        "| --- | --- | --- | --- |",
    ]
    rows.extend(
        f"| {fact.subject} | {fact.report_id} | {fact.report_date} | {fact.reactions} |"
        for fact in facts
    )
    summary = (
        f"FDA FAERS에서 {subjects} 관련 자발보고 {len(facts)}건이 조회됐습니다. "
        "아래 숫자는 각 보고 ID이며 건수가 아닙니다. "
        "또한 자발보고는 약물과 반응의 인과관계를 입증하지 않습니다."
    )
    return "\n\n".join((summary, "\n".join(rows), _timing_block(body))).strip()


def _cleanup_policy_markdown(markdown: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", markdown).strip()


def _clinical_registry_safe_summary(_question: str, fact_md: str) -> str:
    global_count = len(set(re.findall(r"\bNCT\d+\b", fact_md, flags=re.IGNORECASE)))
    legacy_domestic_count = len(
        re.findall(r"(?m)^-\s*.+?:\s*국내\s*임상시험\s*=.*\[식약처\s*의약품\s*정보\]\s*$", fact_md)
    )
    table_domestic_count = len(
        re.findall(r"(?m)^\|\s*mfds_clinical_trial_kr\s*\|", fact_md)
    )
    domestic_count = legacy_domestic_count + table_domestic_count
    observed: list[str] = []
    if global_count:
        observed.append(f"ClinicalTrials.gov 등록정보에서 글로벌 임상시험 {global_count}건")
    if domestic_count:
        observed.append(f"식약처 등록정보에서 국내 임상시험 {domestic_count}건")
    if not observed:
        return ""
    return (
        f"{'과 '.join(observed)}이 확인됩니다. "
        "이는 연구 등록과 제목을 보여주는 근거이며, 결과·효과·안전성 확정이나 개발 성공을 뜻하지는 않습니다."
    )


_FACT_TYPE_DETECTORS: Final[dict[str, Callable[[str], bool]]] = {
    "channel_cross_section": _is_channel_cross_section,
    "brand_share_delta": _is_brand_share_delta,
    "news_context": _is_news_context,
    "external_clinical_registry": _is_external_clinical_registry,
    "external_adverse_event": _is_external_adverse_event,
}

_SAFE_REPLACEMENTS: Final[dict[str, Callable[[str, str], str]]] = {
    "channel_cross_section": _channel_safe_summary,
    "external_clinical_registry": _clinical_registry_safe_summary,
}
