from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jw_chat_agent_poc.orchestrator.markdown_formatting import table
from jw_chat_agent_poc.orchestrator.source_grading import SourceGrade, grade_web_url
from jw_chat_agent_poc.service.web_presentation_policy import web_presentation_policy
from jw_chat_agent_poc.service.web_relevance import filter_web_results


TODAY: date = date(2026, 7, 3)
STALE_DAYS = 365
MAX_SNIPPET_CHARS = 220


@dataclass(frozen=True, slots=True)
class WebSearchItem:
    title: str
    url: str
    snippet: str
    source_grade: SourceGrade
    event_date: str
    relevance: str
    direction: str
    source_count: int
    internal_check: bool
    institution_name: str
    population_text: str
    survey_year: str


def web_search_mi_section(raw_items: Sequence[Mapping[str, object]]) -> str:
    items = sorted(_dedupe_items(raw_items), key=_sort_key)
    current = tuple(item for item in items if not _is_stale(item))
    stale = tuple(item for item in items if _is_stale(item))
    visible_current = tuple(item for item in current if item.relevance != "잡음")
    visible_stale = tuple(item for item in stale if item.relevance != "잡음")
    if not visible_current and not visible_stale:
        return ""
    parts = [
        "### 웹 검색 결과(출처 등급)",
        (
            "A 공식은 공식 원문, B 기관·학술은 기관명·모집단·조사연도를 함께 표시해 사용할 수 있습니다. "
            "C 기타·개인은 정량값의 단독 근거로 사용하지 않습니다."
        ),
    ]
    summary_rows = _summary_rows(visible_current)
    if summary_rows:
        parts.append(
            table(
                "#### 주요 MI 요약",
                ("출처 등급", "관련도", "방향", "사건일", "요약"),
                summary_rows,
            )
        )
    if visible_current:
        parts.append(
            table(
                "#### 최신·관련 결과",
                ("사건일", "출처 등급", "관련도", "제목", "URL", "스니펫", "비고"),
                _detail_rows(visible_current),
            )
        )
    if visible_stale:
        parts.append(
            table(
                "#### 과거 자료",
                ("사건일", "출처 등급", "관련도", "제목", "URL", "스니펫", "비고"),
                _detail_rows(visible_stale),
            )
        )
    return "\n\n".join(parts)


def web_search_mi_section_from_calls(
    tool_calls: Sequence[Mapping[str, object]],
    *,
    question: str | None = None,
) -> str:
    rows: list[Mapping[str, object]] = []
    for call in tool_calls:
        if str(call.get("tool") or "") != "web_search" and str(call.get("source") or "") != "web_search":
            continue
        data = call.get("render_data")
        if not isinstance(data, Mapping):
            continue
        rows.extend(_web_search_items(data))
    if not rows:
        return ""
    disclosure = ""
    if question is not None:
        decision = web_presentation_policy(question, tool_calls)
        if not decision.show_all_results and not decision.accepted_urls:
            return ""
        if decision.accepted_urls:
            accepted_urls = set(decision.accepted_urls)
            rows = [row for row in rows if str(row.get("url") or "").strip() in accepted_urls]
        disclosure = decision.disclosure
        relevance = filter_web_results(question, rows)
        rows = [item for _, item in relevance.accepted]
        if relevance.exclusions:
            exclusion_notice = (
                f"질의 대상과 일치하지 않는 웹 검색 결과 {len(relevance.exclusions)}건을 "
                "제외했습니다(reason_code=web_subject_not_matched)."
            )
            disclosure = " ".join(part for part in (disclosure, exclusion_notice) if part)
        if not rows and relevance.exclusions:
            return (
                "확인 제한:\n- 질의 대상과 관련된 웹 이슈를 찾지 못했습니다. "
                f"관련성 낮은 검색 결과 {len(relevance.exclusions)}건을 제외했습니다."
            )
    section = web_search_mi_section(rows[:5])
    if not section or not disclosure:
        return section
    heading, remainder = section.split("\n\n", maxsplit=1)
    return "\n\n".join((heading, disclosure, remainder))


def _web_search_items(data: Mapping[str, object]) -> list[Mapping[str, object]]:
    direct = data.get("items")
    if isinstance(direct, list):
        return [item for item in direct if isinstance(item, Mapping)]
    calls = data.get("calls")
    if not isinstance(calls, list):
        return []
    rows: list[Mapping[str, object]] = []
    for call in calls:
        render_data = call.get("render_data") if isinstance(call, Mapping) else None
        if not isinstance(render_data, Mapping):
            continue
        nested = render_data.get("items")
        if isinstance(nested, list):
            rows.extend(item for item in nested if isinstance(item, Mapping))
    return rows


def _dedupe_items(raw_items: Sequence[Mapping[str, object]]) -> tuple[WebSearchItem, ...]:
    by_key: dict[str, WebSearchItem] = {}
    for raw in raw_items:
        item = _parse_item(raw)
        if item.relevance == "잡음":
            continue
        key = _dedupe_key(item)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = item
            continue
        by_key[key] = _merge_item(existing, item)
    return tuple(by_key.values())


def _parse_item(raw: Mapping[str, object]) -> WebSearchItem:
    title = _text(raw, "title")
    url = _normalized_url(_text(raw, "url"))
    raw_snippet = _text(raw, "snippet") or _text(raw, "content")
    basis = f"{title} {raw_snippet}"
    snippet = _compact_snippet(raw_snippet)
    return WebSearchItem(
        title=title or "-",
        url=url or "-",
        snippet=snippet or "-",
        source_grade=grade_web_url(url),
        event_date=_event_date(raw, basis),
        relevance=_relevance(basis),
        direction=_direction(basis),
        source_count=1,
        internal_check=_has_internal_metric_claim(basis),
        institution_name=_text(raw, "institution_name") or _institution_name(url),
        population_text=_text(raw, "population_text") or "확인 불가",
        survey_year=_text(raw, "survey_year") or "확인 불가",
    )


def _merge_item(left: WebSearchItem, right: WebSearchItem) -> WebSearchItem:
    return WebSearchItem(
        title=left.title if len(left.title) >= len(right.title) else right.title,
        url=left.url if left.url != "-" else right.url,
        snippet=left.snippet if len(left.snippet) >= len(right.snippet) else right.snippet,
        source_grade=left.source_grade,
        event_date=_latest_event_date(left.event_date, right.event_date),
        relevance=_stronger_relevance(left.relevance, right.relevance),
        direction=left.direction if left.direction != "중립" else right.direction,
        source_count=left.source_count + right.source_count,
        internal_check=left.internal_check or right.internal_check,
        institution_name=_prefer_known(left.institution_name, right.institution_name),
        population_text=_prefer_known(left.population_text, right.population_text),
        survey_year=_prefer_known(left.survey_year, right.survey_year),
    )


def _text(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    return value.strip() if isinstance(value, str) else ""


def _compact_snippet(snippet: str) -> str:
    normalized = re.sub(r"\s+", " ", snippet.replace("|", " ")).strip()
    if len(normalized) <= MAX_SNIPPET_CHARS:
        return normalized
    boundary = normalized.rfind(".", 0, MAX_SNIPPET_CHARS)
    if boundary >= 80:
        return normalized[: boundary + 1]
    return f"{normalized[:MAX_SNIPPET_CHARS].rstrip()}..."


def _normalized_url(url: str) -> str:
    if not url:
        return ""
    split = urlsplit(url)
    kept_query = tuple((key, value) for key, value in parse_qsl(split.query, keep_blank_values=True) if not key.lower().startswith("utm_"))
    return urlunsplit((split.scheme, split.netloc.lower(), split.path.rstrip("/"), urlencode(kept_query), ""))


def _event_date(raw: Mapping[str, object], basis: str) -> str:
    stripped_basis = _strip_revision_dates(basis)
    contextual = _contextual_event_date(stripped_basis)
    if contextual:
        return contextual
    match = re.search(r"\b(20\d{2})[.-](\d{1,2})[.-](\d{1,2})\b", stripped_basis)
    if match:
        year, month, day = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    for key in ("published_date", "publishedAt", "date"):
        value = _text(raw, key)
        match = re.search(r"\b(20\d{2})[.-](\d{1,2})[.-](\d{1,2})\b", value)
        if match:
            year, month, day = match.groups()
            return f"{year}-{int(month):02d}-{int(day):02d}"
    return "날짜 미상"


def _contextual_event_date(basis: str) -> str:
    event_terms = ("승인", "허가", "출시", "발매", "공개", "발표", "계약", "종료", "만료")
    date_pattern = r"(20\d{2})[.-](\d{1,2})[.-](\d{1,2})"
    for term in event_terms:
        pattern = rf"{re.escape(term)}[^\n\r]{{0,24}}?{date_pattern}"
        match = re.search(pattern, basis)
        if not match:
            continue
        year, month, day = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    for term in event_terms:
        pattern = rf"{date_pattern}[^\n\r]{{0,24}}?{re.escape(term)}"
        match = re.search(pattern, basis)
        if not match:
            continue
        year, month, day = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    return ""


def _strip_revision_dates(basis: str) -> str:
    revision_terms = ("최종편집", "최종 수정", "최종수정", "수정", "입력", "업데이트")
    date_pattern = r"20\d{2}[.-]\d{1,2}[.-]\d{1,2}"
    stripped = basis
    for term in revision_terms:
        stripped = re.sub(rf"{re.escape(term)}[^\n\r]{{0,12}}?{date_pattern}", "", stripped)
        stripped = re.sub(rf"{date_pattern}[^\n\r]{{0,12}}?{re.escape(term)}", "", stripped)
    return stripped


def _relevance(basis: str) -> str:
    lowered = basis.lower()
    if any(token in lowered for token in ("rag", "검색 시스템", "llm", "vector db")):
        return "잡음"
    if any(token in basis for token in ("리바로젯", "리바로하이")):
        return "패밀리"
    if "리바로" in basis or "livalo" in lowered:
        return "직접"
    if any(token in basis for token in ("피타바스타틴", "이상지질혈증", "스타틴", "복합제", "경쟁")):
        return "시장"
    return "배경"


def _direction(basis: str) -> str:
    if any(token in basis for token in ("1위", "달성", "확대", "성공", "승인")):
        return "기회"
    if any(token in basis for token in ("경쟁 심화", "지연", "병목", "약가인하", "위협")):
        return "위협"
    return "중립"


def _has_internal_metric_claim(basis: str) -> bool:
    return any(token in basis for token in ("매출", "시장", "점유율", "순위", "MS"))


def _dedupe_key(item: WebSearchItem) -> str:
    grade_prefix = item.source_grade.value
    story_key = _story_key(item)
    if story_key:
        return f"{grade_prefix}:{story_key}"
    if item.url != "-":
        return f"{grade_prefix}:{item.url}"
    title_key = re.sub(r"[^0-9A-Za-z가-힣]+", "", item.title).lower()
    return f"{grade_prefix}:{title_key}"


def _story_key(item: WebSearchItem) -> str:
    basis = f"{item.title} {item.snippet}"
    tokens = [token for token in ("리바로젯", "리바로", "피타바스타틴", "이상지질혈증", "복합제", "매출", "1위", "FDA", "승인") if token in basis]
    if len(tokens) < 4:
        return ""
    return f"{item.event_date}:{':'.join(tokens[:6])}"


def _latest_event_date(left: str, right: str) -> str:
    if left == "날짜 미상":
        return right
    if right == "날짜 미상":
        return left
    return max(left, right)


def _stronger_relevance(left: str, right: str) -> str:
    order = {"직접": 0, "패밀리": 1, "시장": 2, "배경": 3, "잡음": 4}
    return left if order[left] <= order[right] else right


def _is_stale(item: WebSearchItem) -> bool:
    if item.event_date == "날짜 미상":
        return False
    year, month, day = (int(part) for part in item.event_date.split("-"))
    return (TODAY - date(year, month, day)).days > STALE_DAYS


def _sort_key(item: WebSearchItem) -> tuple[int, str]:
    date_key = item.event_date if item.event_date != "날짜 미상" else "0000-00-00"
    return (0 if item.event_date != "날짜 미상" else 1, date_key)


def _summary_rows(items: Sequence[WebSearchItem]) -> tuple[tuple[str, str, str, str, str], ...]:
    rows: list[tuple[str, str, str, str, str]] = []
    for item in reversed(items):
        if item.relevance not in {"직접", "패밀리", "시장"}:
            continue
        tag = " → 내부 지표 확인 가능" if item.internal_check else ""
        metadata = _institution_metadata(item)
        rows.append(
            (
                _grade_label(item.source_grade),
                item.relevance,
                item.direction,
                item.event_date,
                f"{item.snippet}{tag}{metadata}",
            )
        )
    return tuple(rows[:5])


def _detail_rows(items: Sequence[WebSearchItem]) -> tuple[tuple[str, str, str, str, str, str, str], ...]:
    rows: list[tuple[str, str, str, str, str, str, str]] = []
    for item in reversed(items):
        note = []
        if item.source_count > 1:
            note.append(f"매체 병합: {item.source_count}건")
        if item.internal_check:
            note.append("→ 내부 지표 확인 가능")
        metadata = _institution_metadata(item).strip()
        if metadata:
            note.append(metadata)
        rows.append(
            (
                item.event_date,
                _grade_label(item.source_grade),
                item.relevance,
                item.title,
                item.url,
                item.snippet,
                ", ".join(note) or "-",
            )
        )
    return tuple(rows[:8])


def _grade_label(grade: SourceGrade) -> str:
    return {
        SourceGrade.AUTHORITATIVE: "A 공식",
        SourceGrade.SUPPLEMENTARY: "B 기관·학술",
        SourceGrade.UNVERIFIED: "C 기타·개인",
    }[grade]


def _institution_name(url: str) -> str:
    hostname = (urlsplit(url).hostname or "").casefold()
    labels = {
        "hira.or.kr": "건강보험심사평가원",
        "mfds.go.kr": "식품의약품안전처",
        "clinicaltrials.gov": "ClinicalTrials.gov",
        "snuh.org": "서울대학교병원",
        "snubh.org": "분당서울대학교병원",
        "amc.seoul.kr": "서울아산병원",
        "stcarollo.or.kr": "성가롤로병원",
    }
    for domain, label in labels.items():
        if hostname == domain or hostname.endswith(f".{domain}"):
            return label
    return hostname or "확인 불가"


def _prefer_known(left: str, right: str) -> str:
    return right if left == "확인 불가" and right != "확인 불가" else left


def _institution_metadata(item: WebSearchItem) -> str:
    if item.source_grade is not SourceGrade.SUPPLEMENTARY:
        return ""
    return (
        f" · 기관 {item.institution_name} · 모집단 {item.population_text}"
        f" · 조사연도 {item.survey_year}"
    )
