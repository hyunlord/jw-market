from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import requests

from jw_chat_agent_poc.genos_config import (
    resolve_deep_genos_base_url,
    resolve_deep_genos_token,
    resolve_final_genos_base_url,
    resolve_final_genos_token,
)
from jw_chat_agent_poc.agent_loop.factory import PRESCRIPTION_METRIC_UNAVAILABLE_REASON
from jw_chat_agent_poc.agent_loop.requested_source import (
    extract_requested_sources,
    normalize_source,
    served_source_from_calls,
    source_basis_notice,
)
from jw_chat_agent_poc.common.token_usage import usage_call_from_payload
from jw_chat_agent_poc.orchestrator.markdown_formatting import CODE_RE, NUMBER_RE
from jw_chat_agent_poc.orchestrator.markdown_formatting import allowed_numbers as markdown_allowed_numbers
from jw_chat_agent_poc.orchestrator.markdown_formatting import source_label, source_labels, table
from jw_chat_agent_poc.orchestrator.claim_policy import apply_claim_policy
from jw_chat_agent_poc.orchestrator.markdown_response import MarkdownResponseBuilder
from jw_chat_agent_poc.orchestrator.answer_completeness import (
    deterministic_single_period_sales_answer,
    deterministic_top_n_share_answer,
)
from jw_chat_agent_poc.orchestrator.answer_contract import (
    NewsSelection,
    build_news_selection,
    enforce_news_claim_selection,
    news_selection_prompt_fact_markdown,
)
from jw_chat_agent_poc.orchestrator.market_answer_contract import enforce_market_answer_contract
from jw_chat_agent_poc.orchestrator.market_insights import render_market_narrative
from jw_chat_agent_poc.orchestrator.narrative_intent import wants_market_narrative
from jw_chat_agent_poc.orchestrator.provenance import interpretation_has_unverified_numbers, verification_notice
from jw_chat_agent_poc.orchestrator.source_trap import apply_requested_source_trap_gate
from jw_chat_agent_poc.orchestrator.source_grading import is_web_search_call
from jw_chat_agent_poc.orchestrator.unavailable_response import apply_common_unavailable_response
from jw_chat_agent_poc.service.claim_guardrails import apply_claim_guardrails
from jw_chat_agent_poc.service.answer_delivery import ANSWER_BRANCHES
from jw_chat_agent_poc.tool_use.reimbursement_evidence import (
    is_reimbursement_identity_notice,
)
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
    ensure_file_absence_statement,
    ensure_hira_patient_summary,
    ensure_hira_sales_link_analysis,
    ensure_issue_question_quant_analysis,
    ensure_share_delta_line,
    ensure_judgment_insight,
    ensure_natural_fact_lead,
    ensure_top_brand_trend_table,
    fact_token_allowed,
    fallback_fact_answer,
    finalized_fallback_fact_answer,
    replace_internal_fact_dump,
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
    uploaded_file_fact_tokens,
)
from jw_chat_agent_poc.common.timing import latency_observation, stage
from jw_chat_agent_poc.service.portfolio_decline_render import ensure_portfolio_decline_summary
from jw_chat_agent_poc.service.web_mi_summary import web_search_mi_section_from_calls


POLICY_NOTICE_TOOLS = frozenset({"matching_policy_notice"})
_DETERMINISTIC_MARKET_NARRATIVE_TOOLS = frozenset(
    {
        "agent_calculation",
        "get_brand_metric",
        "get_market_landscape",
        "get_market_scope",
        "get_top_brands",
        "query_spec",
        "resolve_relative_date",
    }
)
LOGGER = logging.getLogger(__name__)
_FINAL_GENERATION_DEADLINE: ContextVar[float | None] = ContextVar("final_generation_deadline", default=None)
_FINAL_GENERATION_TIMING: ContextVar[dict[str, Any] | None] = ContextVar(
    "final_generation_timing",
    default=None,
)

_FILE_QUOTE_INSTRUCTION = (
    "업로드 파일 컨텍스트가 있고 질문이 파일의 값·비율·식별코드·고유 토큰·문구를 요구하면, "
    "컨텍스트에서 확인된 해당 값과 코드·토큰을 원문 표기 그대로 답변 본문에 반드시 포함한다. "
    "컨텍스트에 문서 전체 키워드 검색·지정 페이지 직접 조회·SQL 직접 조회가 명시된 경우에만 "
    "찾을 수 없는 대상을 명시한다. 부분 검색 컨텍스트만으로 정보가 없다고 단정하지 않는다."
)
_FILE_OVERVIEW_QUESTION_RE = re.compile(
    r"(?:문서|보고서|파일|발표).{0,16}(?:요약|핵심|결론|뭐에\s*관한|무슨\s*내용)"
    r"|(?:요약|핵심|결론).{0,16}(?:문서|보고서|파일|발표)",
    re.IGNORECASE,
)
_FILE_OVERVIEW_SYNTHESIS_INSTRUCTION = (
    " 문서 전체를 묻는 질문이면 제공된 문서 전체 수준의 요약·결론·미충족 수요 블록을 모두 검토해 "
    "공통 결론과 핵심 시장 맥락을 개별 질환 배경이나 단일 표보다 먼저 종합한다. "
    "서로 다른 개요 블록의 근거를 빠뜨리거나 같은 내용을 반복하지 않는다."
)


def _apply_final_claim_controls(question: str, answer: str, fact_md: str) -> str:
    guarded = apply_claim_guardrails(question, answer, fact_md)
    return apply_claim_policy(question, guarded, fact_md)


def _prompt_fact_markdown(fact_md: str) -> str:
    """Remove prompt-only fact duplication without changing verification facts."""

    unique_blocks: list[str] = []
    seen: set[str] = set()
    for raw_block in re.split(r"(?m)(?=^### )", fact_md):
        block = raw_block.strip()
        if not block or block.startswith("### 필수 답변 fact"):
            continue
        if block in seen:
            continue
        seen.add(block)
        unique_blocks.append(block)
    return "\n\n".join(unique_blocks)


def _news_selection(markdown_response: Mapping[str, Any]) -> NewsSelection | None:
    selection = markdown_response.get("_news_selection")
    return selection if isinstance(selection, NewsSelection) else None


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
    return wants_market_narrative(question)


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
    fallback_prose = _trend_fact_fallback_prose(trend_fact_md)
    if candidate_prose and _needs_trend_fact_prose(question, candidate_prose, trend_fact_md) and fallback_prose:
        candidate_prose = cleanup_markdown_answer(f"{candidate_prose}\n\n{fallback_prose}")
    elif not candidate_prose:
        candidate_prose = fallback_prose
    if not candidate_prose or _needs_trend_fact_prose(question, candidate_prose, trend_fact_md):
        return markdown
    return cleanup_markdown_answer("\n\n".join((candidate_prose, markdown)))


def _ensure_direct_metric_fact_answer(question: str, markdown: str, fact_md: str) -> str:
    """Keep direct metric questions from drifting into adjacent trend commentary."""
    compact_question = question.casefold()
    wants_momentum = "momentum" in compact_question or "모멘텀" in question
    wants_ei = re.search(r"(?<![a-z0-9_])ei(?![a-z0-9_])", compact_question) is not None
    if not any(token in question for token in ("점유율", "순위", "시장규모", "시장 규모")) and not (
        wants_momentum or wants_ei
    ):
        return markdown
    fact = _direct_metric_fact(fact_md)
    if not fact:
        return markdown
    share = fact.get("시장점유율", "")
    rank = fact.get("순위", "")
    market_size = fact.get("시장규모", "")
    momentum = fact.get("Momentum", "")
    ei = fact.get("EI", "")
    needs_share = "점유율" in question and bool(share) and share not in markdown
    needs_rank = "순위" in question and bool(rank) and rank not in markdown
    needs_market_size = any(token in question for token in ("시장규모", "시장 규모")) and bool(market_size) and market_size not in markdown
    needs_momentum = wants_momentum and bool(momentum) and not ("Momentum" in markdown and momentum in markdown)
    needs_ei = wants_ei and bool(ei) and not ("EI" in markdown and ei in markdown)
    if not (needs_share or needs_rank or needs_market_size or needs_momentum or needs_ei):
        return markdown
    brand = fact.get("브랜드/시장", "해당 브랜드")
    period = fact.get("기간") or fact.get("사용 가능한 최신 기준") or "최신"
    if needs_market_size:
        line = f"시장 전체는 {period} 기준 시장규모 {market_size}입니다."
        return cleanup_markdown_answer(_insert_before_first_table(markdown, line))
    parts = [f"{brand}는 {period} 기준"]
    metrics: list[str] = []
    if share:
        metrics.append(f"시장점유율 {share}")
    if rank:
        metrics.append(f"순위 {rank}")
    if wants_momentum and momentum:
        metrics.append(f"Momentum {momentum}")
    if wants_ei and ei:
        metrics.append(f"EI {ei}")
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
    if any(rows.get(label) for label in ("시장점유율", "순위", "시장규모", "Momentum", "EI")):
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
            section_start = _table_section_start(lines, idx)
            return cleanup_markdown_answer(
                "\n".join((*lines[:section_start], "", cleaned_block, "", *lines[section_start:]))
            )
    return cleanup_markdown_answer("\n\n".join((cleaned_block, markdown)))


def _table_section_start(lines: list[str], table_start: int) -> int:
    previous = table_start - 1
    while previous >= 0 and not lines[previous].strip():
        previous -= 1
    if previous < 0:
        return table_start
    heading = lines[previous].strip()
    if re.fullmatch(r"(?:#{1,6}\s+\S.*|\*\*[^*\n]+\*\*)", heading):
        return previous
    return table_start


def _ensure_mfds_permit_date_answer(question: str, markdown: str, fact_md: str) -> str:
    if not re.search(r"허가\s*(?:일|날짜)", question):
        return markdown
    match = re.search(
        r"(?m)^-\s*(?P<subject>.+?)\s+\((?P<date>\d{8})\):[^\n]*?허가일\s+(?P=date)(?:\s|·|\[|$)",
        fact_md,
    )
    if match is None:
        return markdown
    permit_date = match.group("date")
    prose_lines: list[str] = []
    for line in markdown.splitlines():
        if line.strip().startswith("|"):
            break
        prose_lines.append(line)
    if permit_date in "\n".join(prose_lines):
        return markdown
    subject = match.group("subject").strip()
    return _insert_before_first_table(
        markdown,
        f"{subject}의 식약처 허가일은 {permit_date}입니다.",
    )


def _ensure_mfds_clinical_evidence_answer(question: str, markdown: str, fact_md: str) -> str:
    if not re.search(r"임상|허가", question):
        return markdown
    rows = tuple(
        match.groups()
        for match in re.finditer(
            r"(?m)^-\s*(.+?)\s+\((\d{8})\):\s*국내 임상시험\s*=\s*(.+?)\s+"
            r"\[식약처 의약품 정보\]\s*$",
            fact_md,
        )
    )
    missing_rows = tuple(row for row in rows if row[2] not in markdown)
    if not missing_rows:
        return markdown
    highlighted = ", ".join(row[2] for row in missing_rows[:3])
    remainder = " 등" if len(missing_rows) > 3 else ""
    table = "\n".join(
        (
            "**국내 식약처 임상 등록 근거**",
            "",
            "| 질환 | 등록일 | 품목·개발 코드 |",
            "|---|---:|---|",
            *(f"| {subject} | {period} | {item} |" for subject, period, item in missing_rows),
        )
    )
    lead = (
        f"국내 식약처 임상 등록에서는 {highlighted}{remainder}가 확인됩니다. "
        "아래 내용은 등록 품목과 일자를 보여주는 근거이며, 임상 성공이나 현재 개발 단계까지 뜻하지는 않습니다."
    )
    return _insert_before_first_table(markdown, f"{lead}\n\n{table}")


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


_DETERMINISTIC_TOOL_USE_FACT_TOOLS = frozenset(
    {"clinicaltrials_study_details", "mfds_composition"}
)
_CLINICALTRIALS_STUDY_URL_RE = re.compile(
    r"https://clinicaltrials\.gov/study/NCT[0-9A-Za-z]+"
)


def _deterministic_tool_use_external_answer(
    tool_calls: list[dict[str, Any]],
    markdown_response: dict[str, Any],
) -> str:
    successful_tools = {
        str(call.get("tool") or "")
        for call in tool_calls
        if str(call.get("status") or "").casefold() == "ok"
    }
    selected_tools = successful_tools & _DETERMINISTIC_TOOL_USE_FACT_TOOLS
    if not selected_tools:
        return ""

    answer = _deterministic_external_relay_answer(markdown_response)
    if "clinicaltrials_study_details" not in selected_tools or not answer:
        return answer

    url_match = _CLINICALTRIALS_STUDY_URL_RE.search(answer)
    if url_match is None:
        return answer
    disclosure = (
        "선정·제외기준은 현재 연결에서 앞부분 200자까지만 제공됩니다. "
        f"전문은 ClinicalTrials.gov에서 확인하십시오: {url_match.group(0)}"
    )
    if disclosure not in answer:
        answer = f"{answer}\n\n{disclosure}"
    return cleanup_markdown_answer(answer)


def _source_notice_markdown(notice_md: str) -> str:
    return "\n".join(
        line
        for line in notice_md.splitlines()
        if "UBIST" in line
        or "IQVIA" in line
        or is_reimbursement_identity_notice(line)
    ).strip()


def append_source_basis_notice(
    answer: str,
    markdown_response: Mapping[str, Any] | None,
) -> tuple[str, bool]:
    if not isinstance(markdown_response, Mapping):
        return answer, False
    notice = _source_notice_markdown(str(markdown_response.get("notice_md") or ""))
    if not notice or any(character.isdigit() for character in notice):
        return answer, False
    if notice in answer:
        return answer, True
    marker = re.search(r"\n##\s*(?:출처|처리\s*시간)\b", answer)
    if marker:
        before = answer[: marker.start()].rstrip()
        after = answer[marker.start() :].lstrip()
        return cleanup_markdown_answer(f"{before}\n\n{notice}\n\n{after}"), True
    return cleanup_markdown_answer(f"{answer}\n\n{notice}"), True


def prepend_matching_requested_source_basis(
    answer: str,
    question: str,
    tool_calls: list[dict[str, Any]],
) -> str:
    requested_sources = extract_requested_sources(question)
    if len(requested_sources) != 1:
        return answer
    requested_source = requested_sources[0]
    if served_source_from_calls(tool_calls) != requested_source:
        return answer
    notice = source_basis_notice(requested_source)
    if notice is None or notice in answer:
        return answer
    return f"{notice}\n\n{answer}"


def _served_source_from_markdown_response(
    markdown_response: Mapping[str, Any] | None,
) -> str | None:
    if not isinstance(markdown_response, Mapping):
        return None
    sources: list[str] = []
    for line in str(markdown_response.get("sources_md") or "").splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        source = normalize_source(cells[0]) if cells else None
        if source is not None and source not in sources:
            sources.append(source)
    return sources[0] if len(sources) == 1 else None


def _served_source_from_answer(answer: str) -> str | None:
    in_sources = False
    sources: list[str] = []
    for line in answer.splitlines():
        stripped = line.strip()
        if stripped == "## 출처":
            in_sources = True
            continue
        if in_sources and stripped.startswith("## "):
            break
        if not in_sources or not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        source = normalize_source(cells[0]) if cells else None
        if source is not None and source not in sources:
            sources.append(source)
    return sources[0] if len(sources) == 1 else None


def prepend_matching_requested_source_basis_from_result(
    answer: str,
    question: str,
    agent_result: Mapping[str, Any],
    tool_calls: list[dict[str, Any]],
) -> str:
    requested_sources = extract_requested_sources(question)
    if len(requested_sources) != 1:
        return answer
    metrics = agent_result.get("agent_loop_metrics")
    served_source = (
        normalize_source(metrics.get("served_source"))
        if isinstance(metrics, Mapping)
        else None
    )
    served_source = served_source or served_source_from_calls(tool_calls)
    if served_source is None:
        raw_sources = agent_result.get("sources")
        normalized_sources = tuple(
            dict.fromkeys(
                source
                for value in raw_sources
                if (source := normalize_source(value)) is not None
            )
        ) if isinstance(raw_sources, (list, tuple)) else ()
        served_source = normalized_sources[0] if len(normalized_sources) == 1 else None
    served_source = served_source or _served_source_from_markdown_response(
        agent_result.get("markdown_response")
    )
    served_source = served_source or _served_source_from_answer(answer)
    requested_source = requested_sources[0]
    if served_source != requested_source:
        return answer
    notice = source_basis_notice(requested_source)
    if notice is None or notice in answer:
        return answer
    marker = re.search(r"\n##\s*(?:출처|처리\s*시간)\b", answer)
    if marker:
        before = answer[: marker.start()].rstrip()
        after = answer[marker.start() :].lstrip()
        return cleanup_markdown_answer(f"{before}\n\n{notice}\n\n{after}")
    return cleanup_markdown_answer(f"{notice}\n\n{answer}")


def _uploaded_file_context(agent_result: dict[str, Any]) -> str:
    value = agent_result.get("file_context")
    return value.strip() if isinstance(value, str) else ""


def _warn_dropped_file_tokens(question: str, raw_interpretation: str, final_answer: str, file_context: str) -> None:
    """원시 LLM 답변에 있던 파일 유래 토큰이 최종 답변에서 사라지면 경고만 남긴다(차단 아님)."""
    if not file_context.strip():
        return
    file_tokens = set(uploaded_file_fact_tokens(file_context))
    if not file_tokens:
        return
    raw_file_tokens = {
        token
        for token in markdown_allowed_numbers(raw_interpretation)
        if fact_token_allowed(token, file_tokens)
    }
    final_tokens = set(markdown_allowed_numbers(final_answer))
    dropped = sorted(token for token in raw_file_tokens if not fact_token_allowed(token, final_tokens) and token not in final_answer)
    if dropped:
        LOGGER.warning(
            "file-grounded tokens dropped from final answer question=%r dropped=%s",
            question[:120],
            dropped,
        )


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


def append_deferred_prescription_notice(answer: str, result: Mapping[str, Any] | None) -> str:
    """Re-attach the prescription stop the orchestrator already decided on.

    The stop is written onto ``result["answer"]``, but a delivered body is built
    from the fact set — a deterministic market answer, or the generated
    expression — and never from that field. A request that asked for sales and
    prescription therefore served its sales and dropped the refusal on the way
    out. Re-attaching from the typed ``prescription_metric_deferred`` record
    rather than from element counts keeps the wording identical to the wording a
    prescription-only request gets.
    """
    if not isinstance(result, Mapping) or not isinstance(result.get("prescription_metric_deferred"), Mapping):
        return answer
    if PRESCRIPTION_METRIC_UNAVAILABLE_REASON in answer:
        return answer
    separator = "\n\n" if answer.strip() else ""
    return f"{answer}{separator}- {PRESCRIPTION_METRIC_UNAVAILABLE_REASON}"


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


def _append_web_search_section(
    answer: str,
    tool_calls: list[dict[str, Any]] | None,
    *,
    question: str | None = None,
) -> str:
    section = _web_search_unverified_section(tool_calls, question=question)
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


def _web_search_unverified_section(
    tool_calls: list[dict[str, Any]] | None,
    *,
    question: str | None = None,
) -> str:
    return web_search_mi_section_from_calls(tool_calls or (), question=question)


def _has_web_search_rows(
    tool_calls: list[dict[str, Any]] | None,
    *,
    question: str | None = None,
) -> bool:
    return bool(_web_search_unverified_section(tool_calls, question=question))


def _is_web_search_only(tool_calls: list[dict[str, Any]] | None) -> bool:
    calls = [call for call in tool_calls or [] if isinstance(call, dict)]
    if not calls:
        return False
    for call in calls:
        if str(call.get("tool") or "") != "web_search" and str(call.get("source") or "") != "web_search":
            return False
    return True


def _uses_deterministic_market_narrative(
    tool_calls: list[dict[str, Any]] | None,
    narrative: str,
    file_context: str,
) -> bool:
    if not narrative or file_context:
        return False
    tools = {str(call.get("tool") or "") for call in tool_calls or [] if isinstance(call, dict)}
    return bool(tools) and tools <= _DETERMINISTIC_MARKET_NARRATIVE_TOOLS


def _is_tool_use_agent_result(agent_result: dict[str, Any]) -> bool:
    diagnostics = agent_result.get("router_diagnostics")
    return isinstance(diagnostics, dict) and diagnostics.get("mode") == "tool_use_agent"


def _has_combined_clinical_registry_evidence(
    tool_calls: list[dict[str, Any]],
    markdown_response: dict[str, Any],
) -> bool:
    tools = {str(call.get("tool") or "") for call in tool_calls}
    if not {"clinicaltrials_v2_search", "mfds_clinical_trial_kr"}.issubset(tools):
        return False
    fact_md = str(markdown_response.get("fact_md") or markdown_response.get("data_md") or "")
    return "글로벌 임상시험 = NCT" in fact_md and "국내 임상시험 =" in fact_md


def _deterministic_concentration_answer(
    question: str,
    tool_calls: list[dict[str, Any]],
) -> str:
    if not any(token in question for token in ("집중도", "HHI", "CR")):
        return ""
    answer = enforce_market_answer_contract(question, "", tool_calls)
    if "HHI " not in answer or "CR5 " not in answer:
        return ""
    return answer


def _verified_tool_use_agent_answer(agent_result: dict[str, Any]) -> str:
    answer = str(agent_result.get("answer") or "확인 가능한 근거가 없어 답변할 수 없습니다.")
    markdown_response = agent_result.get("markdown_response")
    if not isinstance(markdown_response, dict):
        return FAIL_CLOSED_TEXT
    fact_md = str(markdown_response.get("fact_md") or markdown_response.get("data_md") or "")
    allowed = tuple(str(value) for value in markdown_response.get("allowed_numbers", ()) if value is not None)
    strict_numbers = strict_allowed_numbers(fact_md, allowed)
    if not answer_has_only_fact_numbers(answer, strict_numbers):
        return FAIL_CLOSED_TEXT
    return cleanup_markdown_answer(answer)


def _without_web_fact_context(
    markdown_response: dict[str, Any],
    *,
    calls: list[dict[str, Any]],
    brand: str,
    sources: list[str],
) -> dict[str, Any]:
    web_calls = [call for call in calls if is_web_search_call(call)]
    if not web_calls:
        return markdown_response
    if not any(_web_call_has_result_items(call) for call in web_calls):
        return markdown_response
    fact_calls = [call for call in calls if not is_web_search_call(call)]
    fact_sources = [source for source in sources if source.strip().lower() != "web_search"]
    return MarkdownResponseBuilder().build(
        brand=brand,
        calls=fact_calls,
        sources=fact_sources,
    ).to_dict()


def _web_call_has_result_items(call: dict[str, Any]) -> bool:
    data = call.get("render_data")
    if not isinstance(data, dict):
        return False
    direct = data.get("items")
    if isinstance(direct, list) and any(isinstance(item, dict) for item in direct):
        return True
    nested = data.get("calls")
    return isinstance(nested, list) and any(
        _web_call_has_result_items(item)
        for item in nested
        if isinstance(item, dict)
    )


@dataclass(frozen=True, slots=True)
class GenosClient:
    base_url: str = field(default_factory=resolve_final_genos_base_url)
    token: str | None = field(default_factory=resolve_final_genos_token)
    timeout_s: int = field(default_factory=lambda: int(os.environ.get("GENOS_FINAL_TIMEOUT_S", "50")))
    total_budget_s: int = field(default_factory=lambda: int(os.environ.get("GENOS_FINAL_TOTAL_BUDGET_S", "100")))
    research_mode: str = "standard"
    model: str | None = None
    token_usage_calls: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False, compare=False)
    answer_branch_events: list[str] = field(default_factory=list, init=False, repr=False, compare=False)

    @classmethod
    def for_deep_research(cls) -> GenosClient:
        return cls(
            base_url=resolve_deep_genos_base_url(),
            token=resolve_deep_genos_token(),
            timeout_s=int(os.environ.get("GENOS_DEEP_TIMEOUT_S", "180")),
            total_budget_s=int(os.environ.get("GENOS_DEEP_TOTAL_BUDGET_S", "300")),
            research_mode="deep",
            model="gemini-3.1-pro-preview",
        )

    def stream_answer(self, question: str, agent_result: dict[str, Any]) -> Iterator[str]:
        markdown_response = agent_result.get("markdown_response")
        original_markdown_response = markdown_response
        timing = agent_result.get("timing") if isinstance(agent_result.get("timing"), dict) else None
        file_context = _uploaded_file_context(agent_result)
        diagnostics = agent_result.get("router_diagnostics")
        fallback_code = diagnostics.get("fallback_code") if isinstance(diagnostics, dict) else None
        tool_calls = agent_result.get("tool_calls")
        verified_calls = tool_calls if isinstance(tool_calls, list) else []

        def stream_ready(answer: str) -> str:
            cleaned = cleanup_markdown_answer(answer)
            cleaned, _ = append_source_basis_notice(cleaned, markdown_response)
            return prepend_matching_requested_source_basis_from_result(
                cleaned,
                question,
                agent_result,
                verified_calls,
            )
        if isinstance(markdown_response, dict):
            raw_sources = agent_result.get("sources")
            source_names = (
                [str(source) for source in raw_sources if source]
                if isinstance(raw_sources, list | tuple)
                else [str(call.get("source") or "") for call in verified_calls if call.get("source")]
            )
            markdown_response = _without_web_fact_context(
                markdown_response,
                calls=verified_calls,
                brand=str(agent_result.get("brand") or "시장"),
                sources=source_names,
            )
            fact_md = str(markdown_response.get("fact_md") or markdown_response.get("data_md") or "")
            canonical_brand = str(agent_result.get("brand") or "").strip()
            resolution = agent_result.get("resolution")
            if not canonical_brand and isinstance(resolution, dict):
                canonical_brand = str(resolution.get("canonical_brand") or "").strip()
            news_selection = build_news_selection(
                fact_md,
                canonical_brand=canonical_brand,
            )
            markdown_response["_news_selection"] = news_selection
            if (
                isinstance(original_markdown_response, dict)
                and original_markdown_response is not markdown_response
            ):
                original_markdown_response["_news_selection"] = news_selection
        if self.research_mode != "deep" and self.token and fallback_code is None and isinstance(markdown_response, dict):
            fact_md = str(markdown_response.get("fact_md") or markdown_response.get("data_md") or "")
            single_period_sales_answer = deterministic_single_period_sales_answer(
                question,
                fact_md,
                verified_calls,
            )
            if single_period_sales_answer:
                self._record_answer_branch("genos_single_period_sales")
                with stage(
                    timing,
                    "final_deterministic_single_period_sales_path",
                    "verified single-period sales answer rendering",
                ):
                    answer = _apply_final_claim_controls(question, single_period_sales_answer, fact_md)
                    answer = append_deterministic_source_block(answer, fact_md, file_context=file_context)
                    if not file_context:
                        answer = apply_common_unavailable_response(
                            question,
                            answer,
                            markdown_response,
                            tool_calls=verified_calls,
                        )
                        answer = apply_requested_source_trap_gate(question, answer)
                    answer = ensure_file_absence_statement(question, answer, file_context)
                    if not file_context:
                        answer = enforce_market_answer_contract(question, answer, verified_calls)
                yield from chunk_text(stream_ready(answer))
                return
        if _is_tool_use_agent_result(agent_result):
            verified_answer = _verified_tool_use_agent_answer(agent_result)
            if verified_answer == FAIL_CLOSED_TEXT:
                self._record_answer_branch("genos_tool_fail_closed")
                yield from chunk_text(verified_answer)
                return
            if self.token and fallback_code is None and isinstance(markdown_response, dict):
                fact_md = str(markdown_response.get("fact_md") or markdown_response.get("data_md") or "")
                if fact_md.strip():
                    if _has_combined_clinical_registry_evidence(verified_calls, markdown_response):
                        self._record_answer_branch("genos_tool_clinical_registry")
                        yield from chunk_text(
                            cleanup_markdown_answer(
                                finalized_fallback_fact_answer(question, markdown_response)
                            )
                        )
                        return
                    deterministic_external_answer = _deterministic_tool_use_external_answer(
                        verified_calls,
                        markdown_response,
                    )
                    if deterministic_external_answer:
                        self._record_answer_branch("genos_tool_external")
                        yield from chunk_text(stream_ready(deterministic_external_answer))
                        return
                    markdown_answer = self._markdown_answer(
                        question,
                        markdown_response,
                        timing,
                        verified_calls,
                        file_context,
                    )
                    if self.answer_branch_events:
                        branch = self.answer_branch_events[-1]
                        if branch.startswith("genos_markdown_"):
                            self.answer_branch_events[-1] = branch.replace(
                                "genos_markdown_",
                                "genos_tool_markdown_",
                                1,
                            )
                    yield from chunk_text(stream_ready(markdown_answer))
                    return
            fact_md = str(markdown_response.get("fact_md") or markdown_response.get("data_md") or "")
            verified_answer = ensure_natural_fact_lead(question, verified_answer, fact_md)
            self._record_answer_branch("genos_tool_verified_fallback")
            yield from chunk_text(stream_ready(verified_answer))
            return
        if self.token and isinstance(markdown_response, dict):
            if self.research_mode != "deep" and _requires_deterministic_external_relay(verified_calls):
                self._record_answer_branch("genos_external_relay")
                answer = _deterministic_external_relay_answer(markdown_response)
                answer = replace_internal_fact_dump(question, answer, markdown_response)
                yield from chunk_text(stream_ready(answer))
                return
            fact_md = str(markdown_response.get("fact_md") or markdown_response.get("data_md") or "")
            concentration_answer = (
                _deterministic_concentration_answer(question, verified_calls)
                if self.research_mode != "deep"
                else ""
            )
            if concentration_answer:
                self._record_answer_branch("genos_concentration")
                with stage(
                    timing,
                    "final_deterministic_concentration_path",
                    "verified HHI and CR5 answer rendering",
                ):
                    answer = ensure_file_absence_statement(
                        question,
                        concentration_answer,
                        file_context,
                    )
                yield from chunk_text(stream_ready(answer))
                return
            fast_answer = (
                deterministic_top_n_share_answer(question, fact_md, verified_calls)
                if self.research_mode != "deep"
                else ""
            )
            if fast_answer:
                self._record_answer_branch("genos_top_n")
                with stage(timing, "final_deterministic_fast_path", "verified top-N answer rendering"):
                    answer = _apply_final_claim_controls(question, fast_answer, fact_md)
                    answer = append_deterministic_source_block(answer, fact_md, file_context=file_context)
                    if not file_context:
                        answer = apply_common_unavailable_response(
                            question,
                            answer,
                            markdown_response,
                            tool_calls=verified_calls,
                        )
                        answer = apply_requested_source_trap_gate(question, answer)
                    answer = ensure_file_absence_statement(question, answer, file_context)
                    if not file_context:
                        answer = enforce_market_answer_contract(question, answer, verified_calls)
                yield from chunk_text(stream_ready(answer))
                return
            yield from chunk_text(
                stream_ready(
                    self._markdown_answer(
                        question,
                        markdown_response,
                        timing,
                        verified_calls,
                        file_context,
                    )
                )
            )
            return
        if self._is_cache_only(agent_result) or not self.token:
            self._record_answer_branch("genos_cache")
            answer = str(agent_result["answer"])
            if isinstance(markdown_response, dict):
                answer = replace_internal_fact_dump(question, answer, markdown_response)
            yield from chunk_text(stream_ready(answer))
            return
        policy_notice = self._policy_notice_block(agent_result)
        self._record_answer_branch("genos_legacy_llm")
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
                    "get_brand_metric·search_news·csd_activity_trend 같은 도구 이름, query id, 'fact set', 'agent loop' 등 내부 식별자·처리용어는 답변에 쓰지 말라. "
                    "출처 섹션이나 출처 줄은 작성하지 말라. 출처는 시스템이 별도로 붙인다."
                )
                + (f" {_FILE_QUOTE_INSTRUCTION}" if file_context else ""),
            },
            {"role": "user", "content": self._prompt(question, agent_result)},
        ]
        with latency_observation(
            timing,
            "final_generation",
            operation="genos",
            attempt=1,
        ):
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
        news_selection = _news_selection(markdown_response)
        fact_lookup_md = _fact_lookup_markdown(markdown_response)
        if _is_web_search_only(tool_calls) and _has_web_search_rows(tool_calls, question=question):
            self._record_answer_branch("genos_markdown_web_search")
            return _append_web_search_section("", tool_calls, question=question)
        trend_fact_md = single_brand_trend_fact_markdown(fact_lookup_md, tool_calls) if _question_wants_trend_output(question) else ""
        fact_for_safety = "\n\n".join(part for part in (fact_md, trend_fact_md, file_context) if part)
        strict_numbers = strict_allowed_numbers(fact_for_safety, allowed_numbers)
        if file_context:
            strict_numbers = tuple(sorted({*strict_numbers, *uploaded_file_fact_tokens(file_context)}))
        trend_prose_candidate = render_market_narrative(tool_calls or [])
        if trend_prose_candidate and not answer_has_only_fact_numbers(trend_prose_candidate, strict_numbers):
            trend_prose_candidate = ""
        if self.research_mode != "deep" and _uses_deterministic_market_narrative(tool_calls, trend_prose_candidate, file_context):
            self._record_answer_branch("genos_markdown_deterministic_market")
            with stage(timing, "final_deterministic_market_narrative", "verified market narrative"):
                raw_interpretation = str(markdown_response.get("data_md") or fact_md)
        else:
            self._record_answer_branch("genos_markdown_llm")
            messages = (
                self._deep_markdown_messages(question, markdown_response, trend_fact_md, file_context)
                if self.research_mode == "deep"
                else self._markdown_messages(question, markdown_response, trend_fact_md, file_context)
            )
            try:
                with stage(timing, "final_llm_expression", "GenOS markdown generation"):
                    timing_token = _FINAL_GENERATION_TIMING.set(timing)
                    try:
                        raw_interpretation = self._chat_text(messages)
                    finally:
                        _FINAL_GENERATION_TIMING.reset(timing_token)
            except requests.RequestException:
                self._record_answer_branch("genos_markdown_request_fallback")
                fallback = finalized_fallback_fact_answer(question, markdown_response)
                fallback = _apply_final_claim_controls(question, fallback, fact_md)
                fallback = _ensure_mfds_permit_date_answer(question, fallback, fact_md)
                return enforce_news_claim_selection(fallback, news_selection)
        if not raw_interpretation:
            self._record_answer_branch("genos_markdown_empty_fallback")
            fallback = finalized_fallback_fact_answer(question, markdown_response)
            fallback = _apply_final_claim_controls(question, fallback, fact_md)
            fallback = _ensure_mfds_permit_date_answer(question, fallback, fact_md)
            return enforce_news_claim_selection(fallback, news_selection)
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
        answer = replace_internal_fact_dump(question, answer, markdown_response)
        if not file_context:
            answer = _apply_final_claim_controls(question, answer, fact_md)
        answer = ensure_natural_fact_lead(question, answer, fact_md)
        answer = _ensure_mfds_permit_date_answer(question, answer, fact_md)
        answer = _ensure_mfds_clinical_evidence_answer(question, answer, fact_md)
        answer = append_competitor_patent_coverage_block(answer, fact_md)
        answer = _append_blocked_metric_notices(answer, fact_lookup_md)
        if self.research_mode == "deep":
            answer = _append_web_search_section(answer, tool_calls, question=question)
        answer = append_deterministic_source_block(answer, fact_md, file_context=file_context)
        if not file_context:
            answer = apply_common_unavailable_response(question, answer, markdown_response)
            answer = apply_requested_source_trap_gate(question, answer)
        answer = ensure_file_absence_statement(question, answer, file_context)
        _warn_dropped_file_tokens(question, raw_interpretation, answer, file_context)
        final_answer = (
            answer
            if self.research_mode == "deep"
            else _append_web_search_section(answer, tool_calls, question=question)
        )
        final_answer = enforce_news_claim_selection(final_answer, news_selection)
        final_answer, _source_notice_attached = append_source_basis_notice(
            final_answer,
            markdown_response,
        )
        return prepend_matching_requested_source_basis(
            final_answer,
            question,
            tool_calls or [],
        )

    def _record_answer_branch(self, answer_branch: str) -> None:
        if answer_branch not in ANSWER_BRANCHES:
            raise ValueError(f"unregistered answer branch: {answer_branch}")
        self.answer_branch_events.append(answer_branch)

    @staticmethod
    def _markdown_messages(
        question: str,
        markdown_response: dict[str, Any],
        trend_fact_md: str = "",
        file_context: str = "",
    ) -> list[dict[str, str]]:
        fact_md = str(markdown_response.get("fact_md") or markdown_response.get("data_md") or "")
        news_selection = _news_selection(markdown_response)
        source_notice_md = _source_notice_markdown(
            str(markdown_response.get("notice_md") or "")
        )
        selected_fact_md = (
            news_selection_prompt_fact_markdown(fact_md, news_selection)
            if news_selection is not None
            else fact_md
        )
        prompt_fact_md = _prompt_fact_markdown(selected_fact_md)
        mandatory_md = mandatory_fact_block(selected_fact_md)
        uploaded_md = file_context.strip() or "- 없음"
        file_instruction = f" {_FILE_QUOTE_INSTRUCTION}" if file_context.strip() else ""
        overview_instruction = (
            _FILE_OVERVIEW_SYNTHESIS_INSTRUCTION
            if file_context.strip() and _FILE_OVERVIEW_QUESTION_RE.search(question) is not None
            else ""
        )
        return [
            {
                "role": "system",
                "content": (
                    "너는 JW 시장분석 채팅 에이전트다. 제공된 확정 fact만 근거로 답변 전체를 자연스러운 한국어 Markdown으로 작성한다. "
                    "값이나 표를 먼저 나열하지 않는다. 데이터는 사람에게 설명하듯 자연스러운 문단으로 핵심을 한두 문장으로 먼저 말한다. "
                    "그런 다음 확정 fact의 수치·기간·출처를 근거로 붙여 설명한다. 자연어 본문을 추가하되, 기존 표·차트·뉴스·출처를 삭제하거나 축소하지 않는다. "
                    "표·차트·뉴스는 자연어 본문 뒤에 근거 자료로 그대로 유지한다. "
                    "서술을 부드럽게 만들더라도 확정 fact에 없는 수치·사실·원인은 덧붙이지 않는다. "
                    "업로드 파일 컨텍스트가 있으면 내부 mart fact와 구분해 '업로드 파일 기준'이라고 밝히고, 그 내용은 현재 세션 첨부 파일 근거로만 사용한다. "
                    "업로드 파일 컨텍스트에 파일이 여러 개 있으면 각 업로드 파일의 근거를 최소 1개씩 본문에 반영하고 파일명과 함께 파일별로 구분한다. "
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
                    "내부 저장소명이나 내부 식별자를 쓰지 않는다. get_brand_metric·search_news·csd_activity_trend 같은 도구 이름, query id, 'fact set', 'agent loop' 같은 내부 처리용어도 출력에 쓰지 않는다. "
                    "출처 섹션이나 출처 줄은 작성하지 말라. 출처는 시스템이 검증 fact에서 구조화해 맨 뒤에 붙인다. "
                    "확정 fact로 제공된 표·시계열·뉴스는 누락하지 않고, 모든 표와 시계열 제목에는 브랜드명과 기간을 명시한다. 익명 '브랜드 시계열'은 쓰지 않는다. "
                    "같은 지표를 반복하지 않는다. 근거는 본문 분석에 녹이고 출처 표기는 생성하지 않는다. "
                    "숫자, 비율, 순위, 기간, 질병코드는 fact set에 있는 값만 사용하고 새 값을 만들지 않는다. "
                    "검은 별표 같은 장식 기호를 쓰지 말고, 간결한 한국어로 답한다."
                )
                + file_instruction
                + overview_instruction,
            },
            {
                "role": "user",
                "content": (
                    f"질문: {question}\n\n"
                    f"필수 답변 fact (각 행을 본문에 반영):\n{mandatory_md or '- 없음'}\n\n"
                    f"단일 브랜드 추이 산문용 trend fact:\n{trend_fact_md or '- 없음'}\n\n"
                    f"업로드 파일 컨텍스트 (현재 세션 첨부 파일 검색 결과, 내부 mart fact와 별도):\n{uploaded_md}\n\n"
                    f"요청 source 및 측정 기준 안내:\n{source_notice_md or '- 없음'}\n\n"
                    "확정 fact set:\n"
                    f"{prompt_fact_md or '- 없음'}\n\n"
                    "작성 형식: 자연어 문단을 먼저 쓰고, 결론과 질문 의도별 핵심 근거·시사점·한계를 fact 범위 안에서 설명한다. 기존 표·차트·뉴스는 그대로 유지하고 자연어 본문 뒤에 보조 근거로 둔다. "
                    "여러 업로드 파일을 사용하라는 질문이면 한 파일만 요약하지 말고 각 업로드 파일의 근거를 최소 1개씩 파일별로 구분해 답한다. "
                    "질문이 특정 지표값을 직접 물으면 그 지표의 브랜드명·기간·값을 첫 단락에 먼저 쓴다. "
                    "출처 섹션이나 출처 줄은 쓰지 않는다."
                ),
            },
        ]

    @staticmethod
    def _deep_markdown_messages(
        question: str,
        markdown_response: dict[str, Any],
        trend_fact_md: str = "",
        file_context: str = "",
    ) -> list[dict[str, str]]:
        messages = GenosClient._markdown_messages(
            question,
            markdown_response,
            trend_fact_md,
            file_context,
        )
        system = dict(messages[0])
        system["content"] = (
            "명시적으로 요청된 딥리서치 모드다. 일반 답변보다 충분히 상세하게 작성하되, "
            "제공된 확정 fact와 실제 도구·웹 근거 밖의 수치, URL, 기사, 인과, 전망을 만들지 않는다. "
            "업로드 파일과 시장·외부 도구·웹 근거를 함께 종합하고, 업로드 파일이 여러 개면 각 파일의 실제 근거를 빠짐없이 파일명과 함께 구분한다. "
            "서로 다른 출처를 교차 확인하고, 근거가 충돌하거나 비어 있으면 그 한계를 명시한다. "
            "도구별·출처별 섹션 나열을 금지하고, 시장·경쟁 구도와 임상·허가·안전성·환자 맥락을 질문에 대한 하나의 종합 서사로 작성한다. "
            "구조는 핵심 요약 → 종합 분석 → 뒷받침 표 순으로 고정하며, 출처는 시스템이 맨 마지막에 한 번만 붙인다. "
            "핵심 요약은 3~5줄로 결론을 먼저 말하고, 종합 분석에서는 시장 수치와 뉴스·임상·허가 근거를 서로 연결한다. "
            "같은 사실이나 같은 기사를 여러 섹션에서 반복하지 않는다. 내부 미보유 처리 정책이나 단계 표를 노출하지 않는다. "
            "각 본문 섹션은 반드시 '## '로 시작하는 명시적 제목을 붙이고, 최소 두 개의 본문 섹션을 만든다. "
            "표·차트·뉴스·출처 근거는 유지하고, 확인된 근거가 없는 섹션은 억지로 채우지 않는다. "
            + str(system["content"])
        )
        user = dict(messages[1])
        user["content"] = (
            str(user["content"])
            + "\n\n딥리서치 작성 요구: 먼저 결론을 요약하고, 서로 다른 근거가 정합하거나 반대 방향인지 연결해 설명한 뒤 "
            "실무적 시사점과 한계를 정리한다. 근거가 함께 움직여도 '때문이다'라고 단정하지 않는다. "
            "모든 구체 주장은 제공된 fact로 검증 가능해야 하며, 출처별 결과 목록을 반복하지 않는다."
        )
        return [system, user]

    def _chat_text(
        self,
        messages: list[dict[str, str]],
        *,
        timing: dict[str, Any] | None = None,
    ) -> str:
        active_timing = timing if timing is not None else _FINAL_GENERATION_TIMING.get()
        last_error: requests.RequestException | None = None
        deadline = time.monotonic() + max(1, self.total_budget_s)
        token = _FINAL_GENERATION_DEADLINE.set(deadline)
        try:
            for attempt in range(1, generation_attempts() + 1):
                if deadline - time.monotonic() <= 0:
                    break
                try:
                    with latency_observation(
                        active_timing,
                        "final_generation",
                        operation="genos",
                        attempt=attempt,
                    ) as observation:
                        text = "".join(self._stream_chat(messages)).strip()
                        if not text:
                            observation["status"] = "empty"
                except requests.RequestException as exc:
                    last_error = exc
                    continue
                if text:
                    return text
        finally:
            _FINAL_GENERATION_DEADLINE.reset(token)
        if last_error is not None:
            raise last_error
        return ""

    def uploaded_file_brief(self, messages: list[dict[str, str]]) -> str:
        """Generate one batched exploratory brief from observed upload metadata."""

        return self._chat_text(messages)

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
        deadline = _FINAL_GENERATION_DEADLINE.get()
        remaining = deadline - time.monotonic() if deadline is not None else float(self.timeout_s)
        if remaining <= 0:
            raise requests.Timeout("final generation deadline exceeded")
        payload: dict[str, Any] = {
            "messages": messages,
            "stream": True,
            "temperature": 0.0,
            "stream_options": {"include_usage": True},
        }
        if self.model:
            payload["model"] = self.model
        response = requests.post(
            f"{self.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {self.token}"},
            json=payload,
            stream=True,
            timeout=min(float(self.timeout_s), remaining),
        )
        response.raise_for_status()
        usage_call: dict[str, Any] | None = None
        for raw_line in response.iter_lines(decode_unicode=True):
            if deadline is not None and time.monotonic() >= deadline:
                response.close()
                raise requests.Timeout("final generation deadline exceeded")
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
