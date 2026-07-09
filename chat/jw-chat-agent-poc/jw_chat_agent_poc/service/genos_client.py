from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import requests

from jw_chat_agent_poc.genos_config import resolve_final_genos_base_url, resolve_final_genos_token
from jw_chat_agent_poc.common.token_usage import usage_call_from_payload
from jw_chat_agent_poc.orchestrator.markdown_formatting import CODE_RE, NUMBER_RE
from jw_chat_agent_poc.orchestrator.markdown_formatting import source_label, source_labels, table
from jw_chat_agent_poc.orchestrator.claim_policy import apply_claim_policy
from jw_chat_agent_poc.orchestrator.markdown_response import MarkdownResponseBuilder
from jw_chat_agent_poc.orchestrator.provenance import interpretation_has_unverified_numbers, verification_notice
from jw_chat_agent_poc.orchestrator.source_trap import apply_requested_source_trap_gate
from jw_chat_agent_poc.orchestrator.unavailable_response import apply_common_unavailable_response
from jw_chat_agent_poc.service.claim_guardrails import apply_claim_guardrails
from jw_chat_agent_poc.service.answer_safety import (
    FAIL_CLOSED_TEXT,
    answer_has_only_fact_numbers,
    append_competitor_patent_coverage_block,
    append_deterministic_source_block,
    chunk_text,
    cleanup_markdown_answer,
    dedupe_brand_metric_sentence,
    dedupe_repeated_hira_patient_counts,
    ensure_competitive_movement_analysis,
    ensure_causal_structure,
    ensure_hira_patient_summary,
    ensure_hira_sales_link_analysis,
    ensure_issue_question_quant_analysis,
    ensure_share_delta_line,
    ensure_judgment_insight,
    ensure_top_brand_trend_table,
    fact_token_allowed,
    fallback_fact_answer,
    finalized_fallback_fact_answer,
    generation_attempts,
    has_mandatory_numeric_mismatch,
    mandatory_fact_block,
    mandatory_fact_lines,
    missing_mandatory_lines,
    needs_safe_news_summary,
    presentable_mandatory_lines,
    remove_raw_fact_residue,
    remove_mandatory_numeric_mismatches,
    remove_supported_series_contradictions,
    replace_empty_news_shells,
    safe_news_summary_lines,
    ensure_single_brand_trend_analysis,
    single_brand_trend_fact_markdown,
    strict_allowed_numbers,
    strip_generated_source_sections,
)
from jw_chat_agent_poc.common.timing import stage
from jw_chat_agent_poc.service.portfolio_decline_render import ensure_portfolio_decline_summary
from jw_chat_agent_poc.service.web_mi_summary import web_search_mi_section


POLICY_NOTICE_TOOLS = frozenset({"matching_policy_notice"})


def _apply_final_claim_controls(question: str, answer: str, fact_md: str) -> str:
    guarded = apply_claim_guardrails(question, answer, fact_md)
    return apply_claim_policy(question, guarded, fact_md)


def _repair_answer_with_verified_facts(answer: str, strict_numbers: tuple[str, ...], mandatory_lines: tuple[str, ...]) -> str:
    """Remove unsafe numeric lines while preserving the LLM's analysis whenever possible."""

    answer = remove_mandatory_numeric_mismatches(answer, mandatory_lines)
    sanitized = _sanitize_preserving_analysis(answer, strict_numbers)
    parts: list[str] = []
    if sanitized.strip() not in {"", FAIL_CLOSED_TEXT}:
        parts.append(sanitized)
    missing = missing_mandatory_lines(sanitized, mandatory_lines) if sanitized else mandatory_lines
    if missing:
        parts.append("\n".join(presentable_mandatory_lines(missing)))
    if not parts:
        return sanitized
    repaired = cleanup_markdown_answer("\n\n".join(part for part in parts if part.strip()))
    return _sanitize_preserving_analysis(repaired, strict_numbers)


def _sanitize_preserving_analysis(markdown: str, strict_numbers: tuple[str, ...]) -> str:
    """Drop unsafe numeric lines while preserving intact fact-backed prose."""

    allowed = set(strict_numbers)

    lines: list[str] = []
    for raw_line in markdown.splitlines():
        code_tokens = {match.group(0) for match in CODE_RE.finditer(raw_line)}
        if any(not fact_token_allowed(token, allowed) for token in code_tokens):
            continue
        numeric_tokens = {match.group(0) for match in NUMBER_RE.finditer(raw_line)}
        has_disallowed_number = any(token and not fact_token_allowed(token, allowed) for token in numeric_tokens)
        if has_disallowed_number:
            continue
        if raw_line.strip():
            lines.append(raw_line)
    cleaned = cleanup_markdown_answer("\n".join(lines))
    return cleaned if cleaned else FAIL_CLOSED_TEXT


def _needs_trend_fact_prose(question: str, answer: str, trend_fact_md: str = "") -> bool:
    if not _question_wants_trend_output(question):
        return False
    if not trend_fact_md:
        return _prose_sentence_count(answer) < 1
    if _trend_shape_conflicts_with_answer(trend_fact_md, answer):
        return True
    return not _trend_prose_satisfies_fact(answer, trend_fact_md)


def _question_wants_trend_output(question: str) -> bool:
    return "추이" in question and any(token in question for token in ("매출", "점유율", "어때"))


def _trend_shape_conflicts_with_answer(trend_fact_md: str, answer: str) -> bool:
    shape = _trend_fact_field(trend_fact_md, "shape")
    if not shape:
        return False
    body = re.split(r"\n##\s*(?:출처|처리\s*시간)\b", answer, maxsplit=1)[0]
    if shape == "flat":
        return any(token in body for token in ("회복", "반등", "저점 대비"))
    if shape == "rising":
        return any(token in body for token in ("하락세", "감소세", "위축", "저점 이후"))
    if shape == "falling":
        return any(token in body for token in ("회복", "반등", "상승세", "증가세"))
    if shape == "recovery":
        return any(token in body for token in ("정체된 흐름", "좁은 범위", "뚜렷한 반전 없이"))
    return False


def _trend_prose_satisfies_fact(answer: str, trend_fact_md: str) -> bool:
    if not trend_fact_md:
        return False
    prose = _trend_analysis_prose(answer)
    periods = tuple(period for period in _trend_fact_key_periods(trend_fact_md) if period)
    if not periods:
        return _prose_sentence_count(prose) >= 1
    return _prose_sentence_count(prose) >= 1 and all(period in prose for period in periods)


def _trend_analysis_prose(markdown: str) -> str:
    body = re.split(r"\n##\s*(?:출처|처리\s*시간)\b", markdown, maxsplit=1)[0]
    kept: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("|") or line.startswith("#"):
            continue
        if _looks_like_inline_table(line):
            continue
        if re.fullmatch(r"\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?", line):
            continue
        if re.fullmatch(r"\*\*[^*]+\*\*", line):
            continue
        kept.append(line)
    return re.sub(r"\s+", " ", " ".join(kept)).strip()


def _looks_like_inline_table(line: str) -> bool:
    if line.count("|") < 4:
        return False
    compact = re.sub(r"\s+", "", line)
    if "---" in compact:
        return True
    return "기간" in line and any(token in line for token in ("매출", "점유율", "MS"))


def _remove_endpoint_only_trend_sentence(markdown: str, trend_fact_md: str) -> str:
    if not trend_fact_md or not _trend_prose_satisfies_fact(markdown, trend_fact_md):
        return markdown
    key_periods = set(_trend_fact_key_periods(trend_fact_md))
    lines: list[str] = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if (
            "움직였고" in line
            and "변했습니다" in line
            and sum(1 for period in key_periods if period and period in line) <= 2
        ):
            continue
        lines.append(raw_line)
    return cleanup_markdown_answer("\n".join(lines))


def _ensure_trend_prose_fail_closed(question: str, markdown: str, trend_fact_md: str, trend_prose: str) -> str:
    """Keep single-brand trend answers from degrading to table-only output."""

    if not _needs_trend_fact_prose(question, markdown, trend_fact_md):
        return markdown
    candidate_prose = cleanup_markdown_answer(trend_prose)
    if not candidate_prose or _needs_trend_fact_prose(question, candidate_prose, trend_fact_md):
        candidate_prose = _trend_fact_fallback_prose(trend_fact_md)
    if not candidate_prose or _needs_trend_fact_prose(question, candidate_prose, trend_fact_md):
        return markdown
    return cleanup_markdown_answer(_insert_before_first_table(markdown, candidate_prose))


def _ensure_direct_metric_fact_answer(question: str, markdown: str, fact_md: str) -> str:
    """Keep direct share/rank questions from drifting into adjacent trend commentary."""
    if not any(token in question for token in ("점유율", "순위", "시장규모", "시장 규모")):
        return markdown
    fact = _direct_metric_fact(fact_md)
    if not fact:
        return markdown
    share = fact.get("시장점유율", "")
    rank = fact.get("순위", "")
    market_size = fact.get("시장규모", "")
    needs_share = "점유율" in question and bool(share) and share not in markdown
    needs_rank = "순위" in question and bool(rank) and rank not in markdown
    needs_market_size = any(token in question for token in ("시장규모", "시장 규모")) and bool(market_size) and market_size not in markdown
    if not (needs_share or needs_rank or needs_market_size):
        return markdown
    brand = fact.get("브랜드/시장", "해당 브랜드")
    period = fact.get("기간", "최신")
    if needs_market_size:
        line = f"시장 전체는 {period} 기준 시장규모 {market_size}입니다."
        return cleanup_markdown_answer(_insert_before_first_table(markdown, line))
    parts = [f"{brand}는 {period} 기준"]
    metrics: list[str] = []
    if share:
        metrics.append(f"시장점유율 {share}")
    if rank:
        metrics.append(f"순위 {rank}")
    if not metrics:
        return markdown
    line = f"{parts[0]} {', '.join(metrics)}입니다."
    return cleanup_markdown_answer(_insert_before_first_table(markdown, line))


def _direct_metric_fact(fact_md: str) -> dict[str, str]:
    rows: dict[str, str] = {}
    in_metric_table = False
    for raw_line in fact_md.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("### "):
            in_metric_table = "지표 fact" in stripped
            continue
        if not in_metric_table:
            continue
        if not stripped.startswith("|") or "---" in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) >= 2 and cells[0] and cells[1] and cells[0] != "항목":
            rows[cells[0]] = cells[1]
    if rows.get("시장점유율") or rows.get("순위") or rows.get("시장규모"):
        return rows
    return {}


def _trend_fact_fallback_prose(trend_fact_md: str) -> str:
    """Render a minimal prose analysis from verified trend fact when LLM prose is stripped."""
    brand = _trend_fact_field(trend_fact_md, "brand") or "브랜드"
    shape = _trend_fact_field(trend_fact_md, "shape")
    first = _parse_trend_point_summary(_trend_fact_field(trend_fact_md, "first"))
    peak = _parse_trend_point_summary(_trend_fact_field(trend_fact_md, "peak"))
    trough = _parse_trend_point_summary(_trend_fact_field(trend_fact_md, "trough_after_peak"))
    latest = _parse_trend_point_summary(_trend_fact_field(trend_fact_md, "latest"))
    if not first or not latest:
        return ""

    def point_text(point: tuple[str, str, str]) -> str:
        period, sales, share = point
        return f"{period} {sales}" + (f", MS {share}" if share else "")

    sentences: list[str] = []
    if peak and trough and len({first[0], peak[0], trough[0], latest[0]}) >= 3:
        sentences.append(
            f"{brand}는 {point_text(first)}에서 출발해 {point_text(peak)}로 고점을 찍은 뒤 "
            f"{point_text(trough)}까지 낮아졌고, 최신 {point_text(latest)}로 이어졌습니다."
        )
    else:
        sentences.append(f"{brand}는 {point_text(first)}에서 최신 {point_text(latest)}로 이어졌습니다.")
    if shape == "recovery":
        sentences.append("고점 이후 하락 구간이 있었지만 최신 값은 저점 이후 회복 흐름을 보여줍니다.")
    elif shape == "flat":
        sentences.append("전체 흐름은 뚜렷한 한 방향 추세보다 제한된 범위의 등락에 가깝습니다.")
    elif shape == "rising":
        sentences.append("시작점 대비 최신 값이 높아 상승 흐름이 우세합니다.")
    elif shape == "falling":
        sentences.append("시작점 대비 최신 값이 낮아 하락 압력이 우세합니다.")
    else:
        sentences.append("핵심 시점의 매출과 MS를 함께 보면 최신 위치를 기준으로 추세 강도를 판단할 수 있습니다.")
    return cleanup_markdown_answer(" ".join(sentences))


def _ensure_trend_key_period_table(markdown: str, trend_fact_md: str) -> str:
    if not trend_fact_md or not _trend_prose_satisfies_fact(markdown, trend_fact_md):
        return markdown
    periods = _trend_fact_key_periods(trend_fact_md)
    if not periods or _trend_table_contains_periods(markdown, periods):
        return markdown
    table_md = _trend_key_period_table(trend_fact_md)
    if not table_md:
        return markdown
    marker = re.search(r"\n##\s*(?:처리\s*시간|출처)\b", markdown)
    if marker:
        before = markdown[: marker.start()].rstrip()
        after = markdown[marker.start() :].lstrip()
        return cleanup_markdown_answer(f"{before}\n\n{table_md}\n\n{after}")
    return cleanup_markdown_answer(f"{markdown}\n\n{table_md}")


def _ensure_code_rendered_trend_table(markdown: str, fact_md: str, trend_fact_md: str) -> str:
    if not trend_fact_md:
        return markdown
    table_md = _trend_series_table_from_fact_md(fact_md)
    if not table_md:
        return _ensure_trend_key_period_table(markdown, trend_fact_md)
    without_llm_table = _remove_first_trend_display_table(markdown)
    marker = re.search(r"\n##\s*(?:처리\s*시간|출처)\b", without_llm_table)
    if marker:
        before = without_llm_table[: marker.start()].rstrip()
        after = without_llm_table[marker.start() :].lstrip()
        return cleanup_markdown_answer(f"{before}\n\n{table_md}\n\n{after}")
    return cleanup_markdown_answer(f"{without_llm_table}\n\n{table_md}")


def _trend_series_table_from_fact_md(fact_md: str) -> str:
    lines = fact_md.splitlines()
    for index, line in enumerate(lines):
        title = line.strip()
        if not (title.startswith("### ") and "매출 시계열 fact" in title):
            continue
        table_lines: list[str] = []
        cursor = index + 1
        while cursor < len(lines):
            current = lines[cursor].strip()
            if current.startswith("### "):
                break
            if current.startswith("|"):
                table_lines.append(lines[cursor])
            elif table_lines:
                break
            cursor += 1
        if len(table_lines) < 3:
            continue
        display_title = title.removeprefix("### ").replace(" fact", "").strip()
        return "\n".join((f"**{display_title}**", *table_lines))
    return ""


def _remove_first_trend_display_table(markdown: str) -> str:
    lines = markdown.splitlines()
    table_start = -1
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        header = stripped.strip("|")
        if "기간" in header and any(token in header for token in ("매출", "MS", "시장점유율")):
            table_start = index
            break
    if table_start < 0:
        return markdown
    remove_start = table_start
    previous = table_start - 1
    while previous >= 0 and not lines[previous].strip():
        previous -= 1
    if previous >= 0:
        heading = lines[previous].strip()
        if (
            re.fullmatch(r"\*\*[^*]*(?:추이|시계열|핵심)[^*]*\*\*", heading)
            or re.fullmatch(r"#{1,6}\s+.*(?:추이|시계열|핵심).*", heading)
        ):
            remove_start = previous
    remove_end = table_start
    while remove_end < len(lines) and lines[remove_end].strip().startswith("|"):
        remove_end += 1
    while remove_end < len(lines) and not lines[remove_end].strip():
        remove_end += 1
    return cleanup_markdown_answer("\n".join((*lines[:remove_start], *lines[remove_end:])))


def _trend_table_contains_periods(markdown: str, periods: tuple[str, ...]) -> bool:
    table_text = "\n".join(line for line in markdown.splitlines() if line.strip().startswith("|"))
    return all(period in table_text for period in periods)


def _trend_key_period_table(trend_fact_md: str) -> str:
    brand = _trend_fact_field(trend_fact_md, "brand") or "브랜드"
    rows: list[tuple[str, str, str]] = []
    seen_periods: set[str] = set()
    for field, label in (("first", ""), ("peak", "Peak"), ("trough_after_peak", "Trough"), ("latest", "Latest")):
        value = _trend_fact_field(trend_fact_md, field)
        parsed = _parse_trend_point_summary(value)
        if not parsed:
            continue
        period, sales, share = parsed
        if period in seen_periods:
            continue
        seen_periods.add(period)
        display_period = f"{period} ({label})" if label else period
        rows.append((display_period, sales, share))
    if not rows:
        return ""
    lines = [
        f"**{brand} 핵심 추이 시점**",
        f"| 기간 | {brand} 매출 | 시장점유율(MS) |",
        "| --- | --- | --- |",
    ]
    lines.extend(f"| {period} | {sales} | {share or '-'} |" for period, sales, share in rows)
    return "\n".join(lines)


def _parse_trend_point_summary(value: str) -> tuple[str, str, str] | None:
    parts = [part.strip() for part in value.split("/") if part.strip()]
    if len(parts) < 2:
        return None
    period = parts[0]
    sales = parts[1]
    share = ""
    if len(parts) >= 3:
        share = parts[2].removeprefix("MS").strip()
    return period, sales, share


def _trend_fact_key_periods(trend_fact_md: str) -> tuple[str, ...]:
    periods: list[str] = []
    for field in ("first", "peak", "trough_after_peak", "latest"):
        value = _trend_fact_field(trend_fact_md, field)
        period = value.split("/", maxsplit=1)[0].strip() if value else ""
        if period and period not in periods:
            periods.append(period)
    return tuple(periods)


def _trend_fact_field(trend_fact_md: str, field: str) -> str:
    match = re.search(rf"^\|\s*{re.escape(field)}\s*\|\s*([^|]+?)\s*\|", trend_fact_md, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _prose_sentence_count(markdown: str) -> int:
    body = re.split(r"\n##\s*(?:출처|처리\s*시간)\b", markdown, maxsplit=1)[0]
    kept: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("|") or line.startswith("#"):
            continue
        if _looks_like_inline_table(line):
            continue
        if re.fullmatch(r"\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?", line):
            continue
        if re.fullmatch(r"\*\*[^*]+\*\*", line):
            continue
        kept.append(line)
    prose = re.sub(r"\s+", " ", " ".join(kept)).strip()
    return len(tuple(part for part in re.split(r"(?:다\.|[.!?])\s+", prose) if part.strip()))


def _insert_before_first_table(markdown: str, block: str) -> str:
    cleaned_block = cleanup_markdown_answer(block)
    if not cleaned_block:
        return markdown
    lines = markdown.splitlines()
    for idx, line in enumerate(lines):
        if line.strip().startswith("|"):
            return cleanup_markdown_answer("\n".join((*lines[:idx], "", cleaned_block, "", *lines[idx:])))
    return cleanup_markdown_answer("\n\n".join((cleaned_block, markdown)))


def _fact_lookup_markdown(markdown_response: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("fact_md", "data_md", "markdown"):
        value = str(markdown_response.get(key) or "").strip()
        if value and value not in parts:
            parts.append(value)
    return "\n\n".join(parts)


def _requires_deterministic_external_relay(tool_calls: list[dict[str, Any]] | None) -> bool:
    return any(call.get("tool") == "search_drug_info" for call in tool_calls or ())


def _deterministic_external_relay_answer(markdown_response: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("data_md", "fact_md"):
        value = str(markdown_response.get(key) or "").strip()
        if value and value not in parts:
            parts.append(value)
    for key in ("evidence_md", "sources_md", "notice_md"):
        value = str(markdown_response.get(key) or "").strip()
        if value:
            parts.append(value)
    return cleanup_markdown_answer("\n\n".join(parts))


def _uploaded_file_context(agent_result: dict[str, Any]) -> str:
    value = agent_result.get("file_context")
    return value.strip() if isinstance(value, str) else ""


def _append_uploaded_file_source(answer: str, file_context: str) -> str:
    if not file_context.strip():
        return answer
    source_line = "- 업로드 파일: 현재 세션에 저장된 파일 검색 결과"
    if source_line in answer:
        return answer
    if re.search(r"(?m)^##\s*출처\b", answer):
        return cleanup_markdown_answer("\n".join((answer, source_line)))
    source_block = f"## 출처\n\n{source_line}"
    return cleanup_markdown_answer("\n\n".join((answer, source_block)))


def _append_blocked_metric_notices(answer: str, fact_md: str) -> str:
    rows = _blocked_metric_notice_lines(fact_md)
    missing = tuple(line for line in rows if line not in answer)
    if not missing:
        return answer
    block = "\n".join(("- 조회 실패값 차단:", *missing))
    marker = re.search(r"\n##\s*(?:출처|처리\s*시간)\b", answer)
    if marker:
        before = answer[: marker.start()].rstrip()
        after = answer[marker.start() :].lstrip()
        return cleanup_markdown_answer(f"{before}\n\n{block}\n\n{after}")
    return cleanup_markdown_answer("\n\n".join((answer, block)))


def append_blocked_metric_notices_from_markdown_response(answer: str, markdown_response: dict[str, Any] | None) -> str:
    if not isinstance(markdown_response, dict):
        return answer
    return _append_blocked_metric_notices(answer, _fact_lookup_markdown(markdown_response))


def _blocked_metric_notice_lines(fact_md: str) -> tuple[str, ...]:
    rows: list[str] = []
    seen_messages: set[str] = set()
    for raw_line in fact_md.splitlines():
        stripped = raw_line.strip()
        if not stripped.startswith("|") or "---" in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 2 or cells[0] != "조회 차단":
            continue
        message = cells[1]
        if message and message not in seen_messages:
            seen_messages.add(message)
            rows.append(f"- {message}")
    return tuple(rows)


def _append_web_search_section(answer: str, tool_calls: list[dict[str, Any]] | None) -> str:
    section = _web_search_unverified_section(tool_calls)
    if not section:
        return answer
    body = _ensure_web_search_reference(answer)
    return "\n\n".join(part.strip() for part in (body, section) if part.strip())


def _ensure_web_search_reference(answer: str) -> str:
    reference = "웹 검색 결과는 하단 웹 검색 결과(미검증) 섹션을 참조하세요."
    if reference in answer:
        return answer
    marker = re.search(r"\n##\s*출처\b", answer)
    if marker:
        before = answer[: marker.start()].rstrip()
        after = answer[marker.start() :].lstrip()
        return cleanup_markdown_answer(f"{before}\n\n{reference}\n\n{after}")
    return cleanup_markdown_answer("\n\n".join((answer, reference)))


def _web_search_unverified_section(tool_calls: list[dict[str, Any]] | None) -> str:
    rows: list[dict[str, Any]] = []
    for call in tool_calls or []:
        if str(call.get("tool") or "") != "web_search" and str(call.get("source") or "") != "web_search":
            continue
        data = call.get("render_data")
        if not isinstance(data, dict):
            continue
        for item in _web_search_items(data):
            rows.append(item)
    if not rows:
        return ""
    return web_search_mi_section(rows[:5])


def _web_search_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    direct = data.get("items")
    if isinstance(direct, list):
        return [item for item in direct if isinstance(item, dict)]
    calls = data.get("calls")
    if not isinstance(calls, list):
        return []
    rows: list[dict[str, Any]] = []
    for call in calls:
        render_data = call.get("render_data") if isinstance(call, dict) else None
        if not isinstance(render_data, dict):
            continue
        nested = render_data.get("items")
        if isinstance(nested, list):
            rows.extend(item for item in nested if isinstance(item, dict))
    return rows


def _has_web_search_rows(tool_calls: list[dict[str, Any]] | None) -> bool:
    return bool(_web_search_unverified_section(tool_calls))


def _is_web_search_only(tool_calls: list[dict[str, Any]] | None) -> bool:
    calls = [call for call in tool_calls or [] if isinstance(call, dict)]
    if not calls:
        return False
    for call in calls:
        if str(call.get("tool") or "") != "web_search" and str(call.get("source") or "") != "web_search":
            return False
    return True


@dataclass(frozen=True, slots=True)
class GenosClient:
    base_url: str = field(default_factory=resolve_final_genos_base_url)
    token: str | None = field(default_factory=resolve_final_genos_token)
    timeout_s: int = field(default_factory=lambda: int(os.environ.get("GENOS_FINAL_TIMEOUT_S", "70")))
    token_usage_calls: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False, compare=False)

    def stream_answer(self, question: str, agent_result: dict[str, Any]) -> Iterator[str]:
        markdown_response = agent_result.get("markdown_response")
        timing = agent_result.get("timing") if isinstance(agent_result.get("timing"), dict) else None
        file_context = _uploaded_file_context(agent_result)
        if self.token and isinstance(markdown_response, dict):
            tool_calls = agent_result.get("tool_calls")
            if _requires_deterministic_external_relay(tool_calls if isinstance(tool_calls, list) else None):
                yield from chunk_text(_deterministic_external_relay_answer(markdown_response))
                return
            yield from chunk_text(
                cleanup_markdown_answer(
                    self._markdown_answer(
                        question,
                        markdown_response,
                        timing,
                        tool_calls if isinstance(tool_calls, list) else None,
                        file_context,
                    )
                )
            )
            return
        if self._is_cache_only(agent_result) or not self.token:
            yield from chunk_text(cleanup_markdown_answer(str(agent_result["answer"])))
            return
        policy_notice = self._policy_notice_block(agent_result)
        messages = [
            {
                "role": "system",
                "content": (
                    "너는 JW 시장분석 채팅 에이전트다. 제공된 tool result만 근거로 답하고, "
                    "cache metric summary_text에 포함된 매출, MS, 순위, 시장규모 숫자는 "
                    "사용자 질문이 해당 지표를 묻는 경우 생략하지 말고 그대로 포함하라. "
                    "metric_facts가 있으면 그것이 정답 숫자이므로 질문과 관련된 필드는 반드시 답변에 반영하라. "
                    "uploaded_file_context가 있으면 내부 mart fact와 구분해 업로드 파일 기준으로 답하라. "
                    "notice_count가 1 이상이어도 주의/면책 문구를 작성하지 말라. "
                    "사용자에게 필요한 결론만 남기고 내부 처리 기준은 숨겨라. "
                    "출처 섹션이나 출처 줄은 작성하지 말라. 출처는 시스템이 별도로 붙인다."
                ),
            },
            {"role": "user", "content": self._prompt(question, agent_result)},
        ]
        yield from self._stream_chat(messages)
        if policy_notice:
            yield policy_notice

    def _markdown_answer(
        self,
        question: str,
        markdown_response: dict[str, Any],
        timing: dict[str, Any] | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        file_context: str = "",
    ) -> str:
        allowed_numbers = tuple(str(value) for value in markdown_response.get("allowed_numbers", ()) if value is not None)
        fact_md = str(markdown_response.get("fact_md") or markdown_response.get("data_md") or "")
        fact_lookup_md = _fact_lookup_markdown(markdown_response)
        if _is_web_search_only(tool_calls) and _has_web_search_rows(tool_calls):
            return _append_web_search_section("", tool_calls)
        trend_fact_md = single_brand_trend_fact_markdown(fact_lookup_md, tool_calls) if _question_wants_trend_output(question) else ""
        fact_for_safety = "\n\n".join(part for part in (fact_md, trend_fact_md, file_context) if part)
        strict_numbers = strict_allowed_numbers(fact_for_safety, allowed_numbers)
        messages = self._markdown_messages(question, markdown_response, trend_fact_md, file_context)
        try:
            with stage(timing, "final_llm_expression", "GenOS markdown generation"):
                raw_interpretation = self._chat_text(messages)
        except requests.RequestException:
            fallback = finalized_fallback_fact_answer(question, markdown_response)
            return _apply_final_claim_controls(question, fallback, fact_md)
        if not raw_interpretation:
            fallback = finalized_fallback_fact_answer(question, markdown_response)
            return _apply_final_claim_controls(question, fallback, fact_md)
        # Fast final path: the primary markdown prompt already receives trend_fact_md
        # and explicitly asks for trend prose. Avoid a second pre-safety LLM call;
        # deterministic guards below keep verified facts and numeric safety.
        trend_prose_candidate = ""
        mandatory_lines = mandatory_fact_lines(fact_md)
        raw_has_unverified_number = interpretation_has_unverified_numbers(raw_interpretation, strict_numbers)
        missing_mandatory = missing_mandatory_lines(raw_interpretation, mandatory_lines)
        if missing_mandatory and not raw_has_unverified_number:
            raw_interpretation = cleanup_markdown_answer(
                "\n\n".join((raw_interpretation, "\n".join(presentable_mandatory_lines(missing_mandatory))))
            )
        with stage(timing, "answer_safety", "fact-number validation"):
            removed_unverified_number = interpretation_has_unverified_numbers(raw_interpretation, strict_numbers)
            answer = _sanitize_preserving_analysis(raw_interpretation, strict_numbers)
            answer = cleanup_markdown_answer(remove_supported_series_contradictions(answer, fact_md))
            fail_closed = answer.strip() in {"", FAIL_CLOSED_TEXT}
            if fail_closed and mandatory_lines and not removed_unverified_number:
                answer = cleanup_markdown_answer("\n".join(presentable_mandatory_lines(mandatory_lines)))
                answer = _sanitize_preserving_analysis(answer, strict_numbers)
                fail_closed = answer.strip() in {"", FAIL_CLOSED_TEXT}
            if not fail_closed:
                if has_mandatory_numeric_mismatch(answer, mandatory_lines):
                    answer = _repair_answer_with_verified_facts(answer, strict_numbers, mandatory_lines)
                    fail_closed = answer.strip() in {"", FAIL_CLOSED_TEXT}
                missing_after_cleanup = missing_mandatory_lines(answer, mandatory_lines)
                if missing_after_cleanup:
                    answer = cleanup_markdown_answer("\n\n".join((answer, "\n".join(presentable_mandatory_lines(missing_after_cleanup)))))
                answer = ensure_judgment_insight(question, answer, fact_md)
                answer = ensure_competitive_movement_analysis(question, answer, fact_md)
                answer = replace_empty_news_shells(answer, fact_md)
                if needs_safe_news_summary(question, answer, fact_md):
                    news_lines = safe_news_summary_lines(fact_md)
                    if news_lines:
                        answer = cleanup_markdown_answer("\n\n".join((answer, "\n".join(news_lines))))
                answer = ensure_share_delta_line(question, answer, fact_md)
                answer = ensure_causal_structure(question, answer, fact_md)
                answer = ensure_single_brand_trend_analysis(question, answer, fact_md, tool_calls)
                answer = ensure_issue_question_quant_analysis(question, answer, fact_md)
                answer = ensure_competitive_movement_analysis(question, answer, fact_md)
                answer = strip_generated_source_sections(answer)
                answer = ensure_hira_patient_summary(question, answer, fact_md)
                answer = ensure_hira_sales_link_analysis(question, answer, fact_md)
                answer = dedupe_repeated_hira_patient_counts(answer, mandatory_lines)
                answer = remove_raw_fact_residue(answer, fact_md)
                if not answer_has_only_fact_numbers(answer, strict_numbers):
                    repaired = _repair_answer_with_verified_facts(answer, strict_numbers, mandatory_lines)
                    if repaired.strip() not in {"", FAIL_CLOSED_TEXT}:
                        answer = repaired
                    else:
                        answer = cleanup_markdown_answer(fallback_fact_answer(markdown_response))
                    answer = ensure_share_delta_line(question, answer, fact_md)
                    answer = ensure_causal_structure(question, answer, fact_md)
                    answer = ensure_single_brand_trend_analysis(question, answer, fact_md, tool_calls)
                    answer = ensure_issue_question_quant_analysis(question, answer, fact_md)
                    answer = ensure_competitive_movement_analysis(question, answer, fact_md)
                    answer = strip_generated_source_sections(answer)
                    answer = ensure_hira_patient_summary(question, answer, fact_md)
                    answer = ensure_hira_sales_link_analysis(question, answer, fact_md)
                    answer = dedupe_repeated_hira_patient_counts(answer, mandatory_lines)
                    answer = remove_raw_fact_residue(answer, fact_md)
        # Keep answer_safety as the only post-generation fact/number gate here.
        # Do not perform post-safety trend regeneration; deterministic trend/table
        # guards below decide whether verified trend material can be kept.
        if removed_unverified_number and fail_closed:
            answer = cleanup_markdown_answer("\n\n".join((FAIL_CLOSED_TEXT, verification_notice())))
        answer = _remove_endpoint_only_trend_sentence(remove_raw_fact_residue(answer, fact_md), trend_fact_md)
        answer = _ensure_trend_prose_fail_closed(question, answer, trend_fact_md, trend_prose_candidate)
        answer = _ensure_direct_metric_fact_answer(question, answer, fact_md)
        answer = _ensure_code_rendered_trend_table(answer, fact_lookup_md, trend_fact_md)
        answer = ensure_top_brand_trend_table(answer, fact_md)
        answer = ensure_portfolio_decline_summary(answer, fact_md)
        answer = dedupe_brand_metric_sentence(answer, fact_md)
        answer = _apply_final_claim_controls(question, answer, fact_md)
        answer = append_competitor_patent_coverage_block(answer, fact_md)
        answer = _append_blocked_metric_notices(answer, fact_lookup_md)
        answer = append_deterministic_source_block(answer, fact_md)
        answer = _append_uploaded_file_source(answer, file_context)
        answer = apply_common_unavailable_response(question, answer, markdown_response)
        answer = apply_requested_source_trap_gate(question, answer)
        return _append_web_search_section(answer, tool_calls)

    @staticmethod
    def _markdown_messages(
        question: str,
        markdown_response: dict[str, Any],
        trend_fact_md: str = "",
        file_context: str = "",
    ) -> list[dict[str, str]]:
        fact_md = str(markdown_response.get("fact_md") or markdown_response.get("data_md") or "")
        mandatory_md = mandatory_fact_block(fact_md)
        uploaded_md = file_context.strip() or "- 없음"
        return [
            {
                "role": "system",
                "content": (
                    "너는 JW 시장분석 채팅 에이전트다. 제공된 확정 fact만 근거로 답변 전체를 자연스러운 한국어 Markdown으로 작성한다. "
                    "업로드 파일 컨텍스트가 있으면 내부 mart fact와 구분해 '업로드 파일 기준'이라고 밝히고, 그 내용은 현재 세션 첨부 파일 근거로만 사용한다. "
                    "확정 fact set 안에 '필수 답변 fact'가 있으면 각 행을 답변 본문에 반드시 반영한다. "
                    "특히 데이터 미보유/미지원 행과 조회 실패 행은 별도 문장으로 명확히 쓰고, 비교 브랜드가 있으면 그 한계를 생략하지 않는다. "
                    "반대로 상위 브랜드 월별 MS fact에 있는 브랜드는 시계열 데이터가 있는 것이므로 미지원, 확인 안 됨, 데이터 없음이라고 쓰지 않는다. "
                    "질문의 모든 의도를 반영한다. 예를 들어 비교 브랜드, 뉴스 이슈, 주 브랜드 매출 변화가 함께 있으면 각각 빠뜨리지 않는다. "
                    "질문이 매출, 시장점유율/MS, 순위, 환자수 같은 특정 지표를 직접 물으면 이슈 맥락보다 먼저 해당 지표의 브랜드명·기간·값·순위를 본문에 반드시 쓴다. "
                    "매출 변화, 증감, 추이, 대비 질문은 기준값, 비교값, 변화액, 변화율 또는 %p fact를 반드시 포함한다. "
                    "시장과 브랜드 시계열을 함께 보는 질문은 같은 방향/다른 방향, 변화율 격차, 동반 하락·브랜드 고유 압력 같은 근거 기반 인과 분석을 적극 생성한다. "
                    "판단형 질문은 결론을 첫 문장에 명확히 쓰고, 근거와 시사점/한계까지 포함해 답한다. "
                    "예: 결론 1문장, 핵심 근거 2개, 시사점 또는 추가 관찰 필요성 1문장을 포함한다. "
                    "허용되는 해석은 fact에서 논리적으로 따라오는 비교, 경향, 위험도 평가, 원인 후보, 배경, 작용 경로, 시사점이다. "
                    "근거 기반 인과 분석은 핵심 산출이다. 왜 이런가, 무엇을 시사하는가, 어떤 경쟁 압력으로 볼 수 있는가를 근거끼리 연결해 설명한다. "
                    "단 집계 점유율·매출이 반대 방향으로 움직였다는 사실만으로 잠식, 흡수, 대체, 직접 처방 전환을 단정하지 않는다. 직접 전환 데이터가 없으면 반대 방향 변화와 한계를 함께 쓴다. "
                    "HIRA 환자수 fact가 미반환이면 침투율, 환자당 처방액, 환자 기반 수요 여력 같은 파생 지표를 만들거나 제안하지 않는다. "
                    "금지되는 것은 거짓 수치, 존재하지 않는 기사, fact 밖 사실 날조, 근거 없는 단정뿐이다. "
                    "resolve_relative_date fact가 있으면 그 period를 상대기간으로 사용하고, 다른 월 row를 3달 전 등 상대기간으로 부르지 않는다. "
                    "상태가 unsupported이거나 지표 조회 대상을 확정하지 못했다는 fact는 데이터 미보유/미지원으로 명확히 답하고 추정하지 않는다. "
                    "상태가 error 또는 query_failed인 fact는 데이터가 없다는 뜻으로 쓰지 말고 조회 실패로 답하며 수치를 추정하지 않는다. "
                    "뉴스 fact는 사업 이슈 맥락을 설명하는 정성 근거로 사용한다. 뉴스를 쓰면 반드시 '출처(날짜) [「제목」](URL) — 요약/발췌의 핵심 이슈' 형태로 실제 기사 제목·날짜·출처·URL·요약 내용을 드러낸다. "
                    "'관련 기사에서 언급이 확인됐다'처럼 존재만 표시하는 빈 문장은 쓰지 않는다. "
                    "뉴스 본문·발췌의 기사 숫자, 분기, 전년대비, 증감률은 기사 맥락으로만 다루고 UBIST 정량 지표처럼 해석하지 않는다. "
                    "인사이트 계산 fact가 있으면 share-of-growth, 성장분해, gain-loss, cohort 상대화를 숫자 나열이 아니라 기준 대비 편차·교차·so-what과 인과적 해석으로 설명한다. "
                    "단일 브랜드 추이 산문용 trend fact가 있으면 표만 쓰지 말고 해당 brand, grain, shape, first/peak/trough_after_peak/latest, 시장 first/latest 값을 사용해 2문장 이상의 추이 산문을 쓴다. "
                    "trend fact의 shape가 flat이면 회복·반등이라고 쓰지 말고 좁은 범위 등락/정체로 표현하며, recovery이면 저점 후 반등을 설명한다. "
                    "추이 산문에는 trend fact에 없는 기간·값·원인을 만들지 않는다. "
                    "비자명 신호가 없으면 억지 결론을 만들지 말고, 신호가 있으면 근거 기반 추론임을 밝히며 적극 분석한다. "
                    "뉴스 기준, 필터명, date_grain, on_list, impact_score, 내부 cache, fact_id 같은 내부 메타를 노출하지 않는다. "
                    "내부 저장소명이나 내부 식별자를 쓰지 않는다. "
                    "출처 섹션이나 출처 줄은 작성하지 말라. 출처는 시스템이 검증 fact에서 구조화해 맨 뒤에 붙인다. "
                    "표는 꼭 필요한 경우에만 쓰고, 모든 표와 시계열 제목에는 브랜드명과 기간을 명시한다. 익명 '브랜드 시계열'은 쓰지 않는다. "
                    "같은 지표를 반복하지 않는다. 근거는 본문 분석에 녹이고 출처 표기는 생성하지 않는다. "
                    "숫자, 비율, 순위, 기간, 질병코드는 fact set에 있는 값만 사용하고 새 값을 만들지 않는다. "
                    "검은 별표 같은 장식 기호를 쓰지 말고, 간결한 한국어로 답한다."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"질문: {question}\n\n"
                    f"필수 답변 fact (각 행을 본문에 반영):\n{mandatory_md or '- 없음'}\n\n"
                    f"단일 브랜드 추이 산문용 trend fact:\n{trend_fact_md or '- 없음'}\n\n"
                    f"업로드 파일 컨텍스트 (현재 세션 첨부 파일 검색 결과, 내부 mart fact와 별도):\n{uploaded_md}\n\n"
                    "확정 fact set:\n"
                    f"{fact_md}\n\n"
                    "작성 형식: 결론을 먼저 쓰고, 질문 의도별 핵심 근거와 시사점/한계를 fact 범위 안에서 설명한 뒤 필요한 표를 최소화한다. "
                    "질문이 특정 지표값을 직접 물으면 그 지표의 브랜드명·기간·값을 첫 단락에 먼저 쓴다. "
                    "출처 섹션이나 출처 줄은 쓰지 않는다."
                ),
            },
        ]

    def _chat_text(self, messages: list[dict[str, str]]) -> str:
        last_error: requests.RequestException | None = None
        for _attempt in range(generation_attempts()):
            try:
                text = "".join(self._stream_chat(messages)).strip()
            except requests.RequestException as exc:
                last_error = exc
                continue
            if text:
                return text
        if last_error is not None:
            raise last_error
        return ""

    @staticmethod
    def _trend_prose_messages(question: str, trend_fact_md: str, previous_answer: str) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "너는 JW 시장분석 답변의 추이 산문만 작성한다. "
                    "제공된 단일 브랜드 추이 trend fact 안의 brand, grain, shape, first, peak, trough_after_peak, latest, market_first, market_latest 값만 사용한다. "
                    "새 기간, 새 수치, 새 원인을 만들지 않는다. 표, 출처, 처리시간은 쓰지 않는다. "
                    "포인트 수, 분기 수, 대략 범위처럼 trend fact에 없는 숫자 표현도 쓰지 않는다. "
                    "'억원대', '~', '약', '최근 N분기'처럼 fact에 없는 반올림·범위·개수 표현은 금지한다. "
                    "first/peak/trough_after_peak/latest에 적힌 기간과 값만 그대로 복사해 쓴다. "
                    "shape가 flat이면 회복·반등이라고 쓰지 말고 좁은 범위 등락이나 정체로 설명한다. "
                    "shape가 recovery이면 peak→trough→latest 흐름을 근거로 저점 후 반등을 설명한다. "
                    "분석 방법을 설명하는 메타 문장 없이, 질문에 답하는 자연스러운 한국어 산문 2문장만 쓴다."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"질문: {question}\n\n"
                    f"단일 브랜드 추이 trend fact:\n{trend_fact_md}\n\n"
                    "현재 답변은 표 중심이라 표 앞에 들어갈 산문만 필요하다.\n"
                    f"현재 답변:\n{previous_answer}"
                ),
            },
        ]

    def _stream_chat(self, messages: list[dict[str, str]]) -> Iterator[str]:
        response = requests.post(
            f"{self.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"messages": messages, "stream": True, "temperature": 0.0, "stream_options": {"include_usage": True}},
            stream=True,
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        usage_call: dict[str, Any] | None = None
        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line or not raw_line.startswith("data:"):
                continue
            payload = raw_line.removeprefix("data:").strip()
            if payload == "[DONE]":
                break
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                continue
            usage_call = usage_call_from_payload(data, base_url=self.base_url, stream=True) or usage_call
            token = self._extract_delta_from_data(data)
            if token:
                yield token
        if usage_call is not None:
            self.token_usage_calls.append(usage_call)

    @staticmethod
    def _extract_delta(payload: str) -> str:
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return ""
        return GenosClient._extract_delta_from_data(data)

    @staticmethod
    def _extract_delta_from_data(data: dict[str, Any]) -> str:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        delta = first.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
            return delta["content"]
        message = first.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
        return ""

    @staticmethod
    def _prompt(question: str, agent_result: dict[str, Any]) -> str:
        compact_calls = []
        policy_notices = []
        for call in agent_result.get("tool_calls", [])[:10]:
            if not isinstance(call, dict):
                continue
            if call.get("tool") in POLICY_NOTICE_TOOLS:
                summary = call.get("summary_text")
                if isinstance(summary, str) and summary:
                    policy_notices.append(summary)
                continue
            item: dict[str, Any] = {
                "tool": call.get("tool"),
                "source": source_label(call.get("source")),
                "summary_text": call.get("summary_text"),
            }
            facts = GenosClient._metric_facts(call.get("render_data"))
            if facts:
                item["metric_facts"] = facts
            compact_calls.append(item)
        payload = {
            "question": question,
            "resolution": agent_result.get("resolution"),
            "decomposition": agent_result.get("decomposition"),
            "sources": source_labels(agent_result.get("sources", [])),
            "tool_summaries": compact_calls,
            "uploaded_file_context": _uploaded_file_context(agent_result) or None,
            "notice_count": len(policy_notices),
            "fallback_answer": agent_result.get("answer"),
        }
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _policy_notice_block(agent_result: dict[str, Any]) -> str:
        notices: list[str] = []
        for call in agent_result.get("tool_calls", []):
            if not isinstance(call, dict) or call.get("tool") not in POLICY_NOTICE_TOOLS:
                continue
            summary = call.get("summary_text")
            if isinstance(summary, str) and summary:
                notices.append(summary)
        if not notices:
            return ""
        joined = "\n".join(f"- {notice}" for notice in notices)
        return f"\n\n주의:\n{joined}"

    @staticmethod
    def _is_cache_only(agent_result: dict[str, Any]) -> bool:
        if _uploaded_file_context(agent_result):
            return False
        sources = agent_result.get("sources")
        return isinstance(sources, list) and set(sources).issubset({"cache", "deep_analysis_events"})

    @staticmethod
    def _metric_facts(render_data: Any) -> dict[str, Any]:
        if not isinstance(render_data, dict):
            return {}
        keys = (
            "brand",
            "market_name",
            "period",
            "sales_억원",
            "ms_recent_pct",
            "rank",
            "total_brands_in_market",
            "market_size_억원",
            "gr_mom_pct",
            "gr_qoq_pct",
            "gr_yoy_pct",
            "gr_yoy_mat_pct",
            "gr_yoy_ym_pct",
        )
        return {key: render_data[key] for key in keys if render_data.get(key) is not None}
