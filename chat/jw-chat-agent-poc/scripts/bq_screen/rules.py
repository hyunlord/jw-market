from __future__ import annotations

import re
from collections import Counter

from scripts.bq_screen.models import BqScreenInput, Finding, ScreenResult


UNAVAILABLE_MARKERS: tuple[str, ...] = (
    "미보유",
    "조회 불가",
    "확인 불가",
    "수행할 수 없습니다",
    "데이터가 없습니다",
    "데이터 없음",
)
FIVE_STEP_MARKERS: tuple[str, ...] = (
    "미보유 데이터",
    "현재 가능한 proxy",
    "해석 가능한 상한선",
    "확인 필요 데이터",
    "확보 시",
)
SOURCE_REQUESTS: dict[str, tuple[str, ...]] = {
    "cortellis": ("cortellis", "Cortellis"),
    "datamonitor": ("datamonitor", "Datamonitor"),
    "kol": ("kol", "KOL", "전문가"),
    "nccn": ("nccn", "NCCN"),
}
POSITIONING_TERMS: tuple[str, ...] = (
    "포지셔닝",
    "차별",
    "축",
    "강점",
    "약점",
    "가격",
    "효능",
    "접근성",
    "세그먼트",
)
RELEVANCE_GRADE_TERMS: tuple[str, ...] = (
    "관련성",
    "직접(브랜드)",
    "패밀리",
    "배경",
    "잡음",
)
DUAL_CLASS_TERMS: tuple[str, ...] = (
    "Class 1",
    "Class 2",
    "dual",
    "split",
    "이중",
    "분리",
    "클래스별",
)
URL_RE = re.compile(r"https?://\S+")
PCT_RE = re.compile(r"(?<![\d.])(-?\d+(?:\.\d+)?)%")
VISIBLE_FAILED_ZERO_RE = re.compile(
    r"(?:0\.00\s*(?:억\s*원|억원|%|%p)|매출\s*0\.00|시장점유율\s*0\.00|MS\s*0\.00)",
)


def screen_answer(item: BqScreenInput) -> ScreenResult:
    findings: list[Finding] = []
    _add(findings, _format_findings(item))
    _add(findings, _semantic_findings(item))
    flags = tuple(f.flag for f in findings if f.severity == "flag")
    confirm_needed = tuple(f.flag for f in findings if f.severity == "confirm_needed")
    return ScreenResult(
        case_id=item.case.id,
        flags=_dedupe(flags),
        confirm_needed=_dedupe(confirm_needed),
        findings=tuple(findings),
    )


def _format_findings(item: BqScreenInput) -> tuple[Finding, ...]:
    text = item.text.strip()
    findings: list[Finding] = []
    if not text or len(text) < 20:
        findings.append(Finding("empty", "flag", "answer body is empty or nearly empty"))
    if item.elapsed_s is not None and item.elapsed_s > 90:
        findings.append(Finding("timeout", "flag", f"elapsed_s={item.elapsed_s:.3f} exceeds 90s"))
    if item.status is not None and item.status != 200:
        findings.append(Finding("error", "flag", f"http_status={item.status}"))
    if item.error:
        findings.append(Finding("error", "flag", f"error={item.error}"))
    if _looks_like_unavailable(text) and not _has_five_step(text):
        findings.append(Finding("naked_unavailable", "flag", "unavailable answer lacks common 5-step structure"))
    if "분류해 연결합니다" in text or "뉴스 fact를 분류" in text:
        findings.append(Finding("template_only", "flag", "template/planning phrase surfaced instead of concrete analysis"))
    duplicate = _duplicated_substantive_line(text)
    if duplicate:
        findings.append(Finding("structure", "confirm_needed", f"duplicated substantive line: {duplicate[:80]}"))
    if "Statin/EZE" in text and "제형" in text and "성분 조합 기준" not in text:
        findings.append(Finding("label_or_denominator", "confirm_needed", "combination-segment dosage values lack footnote"))
    if _uses_web_search(item.tools) and _url_before_unverified_section(text):
        findings.append(Finding("web_contamination", "flag", "web URL appears outside the unverified web section"))
    return tuple(findings)


def _semantic_findings(item: BqScreenInput) -> tuple[Finding, ...]:
    text = item.text
    case = item.case
    findings: list[Finding] = []
    if _has_metric_conflict(case.brand, text):
        findings.append(
            Finding(
                "same_entity_period_metric_conflict",
                "flag",
                "same answer surfaces failed 0.00 metric alongside non-zero time-series metric for the requested entity",
            ),
        )
    if _surfaces_query_failed_value(text):
        findings.append(
            Finding(
                "query_failed_value_surface",
                "flag",
                "query-failed or missing metric appears as a visible 0.00 value",
            ),
        )
    mismatch = _source_mismatch(case.question, text)
    if mismatch:
        findings.append(Finding("requested_vs_actual_source_mismatch", "flag", mismatch))
    axis_evidence = _intent_axis_missing(case.type, case.question, text)
    if axis_evidence:
        findings.append(Finding("intent_required_axis_missing", "confirm_needed", axis_evidence))
    if _market_structure_split_missing(case.cohort, text):
        findings.append(
            Finding(
                "market_structure_split_missing",
                "confirm_needed",
                "dual-class market context appears without Class 1/Class 2 split or split denominator explanation",
            ),
        )
    if _news_relevance_grade_missing(case.type, case.question, text):
        findings.append(
            Finding(
                "news_relevance_grade_missing",
                "confirm_needed",
                "news-driven E/I answer lacks direct/family/market/background/noise relevance grade",
            ),
        )
    return tuple(findings)


def _looks_like_unavailable(text: str) -> bool:
    return any(marker in text for marker in UNAVAILABLE_MARKERS)


def _has_five_step(text: str) -> bool:
    return all(marker in text for marker in FIVE_STEP_MARKERS)


def _duplicated_substantive_line(text: str) -> str:
    normalized = [_normalize_line(line) for line in text.splitlines()]
    counts = Counter(line for line in normalized if len(line) >= 45 and not line.startswith("|"))
    for line, count in counts.items():
        if count > 1:
            return line
    return ""


def _normalize_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip(" -*\t")).strip()


def _uses_web_search(tools: tuple[str, ...]) -> bool:
    return "web_search" in tools


def _url_before_unverified_section(text: str) -> bool:
    before = text.split("### 웹 검색 결과(미검증)", 1)[0]
    return bool(URL_RE.search(before))


def _has_metric_conflict(brand: str, text: str) -> bool:
    if brand and brand not in text:
        return False
    percentages = [float(match.group(1)) for match in PCT_RE.finditer(text)]
    has_zero_pct = any(abs(value) < 0.0001 for value in percentages)
    has_nonzero_pct = any(value >= 1.0 for value in percentages)
    conflict_context = any(token in text for token in ("브랜드 핵심 지표", "기준 시장점유율", "query_failed", "조회 실패"))
    return has_zero_pct and has_nonzero_pct and conflict_context


def _surfaces_query_failed_value(text: str) -> bool:
    if not VISIBLE_FAILED_ZERO_RE.search(text):
        return False
    failure_context = any(token in text for token in ("query_failed", "조회 실패", "시장 매핑 불완전", "브랜드 핵심 지표"))
    return failure_context


def _source_mismatch(question: str, text: str) -> str:
    question_lower = question.lower()
    source_block_raw = _source_block(text)
    source_block = source_block_raw.lower()
    for source_key, tokens in SOURCE_REQUESTS.items():
        requested = any(token.lower() in question_lower for token in tokens)
        if not requested:
            continue
        if _declares_requested_source_unavailable(source_key, text) and not _masquerades_requested_source(source_key, text):
            return ""
        if not source_block_raw:
            return ""
        if source_key not in source_block:
            actual = source_block[:160].replace("\n", " ") or "source block missing"
            return f"requested {source_key} but source block does not cite it: {actual}"
    return ""


def _declares_requested_source_unavailable(source_key: str, text: str) -> bool:
    label_tokens = SOURCE_REQUESTS.get(source_key, ())
    if not label_tokens:
        return False
    first_block = text[:500].lower()
    return any(token.lower() in first_block for token in label_tokens) and "미보유" in first_block


def _masquerades_requested_source(source_key: str, text: str) -> bool:
    patterns = {
        "cortellis": r"Cortellis\s*기준",
        "datamonitor": r"Datamonitor\s*기준",
        "kol": r"(?:KOL|전문가|자문)\s*기준",
        "nccn": r"(?:NCCN|가이드라인|치료\s*지침)\s*기준",
    }
    pattern = patterns.get(source_key)
    return bool(pattern and re.search(pattern, text, re.IGNORECASE))


def _source_block(text: str) -> str:
    marker = "## 출처"
    if marker not in text:
        return ""
    return text.split(marker, 1)[1]


def _intent_axis_missing(case_type: str, question: str, text: str) -> str:
    asks_positioning = "B2" in case_type or "포지셔닝" in question or "차별" in question
    if asks_positioning:
        normalized = _without_question_echo(question, text)
        if not any(term in normalized for term in POSITIONING_TERMS):
            return "positioning/differentiation intent lacks an explicit positioning axis or differentiator"
    asks_segment = "C2" in case_type or "세그먼트" in question
    if asks_segment and not _looks_like_unavailable(text):
        required = ("Class", "Molecule", "용량", "제형")
        if not any(axis in text for axis in required):
            return "segment intent lacks requested segment axes"
    return ""


def _without_question_echo(question: str, text: str) -> str:
    reduced = text
    for token in re.split(r"\s+", question):
        if len(token) >= 3:
            reduced = reduced.replace(token, "")
    return reduced


def _market_structure_split_missing(cohort: str, text: str) -> bool:
    dual_context = "dual" in cohort.lower() or "ml_011" in text
    if not dual_context:
        return False
    return not any(term in text for term in DUAL_CLASS_TERMS)


def _news_relevance_grade_missing(case_type: str, question: str, text: str) -> bool:
    asks_news_ei = "E2" in case_type or "External/Internal" in question or "뉴스" in question
    if not asks_news_ei:
        return False
    has_news = bool(URL_RE.search(text)) or "뉴스" in text or "External" in text
    has_direction = "영향방향" in text or "기회" in text or "위협" in text or "불확실" in text
    if not (has_news and has_direction):
        return False
    grade_header = "관련성 등급" in text or "| 등급 |" in text or "| 관련성 |" in text
    grade_value = any(term in text for term in RELEVANCE_GRADE_TERMS)
    return not (grade_header or grade_value)


def _add(target: list[Finding], items: tuple[Finding, ...]) -> None:
    target.extend(items)


def _dedupe(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
