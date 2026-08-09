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
        if not external_call_has_usable_result(call):
            continue
        summary = str(call.get("summary_text") or "").strip()
        if summary:
            lines.append(summary)
        render_data = call.get("render_data")
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


def finalize_external_passthrough_answer(answer: str, result: Mapping[str, object]) -> str:
    body = _SOURCE_SECTION_RE.split(answer.strip(), maxsplit=1)[0].strip()
    body = _normalize_hira_no_data_wording(body, result)
    marker = result.get(EXTERNAL_PASSTHROUGH_FIELD)
    web_fallback_used = isinstance(marker, Mapping) and marker.get("web_fallback_used") is True
    if web_fallback_used and WEB_FALLBACK_DISCLOSURE not in body:
        body = f"{WEB_FALLBACK_DISCLOSURE}\n\n{body}".strip()
    return f"{body}\n\n{external_source_footer(result)}".strip()


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
            rf"(?m)^(?P<prefix>\s*(?:[-*]\s*)?{re.escape(period)}년?\s*[:：]\s*)"
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
    entries: list[tuple[str, str, str]] = []
    for call in external_passthrough_calls(result):
        call_time = str(call.get("queried_at_utc") or queried_at or "확인 불가")
        urls = _public_urls(call)
        if not urls:
            default_url = _default_source_url(call)
            if default_url:
                urls = (default_url,)
        entries.extend((_source_label(call), call_time, url) for url in urls)
    deduped = tuple(dict.fromkeys(entries))
    if not deduped:
        return "## 출처\n- 외부 조회 · 조회시점 확인 불가 · URL 확인 불가"
    return "\n".join(
        ["## 출처"]
        + [
            f"- {label} · 조회시점 {call_time} · [{url}]({url})"
            for label, call_time, url in deduped
        ]
    )


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


def _public_urls(call: Mapping[str, object]) -> tuple[str, ...]:
    candidates: list[str] = []
    safe_url = call.get("safe_url")
    if isinstance(safe_url, str):
        candidates.append(safe_url)
    _collect_urls(call.get("render_data"), candidates)
    return tuple(dict.fromkeys(url for url in candidates if _is_public_url(url)))


def _collect_urls(value: object, output: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in {"url", "link", "source_url"} and isinstance(item, str):
                output.append(item)
            else:
                _collect_urls(item, output)
    elif isinstance(value, list):
        for item in value:
            _collect_urls(item, output)


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
