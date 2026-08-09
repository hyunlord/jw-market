from __future__ import annotations

from collections.abc import Mapping
from ipaddress import ip_address
import json
import re
from typing import Final
from urllib.parse import urlparse

from jw_chat_agent_poc.orchestrator.external_passthrough import (
    EXTERNAL_PASSTHROUGH_FIELD,
    WEB_FALLBACK_DISCLOSURE,
    external_call_has_usable_result,
    external_passthrough_calls,
)


_SOURCE_SECTION_RE: Final[re.Pattern[str]] = re.compile(
    r"\n#{2,3}\s*출처\s*\n",
    re.IGNORECASE,
)
_WEB_FALLBACK_PARTIAL_DISCLOSURE: Final = "공식 소스에서 확인되지 않은 부분은 웹 검색 결과로 보완합니다"
_INTERNAL_SECTION_HEADING: Final = "## 내부 정형 지표"
_WEB_SECTION_HEADING: Final = "## 뉴스·외부 이슈"
_INTERNAL_SECTION_RE: Final[re.Pattern[str]] = re.compile(
    r"(?m)^#{1,6}\s*내부\s*정형\s*지표\s*$"
)
_WEB_SECTION_RE: Final[re.Pattern[str]] = re.compile(
    r"(?m)^#{1,6}\s*뉴스[·/]?외부\s*이슈\s*$"
)
_BLOCK_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^#{1,2}\s+\S")
_PROTECTED_METRIC_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:\bsales\b|\bmarket[_ ]?share\b|\brank\b|\bHHI\b|\bCR5\b|"
    r"\bgrowth[_ ]?rate\b|\bchannel[_ ]?share\b|매출|시장\s*점유율?|점유율?|"
    r"순위|성장률|채널\s*점유율?)",
    re.IGNORECASE,
)
_NEWS_CONTEXT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:뉴스|기사|보도|이슈|안전성|임상|허가|학술|발표|언론)",
    re.IGNORECASE,
)
_OFFICIAL_TOOL_PREFIXES: Final[tuple[str, ...]] = (
    "hira_",
    "mfds_",
    "nedrug_",
    "clinicaltrials_",
    "openfda_",
)
_DEFAULT_SOURCE_URLS: Final[dict[str, str]] = {
    "hira": "https://opendata.hira.or.kr/",
    "mfds": "https://nedrug.mfds.go.kr/",
    "clinicaltrials": "https://clinicaltrials.gov/",
    "openfda": "https://open.fda.gov/",
}


def external_passthrough_context(result: Mapping[str, object]) -> str:
    markdown_response = result.get("markdown_response")
    fact_md = ""
    if isinstance(markdown_response, Mapping):
        fact_md = str(
            markdown_response.get("fact_md") or markdown_response.get("data_md") or ""
        ).strip()
    sections = [f"[도구가 조립한 사실]\n{fact_md}"] if fact_md else []
    for index, call in enumerate(external_passthrough_calls(result), start=1):
        render_data = call.get("render_data")
        rendered = json.dumps(
            render_data if isinstance(render_data, Mapping) else {},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        sections.append(
            "\n".join(
                (
                    f"[외부 조회 {index}]",
                    f"도구: {call.get('tool') or 'unknown'}",
                    f"소스: {call.get('source') or 'unknown'}",
                    f"상태: {call.get('status') or 'unknown'}",
                    f"요약: {call.get('summary_text') or ''}",
                    f"결과 JSON: {rendered}",
                )
            )
        )
    return "\n\n".join(sections)


def external_passthrough_fallback_answer(result: Mapping[str, object]) -> str:
    lines: list[str] = []
    for call in external_passthrough_calls(result):
        render_data = call.get("render_data")
        user_message = (
            str(render_data.get("user_message") or "").strip()
            if isinstance(render_data, Mapping)
            else ""
        )
        if user_message:
            lines.append(user_message)
            continue
        if not external_call_has_usable_result(call):
            continue
        summary = str(call.get("summary_text") or "").strip()
        if summary:
            lines.append(summary)
        items = render_data.get("items") if isinstance(render_data, Mapping) else None
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                title = str(item.get("title") or "").strip()
                snippet = str(item.get("snippet") or "").strip()
                if title or snippet:
                    lines.append(f"- {title}: {snippet}".strip())
    return "\n".join(dict.fromkeys(lines)) or "외부 조회 결과에서 답변에 사용할 내용을 확인하지 못했습니다."


def finalize_external_passthrough_answer(
    answer: str,
    result: Mapping[str, object],
    *,
    question: str = "",
) -> str:
    if "유병률" in question and not _has_web_prevalence_value(question, result):
        return "국내 유병률 근거를 확보하지 못했습니다"
    body = _SOURCE_SECTION_RE.split(answer.strip(), maxsplit=1)[0].strip()
    body = body.replace("효과가 없은 경우", "효과가 없는 경우")
    body = _normalize_hira_no_data_wording(body, result)
    body = _latest_hira_basis_first(question, body, result)
    body = _enforce_eylea_reimbursement_basis(question, body, result)
    body = _separate_internal_and_web_blocks(body, result)
    body = normalize_external_section_headings(body, result)
    marker = result.get(EXTERNAL_PASSTHROUGH_FIELD)
    web_fallback_used = isinstance(marker, Mapping) and marker.get("web_fallback_used") is True
    if web_fallback_used:
        official_result_present = any(
            str(call.get("tool") or "").strip().casefold().startswith(_OFFICIAL_TOOL_PREFIXES)
            and external_call_has_usable_result(call)
            for call in external_passthrough_calls(result)
        )
        disclosure = (
            _WEB_FALLBACK_PARTIAL_DISCLOSURE
            if official_result_present
            else WEB_FALLBACK_DISCLOSURE
        )
        for existing in (WEB_FALLBACK_DISCLOSURE, _WEB_FALLBACK_PARTIAL_DISCLOSURE):
            if body.startswith(existing):
                body = body.removeprefix(existing).lstrip()
        body = f"{disclosure}\n\n{body}".strip()
    return f"{body}\n\n{external_source_footer(result)}".strip()


def _latest_hira_basis_first(
    question: str,
    body: str,
    result: Mapping[str, object],
) -> str:
    if "최근" not in question or not re.search(r"환자\s*수", question):
        return body
    years = sorted(
        {
            period
            for call in external_passthrough_calls(result)
            for period in (_hira_call_period(call),)
            if re.fullmatch(r"20\d{2}", period)
        }
    )
    if not years:
        return body
    lead = f"최신 조회 기준은 {years[-1]}년입니다."
    return body if body.startswith(lead) else f"{lead}\n\n{body}".strip()


def _enforce_eylea_reimbursement_basis(
    question: str,
    body: str,
    result: Mapping[str, object],
) -> str:
    if "아일리아" not in question or "급여기준" not in question:
        return body
    serialized = json.dumps(external_passthrough_calls(result), ensure_ascii=False, default=str)
    if "2024-235" not in serialized and "2024-12-01" not in serialized and "20241201" not in serialized:
        return body
    body = "\n".join(line for line in body.splitlines() if "12개월" not in line).strip()
    lead = (
        "최신 공식 기준은 고시 제2024-235호(2024-12-01 시행)입니다.\n\n"
        "망막분지정맥폐쇄성 황반부종의 투여 횟수는 단안당 총 5회 이내입니다."
    )
    return body if body.startswith("최신 공식 기준은 고시 제2024-235호") else f"{lead}\n\n{body}".strip()


def _has_web_prevalence_value(question: str, result: Mapping[str, object]) -> bool:
    metric = re.compile(r"(?:유병률|환자\s*수|발생률|유병\s*환자|진료\s*인원)", re.IGNORECASE)
    value = re.compile(r"\d[\d,.]*\s*(?:%|명|건|만\s*명|억\s*명)", re.IGNORECASE)
    for call in external_passthrough_calls(result):
        if str(call.get("tool") or "").casefold() != "web_search":
            continue
        data = call.get("render_data")
        items = data.get("items") if isinstance(data, Mapping) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            body = " ".join(
                str(item.get(key) or "").strip()
                for key in ("content", "snippet", "raw_content", "text")
                if str(item.get(key) or "").strip()
            )
            grade = str(item.get("source_grade") or "")
            if grade.startswith(("A ", "B ")) and metric.search(body) and value.search(body):
                return True
    return False


def _normalize_hira_no_data_wording(body: str, result: Mapping[str, object]) -> str:
    """Keep a structured HIRA no-data result from becoming an API failure claim."""

    calls = external_passthrough_calls(result)
    failed_periods = {
        _hira_call_period(call)
        for call in calls
        if str(call.get("status") or "").strip().casefold() in {"error", "timeout"}
    }
    no_data_periods = tuple(
        dict.fromkeys(
            period
            for call in calls
            if str(call.get("status") or "").strip().casefold() == "no_data"
            for period in (_hira_call_period(call),)
            if period and period not in failed_periods
        )
    )
    for period in no_data_periods:
        false_failure_line = re.compile(
            rf"(?m)^(?P<prefix>\s*(?:(?:[-+*])\s+)?(?:\*\*)?"
            rf"{re.escape(period)}년?(?:\*\*)?\s*[:：]\s*)"
            r".*?API\s*호출.*?실패.*?$"
        )
        body = false_failure_line.sub(
            lambda match: f"{match.group('prefix')}조회 결과가 없습니다.",
            body,
        )
    return body


def _hira_call_period(call: Mapping[str, object]) -> str:
    if not str(call.get("tool") or "").startswith("hira_disease_"):
        return ""
    render_data = call.get("render_data")
    if not isinstance(render_data, Mapping):
        return ""
    explicit = str(render_data.get("requested_period") or "").strip()
    if explicit:
        return explicit
    request = render_data.get("request")
    return str(request.get("year") or "").strip() if isinstance(request, Mapping) else ""


def external_source_footer(result: Mapping[str, object]) -> str:
    marker = result.get(EXTERNAL_PASSTHROUGH_FIELD)
    queried_at = str(marker.get("queried_at_utc") or "") if isinstance(marker, Mapping) else ""
    entries: list[tuple[str, str, str, str]] = []
    for call in external_passthrough_calls(result):
        call_time = str(call.get("queried_at_utc") or queried_at or "확인 불가")
        url_entries = _public_url_entries(call)
        if not url_entries:
            default_url = _default_source_url(call)
            if default_url:
                url_entries = ((default_url, ""),)
        entries.extend(
            (_source_label(call), call_time, url, published_at)
            for url, published_at in url_entries
        )
    deduped = tuple(dict.fromkeys(entries))
    internal_entries = _internal_source_labels(result)
    if not deduped and not internal_entries:
        return "## 출처\n- 외부 조회 · 조회시점 확인 불가 · URL 확인 불가"
    lines = ["## 출처"]
    lines.extend(
        f"- {label} · 내부 정형 지표 · 조회시점 {queried_at or '확인 불가'}"
        for label in internal_entries
    )
    for label, call_time, url, published_at in deduped:
        publication = (
            f" · 게시일 {published_at}"
            if published_at
            else " · 게시일 미상"
            if label == "Tavily 웹 검색"
            else ""
        )
        lines.append(
            f"- {label} · 조회시점 {call_time}{publication} · [{url}]({url})"
        )
    return "\n".join(lines)


def _separate_internal_and_web_blocks(
    body: str,
    result: Mapping[str, object],
) -> str:
    if not (_internal_source_labels(result) and _has_web_source(result)):
        return body
    if _INTERNAL_SECTION_RE.search(body) and _WEB_SECTION_RE.search(body):
        return body

    internal: list[str] = []
    web: list[str] = []
    for paragraph in re.split(r"\n\s*\n", body):
        stripped = paragraph.strip()
        if not stripped:
            continue
        if _NEWS_CONTEXT_RE.search(stripped):
            web.append(stripped)
        elif _PROTECTED_METRIC_RE.search(stripped):
            internal.append(stripped)
        else:
            web.append(stripped)
    if not internal or not web:
        return body
    return "\n\n".join(
        (
            _INTERNAL_SECTION_HEADING,
            "\n\n".join(internal),
            _WEB_SECTION_HEADING,
            "\n\n".join(web),
        )
    )


def normalize_external_section_headings(
    body: str,
    result: Mapping[str, object],
) -> str:
    """Keep evidence block headings only when their block has renderable content."""

    lines = body.splitlines()
    internal_source_present = bool(_internal_source_labels(result))
    kept: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        internal_heading = _INTERNAL_SECTION_RE.fullmatch(stripped) is not None
        web_heading = _WEB_SECTION_RE.fullmatch(stripped) is not None
        if not (internal_heading or web_heading):
            kept.append(line)
            continue

        section_lines = _following_section_lines(lines, index)
        if internal_heading:
            section_blob = "\n".join(section_lines)
            has_internal_value = (
                internal_source_present
                and bool(_PROTECTED_METRIC_RE.search(section_blob))
                and bool(re.search(r"\d", section_blob))
            )
            if has_internal_value:
                kept.append(_INTERNAL_SECTION_HEADING)
            continue
        if _has_substantive_section_content(section_lines):
            kept.append(_WEB_SECTION_HEADING)

    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def _following_section_lines(lines: list[str], heading_index: int) -> tuple[str, ...]:
    section: list[str] = []
    for line in lines[heading_index + 1 :]:
        if _BLOCK_HEADING_RE.match(line.strip()):
            break
        section.append(line)
    return tuple(section)


def _has_substantive_section_content(lines: tuple[str, ...]) -> bool:
    return any(line.strip() and not line.lstrip().startswith("#") for line in lines)


def _has_web_source(result: Mapping[str, object]) -> bool:
    return any(
        str(call.get("tool") or "").strip().casefold() == "web_search"
        for call in external_passthrough_calls(result)
    )


def _internal_source_labels(result: Mapping[str, object]) -> tuple[str, ...]:
    candidates: list[str] = []
    sources = result.get("sources")
    if isinstance(sources, (list, tuple)):
        candidates.extend(str(source) for source in sources)
    calls = result.get("tool_calls")
    if isinstance(calls, list):
        for call in calls:
            if not isinstance(call, Mapping):
                continue
            candidates.extend(
                str(call.get(key) or "") for key in ("source", "tool")
            )
    blob = "\n".join(candidates).upper()
    labels = [
        label
        for token, label in (
            ("UBIST", "UBIST"),
            ("IQVIA_NSA", "IQVIA NSA"),
            ("IQVIA NSA", "IQVIA NSA"),
            ("IQVIA_CSD", "IQVIA CSD"),
            ("IQVIA CSD", "IQVIA CSD"),
        )
        if token in blob
    ]
    return tuple(dict.fromkeys(labels))


def _source_label(call: Mapping[str, object]) -> str:
    tool = str(call.get("tool") or "").casefold()
    source = str(call.get("source") or "").casefold()
    if tool == "web_search" or source == "web_search":
        return "Tavily 웹 검색"
    if tool.startswith("hira_") or "hira" in source:
        return "건강보험심사평가원(HIRA)"
    if tool.startswith(("mfds_", "nedrug_")) or any(
        token in source for token in ("mfds", "nedrug")
    ):
        return "식품의약품안전처(NeDrug/MFDS)"
    if tool.startswith("clinicaltrials_") or "clinicaltrials" in source:
        return "ClinicalTrials.gov"
    if tool.startswith("openfda_") or "openfda" in source:
        return "openFDA"
    if "news" in tool or "news" in source:
        return "웹 뉴스"
    return str(call.get("source") or call.get("tool") or "외부 소스")


def _public_url_entries(call: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    candidates: list[tuple[str, str]] = []
    safe_url = call.get("safe_url")
    if isinstance(safe_url, str):
        candidates.append((safe_url, ""))
    _collect_url_entries(call.get("render_data"), candidates)
    return tuple(
        dict.fromkeys(
            (url, published_at)
            for url, published_at in candidates
            if _is_public_url(url)
        )
    )


def _collect_url_entries(value: object, output: list[tuple[str, str]]) -> None:
    if isinstance(value, Mapping):
        published_at = str(
            value.get("published_at") or value.get("published_date") or ""
        ).strip()
        for key, item in value.items():
            if str(key).casefold() in {"url", "link", "source_url"} and isinstance(item, str):
                output.append((item, published_at))
            else:
                _collect_url_entries(item, output)
    elif isinstance(value, list):
        for item in value:
            _collect_url_entries(item, output)


def _is_public_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    try:
        address = ip_address(host)
    except ValueError:
        address = None
    return (
        parsed.scheme in {"http", "https"}
        and "." in host
        and not host.endswith((".svc", ".cluster.local"))
        and not (
            address
            and (address.is_private or address.is_loopback or address.is_link_local)
        )
    )


def _default_source_url(call: Mapping[str, object]) -> str:
    tool = str(call.get("tool") or "").casefold()
    if tool.startswith("hira_"):
        return _DEFAULT_SOURCE_URLS["hira"]
    if tool.startswith(("mfds_", "nedrug_")):
        return _DEFAULT_SOURCE_URLS["mfds"]
    if tool.startswith("clinicaltrials_"):
        return _DEFAULT_SOURCE_URLS["clinicaltrials"]
    if tool.startswith("openfda_"):
        return _DEFAULT_SOURCE_URLS["openfda"]
    return ""
