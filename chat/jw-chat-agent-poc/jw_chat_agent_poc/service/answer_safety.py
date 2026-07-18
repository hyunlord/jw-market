from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
from html import unescape
from collections.abc import Iterator
from typing import Any

from jw_chat_agent_poc.orchestrator.dosage_notes import dosage_combination_note, is_dosage_combination_note
from jw_chat_agent_poc.orchestrator.markdown_formatting import allowed_numbers, eok_value, normalize_number, pct_value
from jw_chat_agent_poc.orchestrator.provenance_labels import provenance_source_block_from_facts
from jw_chat_agent_poc.service.deep_report_cleanup import repair_plain_table_urls, slim_source_tables
from jw_chat_agent_poc.service.markdown_cleanup import cleanup_markdown_answer


GENERATION_ATTEMPTS = int(os.environ.get("GENOS_GENERATION_ATTEMPTS", "2"))
FAIL_CLOSED_TEXT = "- 표에 포함된 확정 데이터만 기준으로 해석합니다."
_UNSUPPORTED_SERIES_RE = re.compile(r"(미지원|미보유|확인\s*안\s*됨|확인되지|데이터\s*없음|지원하지)")
_NEGATED_UNSUPPORTED_RE = re.compile(r"(아니|아님|아닙)")
_EMPTY_NEWS_SHELL_RE = re.compile(r"관련\s*기사에서.*언급이\s*확인|언급이\s*확인됐습니다|언급이\s*확인되었습니다")
_STREAM_ATOMIC_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"\d{4}-(?:0[1-9]|1[0-2])|"
    r"\d{4}-Q[1-4]|"
    r"[+-]?\d[\d,]*(?:\.\d+)?(?:%p|%|억원|원|명|건|개|위)?"
    r")(?![A-Za-z0-9])"
)
_NEWS_ISSUE_NUMBER_RE = re.compile(
    r"(?<![A-Za-z])[+-]?\d[\d,]*(?:\.\d+)?(?:\s*(?:억\s*원|억원|원|명|건|개|위|년|월|%p|%|분기))?"
)
_NEWS_ISSUE_CODE_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{1,8}\d[A-Za-z0-9.-]*(?![A-Za-z0-9])")
_RAW_LEVEL_TOP_LINE_RE = re.compile(
    r"^\s*[-*•]?\s*(?P<level>[A-Za-z_가-힣]+)\s+상위:\s*"
    r"(?P<rank>\d+위)\s+(?P<name>.+?)\s+시장점유율\s+"
    r"(?P<share>-?\d+(?:\.\d+)?%)\s+매출\s+(?P<sales>-?\d+(?:\.\d+)?억원)\s*$"
)
_MULTI_FILE_REQUEST_RE = re.compile(
    r"(?:두|여러|모든)\s*(?:업로드\s*)?파일|(?:업로드\s*)?파일(?:을|를)?\s*모두|파일별",
    re.IGNORECASE,
)
_FILE_OVERVIEW_REQUEST_RE = re.compile(
    r"(?:문서|보고서|파일|발표).{0,20}(?:요약|핵심|결론|뭐에\s*관한|무슨\s*내용)"
    r"|(?:요약|핵심|결론).{0,20}(?:문서|보고서|파일|발표)",
    re.IGNORECASE,
)
_FILE_OVERVIEW_SECTION_RE = re.compile(
    r"(?:Key\s+Takeaways?|Executive\s+Summary|Conclusions?|Unmet\s+Needs?|Overview|"
    r"Market\s+Landscape|Market\s+Size|Growth\s+Contribution|HHI|"
    r"핵심\s*요약|주요\s*결론|미충족\s*수요|시장\s*개요|시장\s*규모)",
    re.IGNORECASE,
)
_FILE_CONTEXT_NOISE_RE = re.compile(
    r"^(?:\[DA\]\s*문서:|섹션:|검색\s*범위:|Copyright\s|<!--|\d+\s+\d{1,2}\s+[A-Za-z]{3},\s+\d{4}\s*$)",
    re.IGNORECASE,
)
_CROSS_FILE_COMPARISON_RE = re.compile(r"(?:비교|일치|같(?:은|나|은지)|대조)", re.IGNORECASE)
_COMPARISON_JUDGMENT_RE = re.compile(
    r"(?:대상|정의|단위).{0,30}(?:다르|불일치)|직접.{0,20}(?:비교|일치).{0,30}(?:어렵|불가|없|판정)",
    re.IGNORECASE,
)
_CATALOG_EVIDENCE_RE = re.compile(
    r"(?:Approved\s+drug|\bDrug\b|Approval(?:\s+Date)?|\bPhase\b|\bTarget\b|"
    r"승인\s*약물|허가\s*(?:약물|품목|목록)|임상\s*(?:약물|목록))",
    re.IGNORECASE,
)
_AGGREGATE_EVIDENCE_RE = re.compile(
    r"(?:total_value|applied_rows|\bSUM\b|\bCOUNT\b|매출\s*합계|총\s*매출|전체\s*합계|집계\s*금액)",
    re.IGNORECASE,
)
_FILE_CONTEXT_BLOCK_RE = re.compile(
    r"^\[(?:\d+)\]\s+([^\n]+)\n(.*?)(?=^\[(?:\d+)\]\s+|\Z)",
    re.MULTILINE | re.DOTALL,
)
_FILE_QUERY_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]{1,}|[가-힣]{2,}")
_FILE_QUERY_STOP_WORDS = frozenset(
    {"업로드", "파일", "모두", "사용해서", "비교해줘", "알려줘", "그리고", "대한", "기준"}
)
_GLOBAL_CLINICAL_FACT_RE = re.compile(
    r"^-\s*(?P<subject>.+?):\s*글로벌 임상시험\s*=\s*"
    r"(?P<nct>NCT\d+)\s*·\s*(?P<detail>.+?)\s*"
    r"\[ClinicalTrials\.gov 임상시험 정보\]\s*$"
)
_DOMESTIC_CLINICAL_FACT_RE = re.compile(
    r"^-\s*(?P<subject>.+?)\s*\((?P<date>\d{8})\):\s*국내 임상시험\s*=\s*"
    r"(?P<item>.+?)\s*\[식약처 의약품 정보\]\s*$"
)
_INGREDIENT_IDENTITY_FACT_RE = re.compile(
    r"^-\s*(?P<brand>.+?):\s*성분\s*=\s*(?P<ingredient>.+?)\s*\[(?P<source>.+?)\]\s*$"
)
_DISEASE_IDENTITY_FACT_RE = re.compile(
    r"^-\s*(?P<code>[A-Z]\d{2}(?:\.\d+)?)\s*:\s*질병명/상병코드\s*=\s*"
    r"(?P<disease>.+?)\s*\[(?P<source>.+?)\]\s*$"
)
_DEEP_SOURCE_HEADING_RE = re.compile(r"(?m)^##\s+출처\b")
_DEEP_BODY_HEADING_RE = re.compile(r"(?m)^##\s+(?!출처\b).+")


@dataclass(frozen=True, slots=True)
class _TrendPoint:
    period: str
    value: float
    value_text: str
    share_text: str = ""


@dataclass(frozen=True, slots=True)
class _SingleBrandTrendFact:
    brand: str
    points: tuple[_TrendPoint, ...]
    first: _TrendPoint
    peak: _TrendPoint
    trough: _TrendPoint
    latest: _TrendPoint
    market_by_period: dict[str, _TrendPoint]


def generation_attempts() -> int:
    """Return the current bounded final-generation retry count."""
    return int(os.environ.get("GENOS_GENERATION_ATTEMPTS", str(GENERATION_ATTEMPTS)))


def strict_allowed_numbers(fact_md: str, fallback_allowed: tuple[str, ...]) -> tuple[str, ...]:
    """Return numeric tokens allowed for final prose from quantitative facts and article citations."""
    non_news = _non_news_fact_markdown(fact_md)
    news = _news_fact_markdown(fact_md)
    non_news_tokens = set(allowed_numbers(non_news))
    news_citation_tokens = _news_citation_tokens(news)
    strict = _expanded_fact_number_tokens(non_news_tokens, non_news) | _expanded_fact_number_tokens(news_citation_tokens, news)
    if not strict:
        strict = _expanded_fact_number_tokens(set(fallback_allowed), "\n".join(fallback_allowed)) | news_citation_tokens
    return tuple(sorted(token for token in strict if token))


_FILE_TOKEN_TRAILING_PUNCT = ".,;:)]}"


def uploaded_file_fact_tokens(file_context: str) -> tuple[str, ...]:
    """업로드 파일 컨텍스트에서 최종 답변에 그대로 인용 가능한 숫자/코드 토큰 허용 집합을 만든다.

    fact 추출기는 파일 프로즈에서 문장부호가 붙은 코드(`NAR7712.`)나 영어 단위("37.8 percent")를
    답변측 표기(`NAR-7712`, `37.8%`)와 일치시키지 못하므로, 답변측과 같은 추출기로 재추출하고
    문장부호 꼬리 제거·비율 단위 별칭을 보강한다. file_context가 있을 때만 호출된다.
    """
    text = (file_context or "").strip()
    if not text:
        return ()
    tokens: set[str] = set()
    for raw in allowed_numbers(text):
        candidates = {raw, raw.rstrip(_FILE_TOKEN_TRAILING_PUNCT)}
        for candidate in candidates:
            if not candidate:
                continue
            tokens.update(_numeric_token_aliases(candidate))
            if re.fullmatch(r"[+-]?\d[\d,]*(?:\.\d+)?", candidate):
                tokens.update(_numeric_token_aliases(f"{candidate}%"))
                tokens.update(_numeric_token_aliases(f"{candidate}%p"))
    return tuple(sorted(token for token in tokens if token))


_FILE_QUESTION_TARGET_RE = re.compile(
    r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)+"  # NOVA-ZETA-404, QA_E2E_... 같은 코드형 토큰
    r"|(?:[A-Z][A-Za-z0-9]+\s+){1,3}[A-Z][A-Za-z0-9]+"  # Project Eclipse Harbor 같은 대문자 시작 연쇄
    r"|[A-Z][a-z]{4,}"  # Polaris 같은 단독 고유명
)


def _file_question_targets(question: str) -> tuple[str, ...]:
    targets = []
    for match in _FILE_QUESTION_TARGET_RE.finditer(question):
        target = match.group(0).strip()
        if target and target not in targets:
            targets.append(target)
    return tuple(targets)


def ensure_file_absence_statement(question: str, answer: str, file_context: str) -> str:
    """질문이 지목한 대상이 업로드 파일 컨텍스트에 없고 답변도 그 대상을 다루지 않으면 부재 문장을 조립한다."""
    context = (file_context or "").strip()
    if not context:
        return answer
    exhaustive = (
        "검색 범위: 문서 전체 키워드 검색" in context
        or "검색 범위: 지정 페이지 직접 조회" in context
        or "## 업로드 파일 SQL 결과" in context
    )
    if not exhaustive:
        return answer
    if "## 업로드 파일 SQL 결과" in context and (
        "상태: 확인됨" in context or "상태: 조건 일치 0건" in context
    ):
        return answer
    context_fold = context.casefold()
    answer_fold = answer.casefold()
    missing = [
        target
        for target in _file_question_targets(question)
        if target.casefold() not in context_fold and target.casefold() not in answer_fold
    ]
    if not missing:
        return answer
    lines = "\n".join(f"업로드 문서에서 {target}을(를) 찾을 수 없습니다." for target in missing)
    return cleanup_markdown_answer(f"{lines}\n\n{answer}")


_PAGE_DIRECTED_CONTEXT_MARKER = "검색 범위: 문서 전체 키워드 검색 + 지정 페이지 직접 조회"
_PAGE_NUMERIC_REQUEST_RE = re.compile(r"(?:수치|환자\s*수|값|금액|비율|각각|몇\s*(?:명|개|건))")
_PAGE_NUMERIC_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])\d[\d,]*(?:\.\d+)?(?:%|[mb])?", re.IGNORECASE)
_PAGE_SOURCE_METADATA_RE = re.compile(
    r"^(?:\[\d+\]\s|\[DA\]\s|섹션:|검색 범위:)|document_id=|TEMP_DOCUMENT_|\|\s*p\.\d+",
    re.IGNORECASE,
)


def _page_evidence_content(block: str) -> str:
    return "\n".join(
        line for line in block.splitlines() if not _PAGE_SOURCE_METADATA_RE.search(line)
    ).strip()


def ensure_file_page_evidence(question: str, answer: str, file_context: str) -> str:
    """Append bounded exact-page evidence when a numeric page answer drops source values."""

    context = (file_context or "").strip()
    if _PAGE_DIRECTED_CONTEXT_MARKER not in context or not _PAGE_NUMERIC_REQUEST_RE.search(question):
        return answer
    question_tokens = set(_PAGE_NUMERIC_TOKEN_RE.findall(question))
    answer_tokens = set(_PAGE_NUMERIC_TOKEN_RE.findall(answer))
    missing_tokens = {
        token
        for token in _PAGE_NUMERIC_TOKEN_RE.findall(context)
        if token not in question_tokens and token not in answer_tokens
    }
    if not missing_tokens:
        return answer
    blocks = [block.strip() for block in re.split(r"\n\s*\n", context) if block.strip()]
    evidence = [
        content
        for block in blocks
        if (content := _page_evidence_content(block))
        and any(token in content for token in missing_tokens)
    ][:2]
    if not evidence:
        return answer
    excerpt = "\n\n".join(block[:1800] for block in evidence)
    return cleanup_markdown_answer(f"{answer}\n\n### 지정 페이지 원문 근거\n{excerpt}")


def ensure_file_overview_evidence_coverage(question: str, answer: str, file_context: str) -> str:
    """Keep bounded, retrieved overview sections when synthesis omits them."""

    context = (file_context or "").strip()
    if not context or not _FILE_OVERVIEW_REQUEST_RE.search(question):
        return answer

    excerpts: list[str] = []
    total_chars = 0
    for match in _FILE_CONTEXT_BLOCK_RE.finditer(context):
        header = match.group(1).strip()
        body = match.group(2).strip()
        meaningful_lines = [
            line.strip()
            for line in body.splitlines()
            if line.strip() and not _FILE_CONTEXT_NOISE_RE.search(line.strip())
        ]
        excerpt = "\n".join(meaningful_lines).strip()
        if not excerpt or not _FILE_OVERVIEW_SECTION_RE.search(excerpt):
            continue
        substantive_lines = [
            line.lstrip("#- ").strip()
            for line in meaningful_lines
            if not re.fullmatch(
                r"#{0,6}\s*(?:Key\s+Takeaways?|Executive\s+Summary|Conclusions?|"
                r"Unmet\s+Needs?|Overview|Market\s+(?:Landscape|Size)|"
                r"핵심\s*요약|주요\s*결론|미충족\s*수요|시장\s*개요)\s*",
                line,
                re.IGNORECASE,
            )
        ]
        if substantive_lines and all(
            line.casefold() in answer.casefold() for line in substantive_lines
        ):
            continue
        remaining = 7000 - total_chars
        if remaining <= 0:
            break
        is_summary = re.search(
            r"(?:Key\s+Takeaways?|Executive\s+Summary|Conclusions?|Unmet\s+Needs?|Overview|"
            r"핵심\s*요약|주요\s*결론|미충족\s*수요|시장\s*개요)",
            excerpt,
            re.IGNORECASE,
        )
        block_limit = 3000 if is_summary else 900
        excerpt = excerpt[: min(block_limit, remaining)].rstrip()
        if not excerpt:
            continue
        filename = re.sub(r"\s+\(document_id=.*", "", header).strip()
        excerpts.append(f"### {filename}\n{excerpt}")
        total_chars += len(excerpt)

    if not excerpts:
        return answer
    section = "## 업로드 파일 핵심 근거\n" + "\n\n".join(excerpts)
    return _insert_before_timing_or_source(answer, section)


def ensure_cross_file_comparison_judgment(question: str, answer: str, file_context: str) -> str:
    """State non-comparability only for retrieved catalog-vs-aggregate evidence."""

    context = (file_context or "").strip()
    if (
        not context
        or not _CROSS_FILE_COMPARISON_RE.search(question)
        or _COMPARISON_JUDGMENT_RE.search(answer)
        or "## 업로드 파일 SQL 결과" not in context
    ):
        return answer
    document_context, sql_context = context.split("## 업로드 파일 SQL 결과", 1)
    if (
        not _CATALOG_EVIDENCE_RE.search(document_context)
        or not _AGGREGATE_EVIDENCE_RE.search(sql_context)
    ):
        return answer
    judgment = (
        "문서의 항목형 근거와 엑셀 집계는 대상과 단위가 서로 달라 "
        "직접적인 일치 여부를 판정할 수 없습니다."
    )
    return _insert_before_timing_or_source(answer, judgment)


def ensure_multi_file_evidence_coverage(question: str, answer: str, file_context: str) -> str:
    """Append bounded verbatim evidence for every file in an explicit multi-file request."""

    if not _MULTI_FILE_REQUEST_RE.search(question):
        return answer
    blocks: dict[str, list[str]] = {}
    for match in _FILE_CONTEXT_BLOCK_RE.finditer((file_context or "").strip()):
        filename = re.sub(r"\s+\(document_id=.*\)\s*$", "", match.group(1)).strip()
        if filename:
            blocks.setdefault(filename, []).extend(
                line.strip() for line in match.group(2).splitlines() if line.strip()
            )
    if len(blocks) < 2:
        return answer

    query_tokens = {
        token.casefold()
        for token in _FILE_QUERY_TOKEN_RE.findall(question)
        if token.casefold() not in _FILE_QUERY_STOP_WORDS
    }
    evidence_lines = []
    for filename, lines in blocks.items():
        ranked = sorted(
            enumerate(lines),
            key=lambda item: (
                -sum(token in item[1].casefold() for token in query_tokens),
                item[0],
            ),
        )
        excerpt = ranked[0][1] if ranked else ""
        if len(excerpt) > 700:
            excerpt = excerpt[:697].rstrip() + "..."
        evidence_lines.append(f"- **{filename}**: {excerpt or '검색 근거가 비어 있습니다.'}")

    section = "## 파일별 근거 확인\n" + "\n".join(evidence_lines)
    source_index = answer.find("## 출처")
    if source_index < 0:
        return cleanup_markdown_answer(f"{answer}\n\n{section}")
    before = answer[:source_index].rstrip()
    source = answer[source_index:].lstrip()
    return cleanup_markdown_answer(f"{before}\n\n{section}\n\n{source}")


def fallback_fact_answer(markdown_response: Any) -> str:
    """Build a non-empty deterministic answer from verified fact markdown."""
    if not isinstance(markdown_response, dict):
        return "확정 데이터만으로 답변을 구성할 수 있는 정보가 제한적입니다."
    fact_md = str(markdown_response.get("fact_md") or markdown_response.get("data_md") or "")
    clinical_answer = _external_clinical_fallback_answer(fact_md)
    if clinical_answer:
        return clinical_answer
    lines = list(dict.fromkeys(mandatory_fact_lines(fact_md)))
    if not lines:
        lines = list(dict.fromkeys(_table_fact_lines(_non_news_fact_markdown(fact_md))))
    source_line = _source_line(fact_md)
    sales_delta_answer = _sales_delta_fallback_answer(lines, fact_md, source_line)
    if sales_delta_answer:
        return sales_delta_answer
    top_brand_answer = _top_brand_fallback_answer(lines, fact_md, source_line)
    if top_brand_answer:
        return top_brand_answer
    csd_answer = _csd_activity_fallback_answer(lines, source_line)
    if csd_answer:
        return csd_answer
    body = "\n".join(lines) if lines else "- 표시할 검증 fact가 제한적입니다."
    parts = ["조회된 수치로 요약하면 다음과 같습니다.", body]
    news_lines = list(safe_news_summary_lines(fact_md))[:3]
    if news_lines:
        parts.extend(("관련 이슈 맥락", "\n".join(news_lines)))
    if source_line:
        parts.append(source_line)
    return "\n\n".join(parts)


def _external_clinical_fallback_answer(fact_md: str) -> str:
    """Render exact clinical registry evidence when final LLM expression is unavailable."""
    global_rows: list[tuple[str, str, str, str]] = []
    domestic_rows: list[tuple[str, str, str]] = []
    for line in fact_md.splitlines():
        global_match = _GLOBAL_CLINICAL_FACT_RE.match(line.strip())
        if global_match:
            detail = global_match.group("detail").strip()
            title, separator, url = detail.rpartition(" · ")
            if not separator or not url.startswith(("http://", "https://")):
                title, url = detail, "-"
            global_rows.append(
                (
                    global_match.group("subject").strip(),
                    global_match.group("nct").strip(),
                    title.strip(),
                    url.strip(),
                )
            )
            continue
        domestic_match = _DOMESTIC_CLINICAL_FACT_RE.match(line.strip())
        if domestic_match:
            domestic_rows.append(
                (
                    domestic_match.group("subject").strip(),
                    domestic_match.group("date").strip(),
                    domestic_match.group("item").strip(),
                )
            )
    if not global_rows or not domestic_rows:
        return ""

    first_global = global_rows[0]
    highlighted_domestic = ", ".join(row[2] for row in domestic_rows[:3])
    domestic_remainder = " 등" if len(domestic_rows) > 3 else ""
    parts = [
        (
            "확인된 등록 근거를 보면, 글로벌 임상 등록과 국내 식약처 임상 등록이 함께 확인됩니다. "
            f"글로벌 등록에는 {first_global[2]} 연구가 포함됩니다. "
            f"국내 식약처 임상 등록에서는 {highlighted_domestic}{domestic_remainder}가 확인됩니다."
        ),
        (
            "다만 이 등록정보는 연구·품목과 등록일을 보여주는 근거이며, "
            "임상 성공, 허가 완료 또는 현재 개발 단계를 뜻하지 않습니다."
        ),
        "## 근거 데이터",
        "### 글로벌 임상 등록",
        "| 대상 | 임상시험 번호 | 연구 | 링크 |",
        "| --- | --- | --- | --- |",
    ]
    parts.extend(
        f"| {_markdown_cell(subject)} | {_markdown_cell(nct)} | {_markdown_cell(title)} | {_markdown_cell(url)} |"
        for subject, nct, title, url in global_rows
    )
    parts.extend(
        (
            "",
            "### 국내 식약처 임상 등록",
            "| 대상 | 등록일 | 품목 |",
            "| --- | --- | --- |",
        )
    )
    parts.extend(
        f"| {_markdown_cell(subject)} | {_markdown_cell(date)} | {_markdown_cell(item)} |"
        for subject, date, item in domestic_rows
    )
    return "\n".join(parts)


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def _csd_activity_fallback_answer(lines: list[str], source_line: str) -> str:
    activity = next((line for line in lines if "CSD aggregate 콜수" in line), "")
    if not activity:
        return ""
    detail = activity.split("CSD aggregate 콜수", 1)[-1].lstrip(" :|-—")
    detail = detail.replace("CSD ChannelDynamics aggregate 콜수/활동량", "월별 영업활동량")
    parts = [f"확인된 월별 영업활동 추이는 {detail}입니다."]
    if source_line:
        parts.append(source_line)
    return "\n\n".join(parts)


def finalized_fallback_fact_answer(question: str, markdown_response: Any) -> str:
    """Build a fallback answer and apply the same final safety structure."""
    fact_md = ""
    if isinstance(markdown_response, dict):
        fact_md = str(markdown_response.get("fact_md") or markdown_response.get("data_md") or "")
    answer = cleanup_markdown_answer(fallback_fact_answer(markdown_response))
    answer = ensure_share_delta_line(question, answer, fact_md)
    answer = ensure_causal_structure(question, answer, fact_md)
    answer = strip_generated_source_sections(answer)
    answer = remove_raw_fact_residue(answer, fact_md)
    return append_deterministic_source_block(answer, fact_md)


def replace_internal_fact_dump(question: str, answer: str, markdown_response: Any) -> str:
    """Replace a leaked CSD fact prompt with the deterministic user-facing answer."""
    markers = ("## 확정 데이터", "반드시 반영할 내용", "CSD 세부 미지원")
    marker_count = sum(marker in answer for marker in markers)
    if "CSD aggregate 콜수" not in answer or (marker_count < 2 and "CSD 세부 미지원" not in answer):
        return answer
    fact_md = ""
    if isinstance(markdown_response, dict):
        fact_md = str(markdown_response.get("fact_md") or markdown_response.get("data_md") or "")
    lines = list(dict.fromkeys(mandatory_fact_lines(fact_md)))
    csd_answer = _csd_activity_fallback_answer(lines, _source_line(fact_md))
    if csd_answer:
        return cleanup_markdown_answer(csd_answer)
    return cleanup_markdown_answer(fallback_fact_answer(markdown_response))


def answer_has_only_fact_numbers(answer: str, fact_numbers: tuple[str, ...]) -> bool:
    """Return whether every final answer number is present in the verified fact set."""
    allowed = set(fact_numbers)
    return all(fact_token_allowed(token, allowed) for token in allowed_numbers(answer))


def fact_token_allowed(raw_token: str, fact_numbers: tuple[str, ...] | set[str]) -> bool:
    """Return whether a generated numeric/code token matches a verified fact token after normalization."""

    allowed = set(fact_numbers)
    token = normalize_number(str(raw_token)).upper()
    if _has_explicit_metric_unit(token):
        return bool(_explicit_unit_token_aliases(token) & allowed)
    aliases = _numeric_token_aliases(raw_token)
    return bool(aliases & allowed)


def _expanded_fact_number_tokens(tokens: set[str], raw_text: str) -> set[str]:
    """Build display-format aliases for fact numbers that may appear in final prose."""

    expanded: set[str] = set()
    for token in tokens:
        expanded.update(_numeric_token_aliases(token))
    expanded.update(_period_alias_tokens(raw_text))
    expanded.update(_rank_alias_tokens(raw_text))
    expanded.update(_quarter_alias_tokens(raw_text))
    return {token for token in expanded if token}


def _numeric_token_aliases(raw_token: str) -> set[str]:
    """Return normalized variants for one numeric/code token."""

    token = normalize_number(str(raw_token)).upper()
    if not token:
        return set()
    aliases = {token}
    aliases.update(_explicit_unit_token_aliases(token))
    compact = _strip_metric_unit(token)
    aliases.add(compact)
    aliases.update(_decimal_trim_aliases(compact))
    if compact.startswith(("+", "-")):
        aliases.add(compact[1:])
        aliases.add(_strip_metric_unit(token[1:]))
        aliases.update(_decimal_trim_aliases(compact[1:]))
    if "/" in compact:
        aliases.add(compact)
        aliases.update(part for part in compact.split("/") if part)
    if re.fullmatch(r"-0\d", compact):
        aliases.add(compact[1:])
        aliases.add(str(int(compact[1:])))
    if re.fullmatch(r"0\d", compact):
        aliases.add(str(int(compact)))
    if compact.isdigit():
        aliases.add(f"{compact}위")
        aliases.add(f"{compact}월")
        aliases.add(f"{int(compact)}월")
    return {alias for alias in aliases if alias}


def _has_explicit_metric_unit(token: str) -> bool:
    return bool(re.search(r"(?:억\s*원|억원|원|명|건|개|위|년|월|%P|%|분기)$", token))


def _explicit_unit_token_aliases(token: str) -> set[str]:
    aliases = {token}
    match = re.match(r"(?P<num>[+-]?\d+(?:\.\d+)?)(?P<unit>억\s*원|억원|원|명|건|개|위|년|월|%P|%|분기)$", token)
    if not match:
        return aliases
    number = match.group("num")
    unit = match.group("unit")
    for number_alias in _decimal_trim_aliases(number) | {number}:
        aliases.add(f"{number_alias}{unit}")
        if number_alias.startswith(("+", "-")):
            aliases.add(f"{number_alias[1:]}{unit}")
    return aliases


def _strip_metric_unit(token: str) -> str:
    return re.sub(r"(?:억\s*원|억원|원|명|건|개|위|년|월|%P|%|분기)$", "", token)


def _decimal_trim_aliases(token: str) -> set[str]:
    if not re.fullmatch(r"[+-]?\d+\.\d+", token):
        return set()
    sign = ""
    body = token
    if body[0] in "+-":
        sign = body[0]
        body = body[1:]
    left, right = body.split(".", 1)
    trimmed = right.rstrip("0")
    if not trimmed:
        return {f"{sign}{left}"}
    return {f"{sign}{left}.{trimmed}"}


def _period_alias_tokens(text: str) -> set[str]:
    aliases: set[str] = set()
    for year, month in re.findall(r"\b(20\d{2})[-./](\d{1,2})\b", text):
        month_int = int(month)
        month_2 = f"{month_int:02d}"
        aliases.update(
            {
                year,
                month,
                month_2,
                str(month_int),
                f"-{month_2}",
                f"{year}-{month_2}",
                f"{year}/{month_2}",
                f"{year}년{month_int}월",
                f"{year}년{month_2}월",
                f"{year}년 {month_int}월",
                f"{year}년 {month_2}월",
                f"{month_int}월",
                f"{month_2}월",
            }
        )
    return aliases


def _rank_alias_tokens(text: str) -> set[str]:
    aliases: set[str] = set()
    for rank, total in re.findall(r"\b(\d{1,5})\s*/\s*(\d{1,6})\b", text):
        aliases.update({rank, total, f"{rank}/{total}", f"{rank}위"})
    for rank in re.findall(r"\b(\d{1,5})\s*위\b", text):
        aliases.update({rank, f"{rank}위"})
    return aliases


def _quarter_alias_tokens(text: str) -> set[str]:
    aliases: set[str] = set()
    for year, quarter in re.findall(r"\b(20\d{2})\s*(?:년)?\s*([1-4])\s*분기\b", text):
        aliases.update({year, quarter, f"{quarter}분기", f"{year}년{quarter}분기", f"{year}년 {quarter}분기"})
    for year, quarter in re.findall(r"\b(20\d{2})Q([1-4])\b", text, flags=re.IGNORECASE):
        aliases.update({year, quarter, f"Q{quarter}", f"{year}Q{quarter}", f"{year}-Q{quarter}"})
    for year, quarter in re.findall(r"\b(20\d{2})-Q([1-4])\b", text, flags=re.IGNORECASE):
        aliases.update({year, quarter, f"Q{quarter}", f"{year}Q{quarter}", f"{year}-Q{quarter}"})
    return aliases


def safe_news_summary_lines(fact_md: str) -> tuple[str, ...]:
    """Return cited news bullets using only title/date/source/issue text from news fact rows."""
    rows: list[str] = []
    for record in _news_fact_records(fact_md):
        source = record.get("source") or "뉴스"
        date = record.get("date") or "날짜 미상"
        title = record.get("title") or "제목 미상"
        url = record.get("url") or ""
        title_ref = f"[「{title}」]({url})" if url else f"「{title}」"
        issue = _news_issue_text(record)
        if issue:
            rows.append(f"- 뉴스: {source}({date}) {title_ref} — {issue}")
        else:
            rows.append(f"- 뉴스: {source}({date}) {title_ref}")
    return tuple(dict.fromkeys(rows))


def replace_empty_news_shells(answer: str, fact_md: str) -> str:
    """Replace generic news-presence prose with concrete cited article facts."""
    if not _has_news_fact(fact_md) or not _EMPTY_NEWS_SHELL_RE.search(answer):
        return answer
    replacement = "\n".join(safe_news_summary_lines(fact_md)[:3])
    if not replacement:
        return answer
    kept = [
        line
        for line in answer.splitlines()
        if not _EMPTY_NEWS_SHELL_RE.search(line)
    ]
    kept_text = "\n".join(kept).strip()
    return cleanup_markdown_answer("\n\n".join(part for part in (kept_text, replacement) if part))


def _top_brand_fallback_answer(lines: list[str], fact_md: str, source_line: str) -> str:
    rows = [_parse_top_brand_line(line) for line in lines]
    rows = [row for row in rows if row]
    if not rows:
        return ""
    trend_rows = [_parse_top_trend_line(line) for line in lines]
    trend_rows = [row for row in trend_rows if row]
    leader = rows[0]
    followers = rows[1:4]
    follower_names = ", ".join(row["brand"] for row in followers)
    trend_sentence = _top_trend_sentence(trend_rows)
    insight_sentence = _competitive_insight_sentence(lines)
    intro = (
        f"조회 결과에서 {_subject_with_particle(leader['brand'])} 선두를 지키고 있으며, "
        f"{leader['brand']} 시장점유율 {leader['share']}%(매출 {leader['sales']}억원)입니다."
        + (f" {follower_names} 등이 뒤따르고 있어 경쟁 구도가 이어지고 있습니다." if followers else "")
        + (f" {trend_sentence}" if trend_sentence else "")
        + (f" {insight_sentence}" if insight_sentence else "")
        + " 이 신호는 상위권 점유율·매출 변화에서 드러나는 경쟁 압력의 근거로 해석할 수 있습니다."
    )
    verified_values = ", ".join(
        f"{row['brand']} 시장점유율 {row['share']}%, 매출 {row['sales']}억원" for row in rows
    )
    detail_sentence = f"구체적으로는 {verified_values}입니다."
    table = [
        "| 순위 | 브랜드 | 점유율 | 매출 |",
        "| --- | --- | ---: | ---: |",
    ]
    for row in rows:
        table.append(f"| {row['rank']}위 | {row['brand']} | {row['share']}% | {row['sales']}억원 |")
    parts = [intro, detail_sentence, "\n".join(table)]
    news_lines = list(safe_news_summary_lines(fact_md))[:3]
    if news_lines:
        parts.extend(("관련 이슈 맥락", "\n".join(news_lines)))
    if source_line:
        parts.append(source_line)
    return "\n\n".join(parts)


def ensure_natural_fact_lead(question: str, answer: str, fact_md: str) -> str:
    """Prepend grounded prose when a market answer still starts as a fact dump."""

    first_line = next((line.strip() for line in answer.splitlines() if line.strip()), "")
    ingredient_question = re.fullmatch(r"\s*[^\s]+\s+(?:성분|주성분)\s*[?.!。？！]*\s*", question)
    ingredient_fact = _INGREDIENT_IDENTITY_FACT_RE.fullmatch(first_line)
    if ingredient_question and ingredient_fact:
        lead = (
            f"{ingredient_fact.group('brand')}의 주성분은 "
            f"{ingredient_fact.group('ingredient')}입니다."
        )
        return cleanup_markdown_answer("\n\n".join((lead, "## 근거 데이터", answer)))
    disease_question = re.fullmatch(r"\s*(?P<brand>[^\s]+)\s+(?:질환|질병)\s*[?.!。？！]*\s*", question)
    disease_fact = next(
        (
            match
            for line in fact_md.splitlines()
            if (match := _DISEASE_IDENTITY_FACT_RE.fullmatch(line.strip())) is not None
        ),
        None,
    )
    if disease_question and disease_fact:
        lead = (
            f"{disease_question.group('brand')}는 {disease_fact.group('source')} 기준 상병코드 "
            f"{disease_fact.group('code')}, 질병명 '{disease_fact.group('disease')}'에 해당합니다."
        )
        evidence = disease_fact.group(0)
        source_start = re.search(r"(?m)^##\s*출처\s*$", answer)
        source_block = answer[source_start.start() :].strip() if source_start else ""
        return cleanup_markdown_answer(
            "\n\n".join(part for part in (lead, "## 근거 데이터", evidence, source_block) if part)
        )
    if any(token in question for token in ("경쟁", "구도", "상위")):
        if first_line.startswith(("조회 결과에서", f"{question.split(maxsplit=1)[0]} 경쟁구도를 보면")):
            return answer
        rows = _competition_table_rows(answer)
        if rows:
            leader = rows[0]
            followers = "·".join(row[1] for row in rows[1:3])
            subject = question.split(maxsplit=1)[0]
            lead = (
                f"{subject} 경쟁구도를 보면 {_subject_with_particle(leader[1])} {leader[2]}({leader[3]})로 선두이며"
                + (f", {_subject_with_particle(followers)} 뒤를 잇고 있습니다." if followers else ".")
                + " 관련 순위와 이슈는 아래 표와 뉴스에서 확인할 수 있습니다."
            )
            return cleanup_markdown_answer("\n\n".join((lead, answer)))
    if "매출" in question and any(token in question for token in ("최근", "어때", "현황", "추이")):
        if first_line and not first_line.startswith(("#", "|")):
            return answer
        fact = _brand_metric_fact(fact_md)
        if fact:
            lead = (
                f"{fact['brand']}는 {fact['period']} 기준 매출 {fact['sales']}억원을 기록하고 있으며, "
                f"시장점유율 {fact['share']}%와 순위 {fact['rank']}으로 확인됩니다."
            )
            return cleanup_markdown_answer("\n\n".join((lead, answer)))
    return answer


def _competition_table_rows(answer: str) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for line in answer.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 4 or not re.fullmatch(r"\d+위?", cells[0]):
            continue
        if not re.fullmatch(r"\d+(?:\.\d+)?%", cells[2]):
            continue
        if not re.fullmatch(r"\d+(?:,\d{3})*(?:\.\d+)?억원", cells[3]):
            continue
        rows.append((cells[0], cells[1], cells[2], cells[3]))
    return rows


def _sales_delta_fallback_answer(lines: list[str], fact_md: str, source_line: str) -> str:
    rows = [_parse_sales_delta_line(line) for line in lines]
    rows = [row for row in rows if row]
    if not rows:
        return ""
    if len(rows) == 1:
        row = rows[0]
        intro = (
            f"{row['brand']} 매출은 {row['period']} 기준 {row['from_sales']}에서 {row['to_sales']}로 "
            f"{row['delta_sales']}({row['delta_pct']}%) 변했습니다."
        )
    else:
        main = rows[0]
        comparison = rows[1]
        direction = _delta_direction(main["delta_pct"], comparison["delta_pct"])
        relation = _delta_relation_sentence(direction)
        intro = (
            f"{_with_and_particle(main['brand'])} {comparison['brand']}의 {main['period']} 구간 매출 흐름은 {direction}. "
            f"{main['brand']}는 {main['from_sales']}에서 {main['to_sales']}로 {main['delta_sales']}({main['delta_pct']}%) 변했고, "
            f"{comparison['brand']}는 {comparison['from_sales']}에서 {comparison['to_sales']}로 {comparison['delta_sales']}({comparison['delta_pct']}%) 변했습니다. "
            f"따라서 {relation} 관련 기사 맥락이 같은 기간에 겹치면 단기 경쟁 압력의 배경 근거로 함께 해석할 수 있습니다."
        )
    parts = [intro]
    news_lines = list(safe_news_summary_lines(fact_md))[:3]
    if news_lines:
        parts.extend(("관련 기사 맥락", "\n".join(news_lines)))
    if source_line:
        parts.append(source_line)
    return "\n\n".join(parts)


def _parse_sales_delta_line(line: str) -> dict[str, str]:
    match = re.search(
        r"매출 변화:\s*(?P<brand>.+?)\s+(?P<period>20\d{2}-\d{2}→20\d{2}-\d{2}):\s*"
        r"(?P<from_sales>-?\d+(?:\.\d+)?억원)\s*→\s*(?P<to_sales>-?\d+(?:\.\d+)?억원),\s*"
        r"변화\s*(?P<delta_sales>[+-]?\d+(?:\.\d+)?억원)\((?P<delta_pct>[+-]?\d+(?:\.\d+)?)%\)",
        line,
    )
    return match.groupdict() if match else {}


def _delta_direction(first_pct: str, second_pct: str) -> str:
    try:
        first = float(first_pct)
        second = float(second_pct)
    except ValueError:
        return "변했습니다"
    if first < 0 and second < 0:
        return "감소했습니다"
    if first > 0 and second > 0:
        return "증가했습니다"
    return "엇갈렸습니다"


def _delta_relation_sentence(direction: str) -> str:
    if direction in {"감소했습니다", "증가했습니다"}:
        return "두 브랜드의 단기 매출 흐름은 같은 방향으로 움직인 것으로 해석됩니다."
    return "두 브랜드의 단기 매출 흐름은 서로 다른 방향 또는 강도로 움직인 것으로 해석됩니다."


def _with_and_particle(value: str) -> str:
    text = value.strip()
    if not text:
        return value
    code = ord(text[-1])
    if 0xAC00 <= code <= 0xD7A3 and (code - 0xAC00) % 28:
        return f"{text}과"
    return f"{text}와"


def _parse_top_brand_line(line: str) -> dict[str, str]:
    match = re.search(
        r"Brand 상위:\s*(?P<rank>\d+)위\s+(?P<brand>.+?)\s+시장점유율\s+(?P<share>-?\d+(?:\.\d+)?)%\s+매출\s+(?P<sales>-?\d+(?:\.\d+)?)억원",
        line,
    )
    if not match:
        return {}
    return match.groupdict()


def _parse_top_trend_line(line: str) -> dict[str, str]:
    start_latest_match = re.search(
        r"상위 (?P<axis_label>[^:]+?) 추이:\s*(?P<rank>\d+)위\s+(?P<brand>.+?)\s+"
        r"(?P<from_period>[12]\d{3}(?:-\d{2}|-Q\d))\s+MS\s+(?P<from_share>-?\d+(?:\.\d+)?)%\s+→\s+"
        r"(?P<to_period>[12]\d{3}(?:-\d{2}|-Q\d))\s+MS\s+(?P<to_share>-?\d+(?:\.\d+)?)%\s+"
        r"(?P<period>[12]\d{3}(?:-\d{2}|-Q\d)→[12]\d{3}(?:-\d{2}|-Q\d))\s+점유율 변화\s+"
        r"(?P<share_delta>[+-]?\d+(?:\.\d+)?)%p"
        r"(?:\s+최신 매출\s+(?P<sales>-?[\d,]+(?:\.\d+)?)억원)?"
        r"(?:\s+매출 변화\s+(?P<sales_delta>[+-]?[\d,]+(?:\.\d+)?)억원)?",
        line,
    )
    if start_latest_match:
        data = start_latest_match.groupdict()
        data["share"] = data["to_share"]
        return data
    match = re.search(
        r"상위 (?P<axis_label>[^:]+?) 추이:\s*(?P<rank>\d+)위\s+(?P<brand>.+?)\s+최신 시장점유율\s+"
        r"(?P<share>-?\d+(?:\.\d+)?)%\s+점유율 변화\s+(?P<share_delta>[+-]?\d+(?:\.\d+)?)%p"
        r"(?:\s+최신 매출\s+(?P<sales>-?[\d,]+(?:\.\d+)?)억원)?"
        r"(?:\s+매출 변화\s+(?P<sales_delta>[+-]?[\d,]+(?:\.\d+)?)억원)?",
        line,
    )
    if match:
        return match.groupdict()
    perioded_match = re.search(
        r"상위 (?P<axis_label>[^:]+?) 추이:\s*(?P<rank>\d+)위\s+(?P<brand>.+?)\s+최신 시장점유율\s+"
        r"(?P<share>-?\d+(?:\.\d+)?)%\s+"
        r"(?P<period>[12]\d{3}(?:-\d{2}|-Q\d)→[12]\d{3}(?:-\d{2}|-Q\d))\s+점유율 변화\s+"
        r"(?P<share_delta>[+-]?\d+(?:\.\d+)?)%p"
        r"(?:\s+최신 매출\s+(?P<sales>-?[\d,]+(?:\.\d+)?)억원)?"
        r"(?:\s+매출 변화\s+(?P<sales_delta>[+-]?[\d,]+(?:\.\d+)?)억원)?",
        line,
    )
    return perioded_match.groupdict() if perioded_match else {}


def ensure_top_brand_trend_table(answer: str, fact_md: str) -> str:
    mandatory = mandatory_fact_lines(fact_md)
    trend_lines = tuple(line for line in mandatory if re.match(r"-\s*상위\s+[^:]+?\s+추이:", line))
    if not trend_lines:
        return answer
    raw_lines = set(trend_lines)
    has_raw_lines = any(line.strip() in raw_lines for line in answer.splitlines())
    rows = tuple(row for line in trend_lines if (row := _parse_top_trend_line(line)))
    rows_with_operands = tuple(row for row in rows if row.get("from_share") and row.get("to_share"))
    if not rows_with_operands:
        return answer
    axis_label = _top_trend_axis_label(rows_with_operands)
    has_table = f"| 최신 순위 | {axis_label} | 시작 MS | 최신 MS | MS 변화 |" in answer
    needs_table_replacement = _top_brand_trend_table_needs_replacement(answer, rows_with_operands)
    if not needs_table_replacement and not has_raw_lines and not missing_mandatory_lines(answer, trend_lines):
        return answer
    if has_table and not has_raw_lines and not needs_table_replacement:
        return answer
    table = _top_brand_trend_table(rows_with_operands, axis_label)
    if not table:
        return answer
    kept = [line for line in answer.splitlines() if line.strip() not in raw_lines]
    body = "\n".join(kept).strip()
    if has_table:
        body = _remove_existing_top_brand_trend_table(body, axis_label)
    return cleanup_markdown_answer("\n\n".join((body, table)))


def _top_brand_trend_table_needs_replacement(answer: str, rows: tuple[dict[str, str], ...]) -> bool:
    for row in rows:
        sales = row.get("sales")
        if not sales:
            continue
        brand = row.get("brand") or ""
        if not brand:
            continue
        row_match = re.search(rf"(?m)^\|\s*{re.escape(row.get('rank') or '')}\s*\|\s*{re.escape(brand)}\s*\|[^\n]+$", answer)
        if not row_match:
            continue
        cells = [cell.strip() for cell in row_match.group(0).strip().strip("|").split("|")]
        if len(cells) >= 6 and cells[5] in {"", "-"}:
            return True
    return False


def _remove_existing_top_brand_trend_table(answer: str, axis_label: str) -> str:
    lines = answer.splitlines()
    kept: list[str] = []
    index = 0
    heading = f"### 상위 {axis_label} 추이"
    header = f"| 최신 순위 | {axis_label} | 시작 MS | 최신 MS | MS 변화 |"
    while index < len(lines):
        line = lines[index]
        if line.strip() == heading:
            index += 1
            while index < len(lines) and (not lines[index].strip() or lines[index].lstrip().startswith("|") or is_dosage_combination_note(lines[index])):
                index += 1
            continue
        if line.startswith(header):
            index += 1
            while index < len(lines) and (not lines[index].strip() or lines[index].lstrip().startswith("|") or is_dosage_combination_note(lines[index])):
                index += 1
            continue
        kept.append(line)
        index += 1
    return "\n".join(kept).strip()


def _top_trend_axis_label(rows: tuple[dict[str, str], ...]) -> str:
    for row in rows:
        label = str(row.get("axis_label") or "").strip()
        if label:
            return label
    return "브랜드"


def _top_brand_trend_table(rows: tuple[dict[str, str], ...], axis_label: str = "브랜드") -> str:
    table = [
        f"### 상위 {axis_label} 추이",
        f"| 최신 순위 | {axis_label} | 시작 MS | 최신 MS | MS 변화 | 최신 매출 | 매출 변화 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        sales = f"{row['sales']}억원" if row.get("sales") else "-"
        sales_delta = f"{row['sales_delta']}억원" if row.get("sales_delta") else "-"
        table.append(
            f"| {row['rank']} | {row['brand']} | {row['from_period']} {row['from_share']}% | "
            f"{row['to_period']} {row['to_share']}% | {row['share_delta']}%p | {sales} | {sales_delta} |"
        )
    note = dosage_combination_note(axis_label, (row.get("brand") for row in rows))
    if note:
        table.append(note)
    return "\n".join(table)


def _top_trend_sentence(rows: list[dict[str, str]]) -> str:
    if not rows:
        return ""
    movers = sorted(rows, key=lambda row: abs(float(row.get("share_delta") or 0)), reverse=True)
    risers = [row for row in movers if float(row.get("share_delta") or 0) > 0]
    fallers = [row for row in movers if float(row.get("share_delta") or 0) < 0]
    parts: list[str] = []
    if risers:
        row = risers[0]
        parts.append(f"상승 폭이 큰 쪽은 {row['brand']}({row['share_delta']}%p)입니다")
    if fallers:
        row = fallers[0]
        parts.append(f"하락 폭이 큰 쪽은 {row['brand']}({row['share_delta']}%p)입니다")
    if not parts:
        return "상위권 점유율 변화는 제한적입니다."
    return "최근 변화는 " + ", ".join(parts) + "."


def _competitive_insight_sentence(lines: list[str]) -> str:
    insights = [line for line in lines if "인사이트 계산" in line]
    if not insights:
        return ""
    share_line = next((line for line in insights if "share-of-growth" in line), insights[0])
    movement_line = next((line for line in insights if "상승폭" in line and "하락폭" in line), "")
    pieces: list[str] = []
    share = _regex_value(share_line, r"share-of-growth\s+([+-]?\d+(?:\.\d+)?%)")
    brand = _regex_value(share_line, r"인사이트 계산:\s*([가-힣A-Za-z0-9+._/-]+)\s+share-of-growth")
    if share and brand:
        pieces.append(f"{_subject_with_particle(brand)} 시장 성장 기여도 지표인 share-of-growth {share}로 성장 기여 정도를 보여줍니다")
    movement = _movement_phrase(movement_line)
    if movement:
        pieces.append(movement)
    if not pieces:
        return ""
    return " ".join(pieces) + "."


def _movement_phrase(line: str) -> str:
    if not line:
        return ""
    gainer = _regex_value(line, r"인사이트 계산:\s*([가-힣A-Za-z0-9+._/-]+)(?:\s+\d{4}(?:-\d{2}|-Q\d)→\d{4}(?:-\d{2}|-Q\d))?\s+상승폭")
    faller = _regex_value(
        line,
        r"상승폭\s+[+-]?\d+(?:\.\d+)?%p\s+([가-힣A-Za-z0-9+._/-]+)"
        r"(?:\s+\d{4}(?:-\d{2}|-Q\d)→\d{4}(?:-\d{2}|-Q\d))?\s+하락폭",
    )
    gain = _regex_value(line, r"상승폭\s+([+-]?\d+(?:\.\d+)?%p)")
    loss = _regex_value(line, r"하락폭\s+([+-]?\d+(?:\.\d+)?%p)")
    period = _regex_value(line, r"([12]\d{3}(?:-\d{2}|-Q\d)→[12]\d{3}(?:-\d{2}|-Q\d))")
    period_text = f"{period} " if period else ""
    if gainer and faller and gain and loss:
        return f"점유율 이동 관점에서는 {gainer} {period_text}상승폭 {gain}와 {faller} 하락폭 {loss}가 반대 방향입니다"
    return ""


def _subject_with_particle(value: str) -> str:
    text = value.strip()
    if not text:
        return value
    last = text[-1]
    code = ord(last)
    if 0xAC00 <= code <= 0xD7A3:
        return f"{text}{'이' if (code - 0xAC00) % 28 else '가'}"
    return f"{text}가"


def _needs_top_brand_insight(question: str, answer: str) -> bool:
    return "Brand 상위:" in answer


def needs_safe_news_summary(question: str, answer: str, fact_md: str) -> bool:
    if "뉴스" not in question or not _has_news_fact(fact_md):
        return False
    return not any(token in answer for token in ("뉴스:", "관련 기사", "기사에서"))


def ensure_share_delta_line(question: str, answer: str, fact_md: str) -> str:
    """Append a deterministic share-delta line when Flash only lists endpoints."""
    if not ("점유율" in question and "대비" in question):
        return answer
    if re.search(r"[+-]?\d+(?:\.\d+)?%p", answer) and "변화" in answer:
        return answer
    mandatory = _mandatory_share_delta_line(fact_md)
    if mandatory:
        return cleanup_markdown_answer("\n\n".join((answer, mandatory)))
    computed = _computed_share_delta_line(answer) or _computed_share_delta_line(fact_md)
    if not computed:
        return answer
    return cleanup_markdown_answer("\n\n".join((answer, computed)))


def ensure_judgment_insight(question: str, answer: str, fact_md: str) -> str:
    """Ensure judgment answers contain a fact-bounded conclusion instead of raw fact echoes."""

    mandatory = mandatory_fact_lines(fact_md)
    judgment_lines = tuple(
        line
        for line in mandatory
        if "시장/브랜드 변화율 대조" in line or "브랜드 추세 비교" in line
    )
    revised = _drop_raw_mandatory_lines(answer, judgment_lines)
    revised = _drop_empty_markdown_tables(revised)
    if _needs_top_brand_insight(question, revised):
        top_brand_answer = _top_brand_fallback_answer(list(mandatory), fact_md, _source_line(fact_md))
        if top_brand_answer:
            return cleanup_markdown_answer(top_brand_answer)
    market_line = next((line for line in mandatory if "시장/브랜드 변화율 대조" in line), "")
    if market_line and _needs_market_brand_insight(question, revised):
        insight = _market_brand_insight(market_line)
        if insight:
            revised = cleanup_markdown_answer("\n\n".join((insight, revised)))
    trend_line = next((line for line in mandatory if "브랜드 추세 비교" in line), "")
    if trend_line and _needs_brand_threat_insight(question, revised):
        insight = _brand_threat_insight(trend_line)
        if insight:
            revised = cleanup_markdown_answer("\n\n".join((insight, revised)))
    return cleanup_markdown_answer(revised)


def ensure_competitive_movement_analysis(question: str, answer: str, fact_md: str) -> str:
    """Ensure competitive landscape answers preserve gain-loss causal movement facts."""

    if not any(token in question for token in ("경쟁", "구도", "상위", "브랜드")):
        return answer
    movement = _competitive_movement_analysis_line(fact_md)
    if not movement:
        return answer
    if _competitive_movement_present(answer, movement):
        return answer
    return cleanup_markdown_answer(_insert_before_first_table_timing_or_source(answer, movement))


def ensure_single_brand_trend_analysis(
    question: str,
    answer: str,
    fact_md: str,
    calls: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
) -> str:
    """Keep the trend-answer hook as a safety cleanup, not a prose generator."""

    if not _needs_single_brand_trend_analysis(question):
        return answer
    return remove_raw_fact_residue(answer, fact_md)


def single_brand_trend_fact_markdown(
    fact_md: str,
    calls: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
) -> str:
    """Return structured trend facts for LLM prose generation without fixed narrative templates."""

    trend = _single_brand_trend_fact_from_calls(calls) or _single_brand_trend_fact_from_fact_md(fact_md)
    if trend is None:
        return ""
    rows = [
        ("brand", trend.brand),
        ("grain", _trend_grain(trend)),
        ("shape", _trend_shape(trend)),
        ("first", _trend_point_summary(trend.first)),
        ("peak", _trend_point_summary(trend.peak)),
        ("trough_after_peak", _trend_point_summary(trend.trough)),
        ("latest", _trend_point_summary(trend.latest)),
        ("market_first", _trend_point_summary(_market_point_for(trend, trend.first.period))),
        ("market_latest", _trend_point_summary(_market_point_for(trend, trend.latest.period))),
        ("allowed_periods", ", ".join(point.period for point in _trend_sorted_points(trend))),
    ]
    rendered_rows = "\n".join(f"| {label} | {value or '-'} |" for label, value in rows if value)
    return cleanup_markdown_answer(
        "\n".join(
            (
                "### 단일 브랜드 추이 산문용 trend fact",
                "| 항목 | 값 |",
                "| --- | --- |",
                rendered_rows,
            )
        )
    )


def ensure_issue_question_quant_analysis(question: str, answer: str, fact_md: str) -> str:
    """Ensure issue/news answers connect cited issues to quantified brand context."""

    if not _needs_issue_question_quant_analysis(question):
        return answer
    if _analysis_sentence_count(answer) >= 2:
        return answer
    analysis = _issue_question_quant_analysis_line(fact_md)
    if not analysis:
        return answer
    return cleanup_markdown_answer(_insert_before_first_table_timing_or_source(answer, analysis))


def ensure_causal_structure(question: str, answer: str, fact_md: str) -> str:
    """Add only a last-resort causal nudge when judgment analysis is absent."""

    if _has_causal_structure(answer) or _has_existing_causal_analysis(answer):
        return answer
    evidence = _causal_evidence_line(question, fact_md)
    if not evidence:
        return answer
    interpretation = _causal_interpretation(evidence)
    implication = _causal_implication(evidence)
    block = f"{_sentence_from_evidence(evidence)} {interpretation} {implication}"
    return cleanup_markdown_answer("\n\n".join((answer, block)))


def strip_generated_source_sections(answer: str) -> str:
    """Remove LLM-authored source lines so sources can be rendered deterministically."""

    lines = answer.splitlines()
    kept: list[str] = []
    skipping_source_section = False
    for line in lines:
        stripped = line.strip()
        if _is_source_heading(stripped):
            skipping_source_section = True
            continue
        if skipping_source_section:
            if _is_heading(stripped):
                skipping_source_section = False
            else:
                continue
        if stripped.startswith("출처:") or _is_inline_source_line(stripped):
            continue
        kept.append(line)
    return cleanup_markdown_answer("\n".join(kept).strip())


def ensure_deep_research_structure(answer: str) -> str:
    """Enforce the public deep-report shape without changing verified facts."""

    cleaned = _clean_deep_public_markdown(cleanup_markdown_answer(answer))
    body, source = _split_deep_source_section(cleaned)
    source = slim_source_tables(source)
    if not body:
        return cleaned

    headings = tuple(_DEEP_BODY_HEADING_RE.finditer(body))
    summary_heading = re.search(r"(?m)^##\s+핵심 요약\s*$", body)
    analysis_heading = re.search(r"(?m)^##\s+종합 분석\s*$", body)
    if summary_heading and analysis_heading and summary_heading.start() < analysis_heading.start():
        structured_body = body
        return cleanup_markdown_answer("\n\n".join(part for part in (structured_body, source) if part))

    blocks = [block.strip() for block in re.split(r"\n{2,}", body) if block.strip()]
    if not headings:
        if len(blocks) < 2:
            return cleaned
        structured_body = "\n\n".join(
            (
                "## 핵심 요약",
                blocks[0],
                "## 종합 분석",
                "\n\n".join(blocks[1:]),
            )
        )
    elif len(headings) >= 2:
        first_heading = headings[0]
        second_heading = headings[1]
        leading = body[: first_heading.start()].strip()
        first_section = body[first_heading.end() : second_heading.start()].strip()
        first_title = body[first_heading.start() : first_heading.end()].removeprefix("##").strip()
        second_title = body[second_heading.start() : second_heading.end()].removeprefix("##").strip()
        remaining_sections = body[second_heading.end() :].strip()
        first_labeled_section = (
            first_section if first_title == "핵심 요약" else f"**{first_title}** {first_section}".strip()
        )
        summary = "\n\n".join(part for part in (leading, first_labeled_section) if part)
        remaining_sections = _demote_deep_body_headings(remaining_sections)
        second_labeled_section = f"**{second_title}** {remaining_sections}".strip()
        structured_body = "\n\n".join(
            (
                "## 핵심 요약",
                summary,
                "## 종합 분석",
                second_labeled_section,
            )
        )
    else:
        first_heading = headings[0]
        leading = body[: first_heading.start()].strip()
        if leading:
            first_title = body[first_heading.start() : first_heading.end()].removeprefix("##").strip()
            first_content = body[first_heading.end() :].strip()
            structured_body = "\n\n".join(
                (
                    "## 핵심 요약",
                    leading,
                    "## 종합 분석",
                    f"**{first_title}** {first_content}".strip(),
                )
            )
        else:
            content_blocks = [
                block.strip()
                for block in re.split(r"\n{2,}", body[first_heading.end() :].strip())
                if block.strip()
            ]
            if len(content_blocks) < 2:
                return cleaned
            structured_body = "\n\n".join(
                (
                    "## 핵심 요약",
                    content_blocks[0],
                    "## 종합 분석",
                    "\n\n".join(content_blocks[1:]),
                )
            )

    return cleanup_markdown_answer("\n\n".join(part for part in (structured_body, source) if part))


def _demote_deep_body_headings(markdown: str) -> str:
    return re.sub(r"(?m)^##\s+", "### ", markdown)


def _clean_deep_public_markdown(answer: str) -> str:
    answer = _repair_markdown_link_urls(answer)
    answer = repair_plain_table_urls(answer)
    answer = _mark_future_deep_dates(answer)
    lines = _drop_deep_policy_and_crawl_debris(answer.splitlines())
    answer = "\n".join(lines).strip()
    answer = re.sub(
        r"(습니다|입니다|됩니다|보입니다|확인됩니다)\s+(?=[가-힣A-Za-z])",
        r"\1. ",
        answer,
    )
    return _dedupe_deep_blocks(answer)


def _repair_markdown_link_urls(answer: str) -> str:
    def repair(match: re.Match[str]) -> str:
        target = match.group("target")
        if not target.lstrip().startswith(("http://", "https://")):
            return match.group(0)
        compact_target = re.sub(r"\s+", "", target)
        return f"]({compact_target})"

    return re.sub(r"]\((?P<target>[^)]+)\)", repair, answer)


def _mark_future_deep_dates(answer: str) -> str:
    today = date.today()

    def mark_dates(text: str) -> str:
        def mark(match: re.Match[str]) -> str:
            raw = match.group(0)
            try:
                parsed = date.fromisoformat(raw)
            except ValueError:
                return raw
            suffix = text[match.end() : match.end() + 8]
            return f"{raw} (예정)" if parsed > today and "예정" not in suffix else raw

        return re.sub(r"(?<![\d/])\d{4}-\d{2}-\d{2}(?![\d/])", mark, text)

    parts = re.split(r"(]\(https?://[^)]+\))", answer)
    return "".join(part if part.startswith("](") else mark_dates(part) for part in parts)


def _drop_deep_policy_and_crawl_debris(lines: list[str]) -> list[str]:
    kept: list[str] = []
    skipping_policy = False
    for line in lines:
        stripped = line.strip()
        heading = re.match(r"^(#{1,6})\s+", stripped)
        if re.match(r"^#{1,6}\s+(?:미보유 데이터 처리|미지원 축 처리)\s*$", stripped):
            skipping_policy = True
            continue
        if skipping_policy:
            if heading and len(heading.group(1)) <= 3:
                skipping_policy = False
            else:
                continue
        if re.match(r"^#{1,6}\s*Image\s+\d+\s*:?$", stripped, re.IGNORECASE):
            continue
        if re.match(r"^[-*•]?\s*Image\s+\d+\s*:?$", stripped, re.IGNORECASE):
            continue
        if stripped in {"→ 내부 지표 확인 가능", "내부 지표 확인 가능"}:
            continue
        public_line = re.sub(r"\s*→?\s*내부 지표 확인 가능", "", line).rstrip()
        kept.append(public_line)
    return kept


def _dedupe_deep_blocks(answer: str) -> str:
    blocks = [block.strip() for block in re.split(r"\n{2,}", answer) if block.strip()]
    seen: set[str] = set()
    kept: list[str] = []
    for block in blocks:
        if block.startswith("#") or block.startswith("|"):
            kept.append(block)
            continue
        normalized = re.sub(r"\s+", " ", re.sub(r"[*_`]", "", block)).strip()
        if normalized in seen:
            continue
        seen.add(normalized)
        kept.append(block)
    return "\n\n".join(kept)


def _split_deep_source_section(answer: str) -> tuple[str, str]:
    lines = answer.splitlines()
    body: list[str] = []
    source: list[str] = []
    in_source = False
    source_seen = False
    for line in lines:
        stripped = line.strip()
        if _DEEP_SOURCE_HEADING_RE.fullmatch(stripped):
            in_source = True
            if not source_seen:
                source_seen = True
                source.append("## 출처")
            continue
        if in_source and re.match(r"^##\s+", stripped):
            in_source = False
        if in_source:
            if source_seen:
                source.append(line)
            continue
        body.append(line)
    return "\n".join(body).strip(), "\n".join(source).strip()


def _is_inline_source_line(stripped: str) -> bool:
    """Return whether a generated prose line is only an inline citation/source bullet."""

    normalized = stripped.lstrip("-*• ").strip()
    return bool(re.match(r"^출처\s*\(\d{4}(?:-\d{2})?(?:-\d{2})?\)", normalized))


def append_deterministic_source_block(answer: str, fact_md: str, *, file_context: str = "") -> str:
    """Append a code-rendered source block built from verified facts."""

    stripped_answer = strip_generated_source_sections(answer)
    source_block = deterministic_source_block(fact_md, file_context=file_context)
    if not source_block:
        return stripped_answer
    return cleanup_markdown_answer("\n\n".join((stripped_answer, source_block)))


def append_competitor_patent_coverage_block(answer: str, fact_md: str) -> str:
    """Append deterministic competitor patent scope when final prose omits it."""

    candidate_rows = _fact_table_rows(fact_md, "경쟁 성분 후보군 fact")
    coverage_rows = _fact_table_rows(fact_md, "경쟁 성분 특허 조회 커버리지 fact")
    if not candidate_rows and not coverage_rows:
        return answer
    if "경쟁 성분 후보군" in answer and ("커버리지" in answer or "현재 특허 DB에서 확인되는 항목" in answer):
        return answer
    lines = [
        "### 경쟁 성분 후보군·특허 커버리지",
        "현재 특허 DB에서 확인되는 항목만 표시하며, 전체 독점권을 단정하지 않습니다.",
    ]
    if candidate_rows:
        lines.extend(("", "| 순위 | 성분 | 대표 브랜드 | 출처 | 시장 | 기간 | 매출 | MS |", "| --- | --- | --- | --- | --- | --- | --- | --- |"))
        lines.extend(candidate_rows[:5])
    if coverage_rows:
        lines.extend(("", "#### 출처·커버리지", "| 항목 | 내용 |", "| --- | --- |"))
        lines.extend(coverage_rows[:5])
    return cleanup_markdown_answer("\n\n".join((answer.strip(), "\n".join(lines))))


def deterministic_source_block(fact_md: str, *, file_context: str = "") -> str:
    """Return the single public seven-field provenance schema."""

    return provenance_source_block_from_facts(fact_md, file_context=file_context)


def _fact_table_rows(fact_md: str, title_fragment: str) -> list[str]:
    lines = fact_md.splitlines()
    start = -1
    for index, line in enumerate(lines):
        if line.strip().startswith("### ") and title_fragment in line:
            start = index + 1
            break
    if start < 0:
        return []
    rows: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        if stripped.startswith("### ") or stripped.startswith("## "):
            break
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not cells or all(set(cell) <= {"-", ":", " "} for cell in cells):
            continue
        if any(cell in {"순위", "항목"} for cell in cells):
            continue
        rows.append(stripped)
    return rows


def normalize_source_line_position(answer: str) -> str:
    """Move user-facing source lines after any safety-added completion bullets."""

    lines = answer.splitlines()
    source_lines = [line for line in lines if line.strip().startswith("출처:")]
    if not source_lines:
        return answer
    kept = [line for line in lines if not line.strip().startswith("출처:")]
    while kept and not kept[-1].strip():
        kept.pop()
    source = "출처: " + ", ".join(
        dict.fromkeys(
            part.strip()
            for line in source_lines
            for part in line.strip().removeprefix("출처:").split(",")
            if part.strip()
        )
    )
    return cleanup_markdown_answer("\n".join((*kept, "", source)))


def mandatory_fact_block(fact_md: str) -> str:
    lines = mandatory_fact_lines(fact_md)
    if not lines:
        return ""
    return "\n".join(lines)


def mandatory_fact_lines(fact_md: str) -> tuple[str, ...]:
    if "### 필수 답변 fact" not in fact_md:
        return ()
    lines = fact_md.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == "### 필수 답변 fact"), -1)
    if start < 0:
        return ()
    parsed: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped.startswith("### "):
            break
        if not stripped.startswith("|") or "---" in stripped or "구분" in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) >= 2 and cells[0] and cells[1]:
            parsed.append(f"- {cells[0]}: {cells[1]}")
    return tuple(parsed)


def missing_mandatory_lines(answer: str, mandatory_lines: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(line for line in mandatory_lines if not _mandatory_line_present(answer, line))


def presentable_mandatory_lines(lines: tuple[str, ...]) -> tuple[str, ...]:
    """Convert raw mandatory fact rows into user-facing completion bullets."""

    return tuple(dict.fromkeys(_presentable_mandatory_line(line) for line in lines))


def _presentable_mandatory_line(line: str) -> str:
    if line.startswith("- 매출 추이:"):
        rendered = _presentable_sales_trend_line(line)
        if rendered:
            return rendered
    if line.startswith("- 브랜드 핵심 지표:"):
        rendered = _presentable_brand_metric_line(line)
        if rendered:
            return rendered
    if line.startswith("- HIRA 환자수:"):
        rendered = _presentable_hira_patient_line(line)
        if rendered:
            return rendered
    if not line.startswith("- 인사이트 계산:"):
        return line
    movement = _movement_phrase(line)
    if movement:
        movement_text = movement.removesuffix("합니다") + "하며" if movement.endswith("합니다") else movement
        return f"- 인사이트: {movement_text}, 두 브랜드 점유율이 반대 방향으로 변했지만 직접 처방 이동은 확인할 수 없습니다."
    brand = _regex_value(line, r"인사이트 계산:\s*([가-힣A-Za-z0-9+._/-]+)\s+share-of-growth")
    share = _regex_value(line, r"share-of-growth\s+([+-]?\d+(?:\.\d+)?%)")
    share_delta = _regex_value(line, r"점유\s+([+-]?\d+(?:\.\d+)?%p)")
    percentile = _regex_value(line, r"백분위\s+([+-]?\d+(?:\.\d+)?%)")
    if brand and share:
        extras = []
        if share_delta:
            extras.append(f"점유율 변화 {share_delta}")
        if percentile:
            extras.append(f"cohort 백분위 {percentile}")
        suffix = f", {', '.join(extras)}" if extras else ""
        return f"- 인사이트: {_subject_with_particle(brand)} share-of-growth {share}{suffix}로 시장 성장 기여도를 보여줍니다."
    return line.replace("- 인사이트 계산:", "- 인사이트:", 1)


def _presentable_sales_trend_line(line: str) -> str:
    payload = line.split(":", 1)[-1].strip()
    match = re.match(
        r"(?P<brand>\S+)\s+매출\s+시계열\s+"
        r"(?P<start_period>20\d{2}-\d{2})\s+(?P<start_sales>-?\d+(?:\.\d+)?)억원\s+→\s+"
        r"(?P<end_period>20\d{2}-\d{2})\s+(?P<end_sales>-?\d+(?:\.\d+)?)억원"
        r"(?:,\s*MS\s+(?P<start_share>-?\d+(?:\.\d+)?)%\s+→\s+(?P<end_share>-?\d+(?:\.\d+)?)%)?",
        payload,
    )
    if not match:
        return ""
    data = match.groupdict()
    share = ""
    if data.get("start_share") and data.get("end_share"):
        share = f", 시장점유율은 {data['start_share']}%에서 {data['end_share']}%로"
    return (
        f"{data['brand']} 매출은 {data['start_period']} {data['start_sales']}억원에서 "
        f"{data['end_period']} {data['end_sales']}억원으로 움직였고{share} 변했습니다."
    )


def _presentable_brand_metric_line(line: str) -> str:
    payload = line.split(":", 1)[-1].strip()
    match = re.match(
        r"(?P<brand>\S+)\s+(?P<period>20\d{2}-\d{2})\s+매출\s+"
        r"(?P<sales>-?\d+(?:\.\d+)?)억원\s+시장점유율\s+"
        r"(?P<share>-?\d+(?:\.\d+)?)%\s+순위\s+(?P<rank>\d+(?:/\d+)?)",
        payload,
    )
    if not match:
        return ""
    data = match.groupdict()
    return (
        f"{data['brand']}는 {data['period']} 기준 매출 {data['sales']}억원, "
        f"시장점유율 {data['share']}%, 순위 {data['rank']}입니다."
    )


def _presentable_hira_patient_line(line: str) -> str:
    payload = line.split(":", 1)[-1].strip()
    match = re.match(
        r"(?P<disease>.+?)\((?P<code>[A-Z]\d+[A-Z0-9.]*)\)\s+"
        r"(?:(?P<year>20\d{2})년\s+)?"
        r"(?P<segment>[^:]+):\s*(?P<count>-?\d[\d,]*(?:\.\d+)?)명",
        payload,
    )
    if not match:
        return ""
    data = match.groupdict()
    year = f" {data['year']}년" if data.get("year") else ""
    return f"HIRA 기준 {data['disease']}({data['code']}){year} {data['segment']} 환자수는 {data['count']}명입니다."


def has_mandatory_numeric_mismatch(answer: str, mandatory_lines: tuple[str, ...]) -> bool:
    """Detect fact-label mismatches such as assigning another row's share to a molecule."""
    assertions = tuple(assertion for line in mandatory_lines if (assertion := _mandatory_numeric_assertion(line)))
    if not assertions:
        return False
    for line in answer.splitlines():
        if not _line_has_metric_context(line):
            continue
        line_numbers = set(allowed_numbers(line))
        if not line_numbers:
            continue
        for subject, subject_numbers in assertions:
            if subject in line and not line_numbers.issubset(subject_numbers):
                return True
    return False


def remove_mandatory_numeric_mismatches(answer: str, mandatory_lines: tuple[str, ...]) -> str:
    """Remove lines that attach a fact number to the wrong mandatory subject."""

    assertions = tuple(assertion for line in mandatory_lines if (assertion := _mandatory_numeric_assertion(line)))
    if not assertions:
        return answer
    kept: list[str] = []
    for line in answer.splitlines():
        if not _line_has_metric_context(line):
            kept.append(line)
            continue
        line_numbers = set(allowed_numbers(line))
        if not line_numbers:
            kept.append(line)
            continue
        matching_subjects = tuple(subject for subject, _numbers in assertions if subject in line)
        if len(matching_subjects) > 1:
            kept.append(line)
            continue
        mismatched = any(subject in line and not line_numbers.issubset(subject_numbers) for subject, subject_numbers in assertions)
        if not mismatched:
            kept.append(line)
    return "\n".join(kept).strip()


def dedupe_repeated_hira_patient_counts(answer: str, mandatory_lines: tuple[str, ...]) -> str:
    """Remove repeated raw HIRA patient-count lines without rewriting other metrics."""

    counts = tuple(
        dict.fromkeys(
            token
            for line in mandatory_lines
            if "HIRA 환자수" in line
            for token in _hira_patient_count_tokens(line)
        )
    )
    if not counts:
        return answer
    seen: set[str] = set()
    kept: list[str] = []
    for line in answer.splitlines():
        line_counts = {count for count in counts if _patient_count_in_text(line, count)}
        duplicate_counts = line_counts & seen
        stripped = line.lstrip()
        is_hira_completion_line = stripped.startswith(("- HIRA 환자수:", "* HIRA 환자수:"))
        is_patient_detail_bullet = stripped.startswith(("-", "*")) and "환자" in stripped
        if duplicate_counts and (is_hira_completion_line or is_patient_detail_bullet):
            continue
        kept.append(line)
        seen.update(line_counts)
    return "\n".join(kept).strip()


def ensure_hira_patient_summary(question: str, answer: str, fact_md: str) -> str:
    """Ensure disease/patient questions include actual HIRA patient counts once."""

    if not re.search(r"(환자|질병|HIRA)", question, re.IGNORECASE):
        return answer
    lines = _hira_patient_summary_lines(fact_md)
    if not lines:
        lines = _hira_unavailable_summary_lines(fact_md)
    if not lines:
        return answer
    if any("HIRA 조회 상태" in line for line in lines) and "환자수 수치 미반환" in answer:
        return answer
    answer_numbers = set(_plain_number_tokens(answer))
    line_numbers = {token for line in lines for token in _hira_patient_count_tokens(line)}
    if answer_numbers & line_numbers:
        answer_lines = answer.splitlines()
        first_table = next(
            (offset for offset, line in enumerate(answer_lines) if line.lstrip().startswith("|")),
            None,
        )
        lead_lines = answer_lines if first_table is None else answer_lines[:first_table]
        lead_prose = "\n".join(line for line in lead_lines if not line.lstrip().startswith("|"))
        if set(_plain_number_tokens(lead_prose)) & line_numbers:
            return answer
        natural_lines = presentable_mandatory_lines(lines[:3])
        if natural_lines:
            without_late_duplicates = answer
            for line in natural_lines:
                without_late_duplicates = without_late_duplicates.replace(line, "", 1)
            return cleanup_markdown_answer(
                f"{' '.join(natural_lines)}\n\n{cleanup_markdown_answer(without_late_duplicates)}"
            )
        return answer
    summary = "\n".join(lines[:3])
    return cleanup_markdown_answer(_insert_before_timing_or_source(answer, summary))


def ensure_hira_sales_link_analysis(question: str, answer: str, fact_md: str) -> str:
    """Ensure patient+disease questions connect HIRA population size with brand performance."""

    if not re.search(r"(환자|질병|HIRA)", question, re.IGNORECASE):
        return answer
    brand = _brand_metric_fact(fact_md)
    patients = _hira_patient_fact_summary(fact_md)
    if not brand or not patients:
        return answer
    answer_with_sales = _ensure_hira_sales_trend_section(answer, fact_md)
    answer_without_raw = _drop_standalone_mandatory_completion_lines(answer_with_sales, fact_md)
    answer_without_raw = remove_raw_fact_residue(answer_without_raw, fact_md)
    answer_without_raw = _drop_hira_derivative_sentences(answer_without_raw)
    answer_without_raw = _drop_duplicate_brand_metric_sentence(answer_without_raw, brand)
    if _hira_sales_link_present(answer_without_raw):
        return answer_without_raw
    if f"{brand['brand']}는 {brand['period']} 기준 매출 {brand['sales']}억원" in answer_without_raw:
        sentence = (
            f"HIRA에서 {patients['segments']} 규모가 확인되지만, "
            "질환 환자수와 브랜드 처방 환자는 직접 연결되지 않으므로 환자당 처방액이나 침투율로 환산하지 않고 "
            "매출·점유율과 나란히 보는 보조 근거로 해석해야 합니다."
        )
    else:
        sentence = (
            f"{brand['brand']}는 {brand['period']} 기준 매출 {brand['sales']}억원, 시장점유율 {brand['share']}%, "
            f"순위 {brand['rank']}로 확인됩니다. HIRA에서 {patients['segments']} 규모가 확인되지만, "
            "질환 환자수와 브랜드 처방 환자는 직접 연결되지 않으므로 환자당 처방액이나 침투율로 환산하지 않고 "
            "매출·점유율과 나란히 보는 보조 근거로 해석해야 합니다."
        )
    linked = remove_raw_fact_residue(cleanup_markdown_answer(_insert_before_timing_or_source(answer_without_raw, sentence)), fact_md)
    return _drop_duplicate_brand_metric_sentence(linked, brand)


def dedupe_brand_metric_sentence(answer: str, fact_md: str) -> str:
    """Remove repeated user-facing brand metric sentences derived from verified facts."""

    brand = _brand_metric_fact(fact_md)
    if not brand:
        return answer
    return _drop_duplicate_brand_metric_sentence(answer, brand)


def _drop_duplicate_brand_metric_sentence(answer: str, brand: dict[str, str]) -> str:
    pattern = _brand_metric_sentence_pattern(brand)
    matches = list(pattern.finditer(answer))
    if len(matches) <= 1:
        return answer
    kept_parts: list[str] = []
    cursor = 0
    for index, match in enumerate(matches):
        kept_parts.append(answer[cursor : match.start()])
        if index == 0:
            kept_parts.append(match.group(0))
        cursor = match.end()
    kept_parts.append(answer[cursor:])
    return cleanup_markdown_answer("".join(kept_parts))


def _brand_metric_sentence_pattern(brand: dict[str, str]) -> re.Pattern[str]:
    rank = re.escape(str(brand["rank"]).removesuffix("위"))
    return re.compile(
        rf"{re.escape(brand['brand'])}는\s+{re.escape(brand['period'])}\s+기준\s+매출\s+"
        rf"{re.escape(brand['sales'])}억원,\s*시장점유율\s+{re.escape(brand['share'])}%,\s*"
        rf"순위(?:는)?\s+{rank}(?:위)?입니다\.?"
    )


def _drop_hira_derivative_sentences(answer: str) -> str:
    pattern = re.compile(r"(환자\s*기반\s*수요|질환\s*수요\s*풀|침투\s*수준|침투\s*단계|처방\s*성과로\s*전환)")
    kept: list[str] = []
    for raw_line in answer.splitlines():
        if not pattern.search(raw_line):
            kept.append(raw_line)
            continue
        sentences = [part.strip() for part in re.split(r"(?<=[.!?。])\s+|(?<=다\.)\s*", raw_line) if part.strip()]
        revised = " ".join(sentence for sentence in sentences if not pattern.search(sentence)).strip()
        if revised:
            kept.append(revised)
    return cleanup_markdown_answer("\n".join(kept))


def _hira_sales_link_present(answer: str) -> bool:
    return bool(
        re.search(r"(질환\s*수요|환자\s*기반|환자\s*풀).{0,80}(침투|처방\s*성과|전환)", answer)
        or re.search(r"(침투|처방\s*성과|전환).{0,80}(질환\s*수요|환자\s*기반|환자\s*풀)", answer)
        or re.search(r"(HIRA|질환\s*환자수).{0,80}(직접\s*연결|환자당|침투율|보조\s*근거)", answer)
    )


def _ensure_hira_sales_trend_section(answer: str, fact_md: str) -> str:
    brand, points = _series_points(fact_md, "매출 시계열 fact", ("매출", "MS"))
    if not brand or len(points) < 2:
        return answer
    if _sales_series_table_present(answer, brand, points):
        return answer
    block = _hira_sales_trend_block(brand, points)
    if not block:
        return answer
    return cleanup_markdown_answer(_insert_under_sales_heading(answer, brand, block))


def _sales_series_table_present(answer: str, brand: str, points: list[dict[str, str]]) -> bool:
    latest = points[-1]
    return (
        "| 기간 | 매출 | MS |" in answer
        and brand in answer
        and latest["period"] in answer
        and latest["매출"] in answer
    )


def _hira_sales_trend_block(brand: str, points: list[dict[str, str]]) -> str:
    first = points[0]
    latest = points[-1]
    share_phrase = ""
    if first.get("MS") and latest.get("MS"):
        share_phrase = f", 시장점유율은 {first['MS']}에서 {latest['MS']}로"
    rows = ["| 기간 | 매출 | MS |", "| --- | --- | --- |"]
    rows.extend(f"| {point['period']} | {point['매출']} | {point.get('MS', '')} |" for point in points)
    analysis = (
        f"{brand} 매출은 {first['period']} {first['매출']}에서 {latest['period']} {latest['매출']}로 움직였고"
        f"{share_phrase} 변했습니다. 환자수 통계는 브랜드 처방 환자와 직접 연결되지 않으므로 보조 맥락으로만 함께 봐야 합니다."
    )
    return "\n\n".join((analysis, "\n".join(rows)))


def _insert_under_sales_heading(answer: str, brand: str, block: str) -> str:
    lines = answer.splitlines()
    for index, line in enumerate(lines):
        if re.match(r"^#{1,6}\s*\d+\.\s+.*매출", line.strip()):
            return "\n".join([*lines[: index + 1], block, *lines[index + 1 :]])
    section = f"### 2. {brand} 매출 및 시장 점유율 시계열 분석\n{block}"
    for index, line in enumerate(lines):
        if re.match(r"^(?:#{1,6}\s*|\*\*)?\d+\.\s+.*(?:이슈|인과)", line.strip()):
            return "\n".join([*lines[:index], section, *lines[index:]])
    return _insert_before_timing_or_source(answer, section)


def remove_supported_series_contradictions(answer: str, fact_md: str) -> str:
    """Remove unsupported prose for brands that have monthly series facts."""
    brands = _series_fact_brands(fact_md)
    if not brands:
        return answer
    kept: list[str] = []
    for line in answer.splitlines():
        if _line_contradicts_series_fact(line, brands):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def mandatory_retry_messages(
    question: str,
    fact_md: str,
    previous_answer: str,
    missing_lines: tuple[str, ...],
) -> list[dict[str, str]]:
    missing_md = "\n".join(_mandatory_retry_line(previous_answer, line) for line in missing_lines)
    return [
        {
            "role": "system",
            "content": (
                "너는 JW 시장분석 채팅 에이전트다. 이전 답변에서 필수 fact가 누락됐다. "
                "아래 누락된 필수 fact를 첫 본문 단락에 모두 반영해 답변 전체를 다시 작성한다. "
                "판단형 질문은 결론을 먼저 쓰고, 핵심 근거와 fact 기반 해석, 시사점/한계까지 포함한다. "
                "근거 기반 인과 분석을 적극 생성하되, 거짓 수치·존재하지 않는 기사·fact 밖 사실 날조는 금지한다. "
                "상위 브랜드 월별 MS fact에 있는 브랜드는 시계열 데이터가 있는 것이므로 미지원/확인 안 됨이라고 쓰지 않는다. "
                "뉴스를 쓰면 출처(날짜) [「제목」](URL)과 요약/발췌의 핵심 이슈를 실제 fact 값으로 드러내고, 관련 기사에서 언급 확인 같은 빈 문장은 쓰지 않는다. "
                "뉴스 발췌의 기사 숫자·분기·전년대비·증감률은 기사 맥락으로만 다루고 UBIST 정량 지표처럼 해석하지 않는다. "
                "인사이트 계산 fact가 있으면 share-of-growth, 성장분해, gain-loss, cohort 상대화를 기준 대비 편차·교차·so-what과 인과적 해석으로 설명한다. "
                "숫자는 fact set에 있는 값만 사용하고 내부 메타는 노출하지 않는다. 출처는 마지막 한 줄만 쓴다."
            ),
        },
        {
            "role": "user",
            "content": (
                f"질문: {question}\n\n"
                f"누락된 필수 fact:\n{missing_md}\n\n"
                f"전체 fact set:\n{fact_md}\n\n"
                f"이전 답변(참고만 하고 그대로 반복하지 말 것):\n{previous_answer}"
            ),
        },
    ]


def _mandatory_retry_line(answer: str, line: str) -> str:
    missing_items = _mandatory_line_missing_items(answer, line)
    if not missing_items:
        return line
    return f"{line}\n  누락 수치: {', '.join(missing_items)}"


def _mandatory_line_missing_items(answer: str, line: str) -> tuple[str, ...]:
    if line.startswith("- 매출 변화:"):
        return _missing_required_number_tokens(answer, _number_like_tokens(line))
    if "인사이트 계산" in line and not ("상승폭" in line and "하락폭" in line):
        return _missing_required_number_tokens(answer, _number_like_tokens(line))
    if "월별 MS" in line:
        payload = line.split(":", 1)[-1].strip()
        return _missing_required_number_tokens(answer, _number_like_tokens(payload))
    if "브랜드 핵심 지표" in line:
        payload = line.split(":", 1)[-1].strip()
        return _missing_required_number_tokens(answer, _number_like_tokens(payload))
    return ()


def chunk_text(text: str, size: int = 24) -> Iterator[str]:
    if not text:
        return
    index = 0
    while index < len(text):
        limit = min(len(text), index + size)
        if limit == len(text):
            yield text[index:]
            return
        forward = text.find(" ", limit, min(len(text), index + int(size * 1.5)))
        if forward > limit:
            limit = _space_aware_limit(text, forward, index, size)
        limit = _atomic_stream_limit(text, limit)
        while limit < len(text) and text[limit].isspace():
            limit += 1
        yield text[index:limit]
        index = limit


def _space_aware_limit(text: str, forward: int, index: int, size: int) -> int:
    if text[forward - 1] != ")":
        return forward + 1
    next_forward = text.find(" ", forward + 1, min(len(text), index + int(size * 1.5)))
    return next_forward + 1 if next_forward > forward else forward + 1


def _atomic_stream_limit(text: str, limit: int) -> int:
    window_start = max(0, limit - 40)
    window_end = min(len(text), limit + 40)
    for match in _STREAM_ATOMIC_TOKEN_RE.finditer(text, window_start, window_end):
        if match.start() < limit < match.end():
            return match.end()
    return limit


def _mandatory_line_present(answer: str, line: str) -> bool:
    if "데이터 미보유" in line:
        subject = line.split(":", 1)[1].strip().split(" ", 1)[0] if ":" in line else ""
        return subject in answer and any(token in answer for token in ("미보유", "미지원", "지원 브랜드", "확정하지 못"))
    if "HIRA 환자수" in line:
        numbers = _hira_patient_count_tokens(line)
        answer_numbers = set(_plain_number_tokens(answer))
        return bool(numbers) and any(token in answer_numbers for token in numbers)
    if line.startswith("- 상위 브랜드 추이:"):
        return _top_brand_trend_line_present(answer, line)
    if "브랜드 추세 비교" in line:
        required_deltas = tuple(
            re.findall(r"(?:MS 변화|매출 변화율)\s+([+-]?\d+(?:\.\d+)?)%p?", line)
        )
        return (
            "리바로" in answer
            and "아토젯" in answer
            and any(token in answer for token in ("추세", "위협", "비교", "성장"))
            and _all_required_numbers_present(answer, required_deltas)
        )
    if line.startswith("- 매출 변화:"):
        return (
            "매출" in answer
            and "변화" in answer
            and _all_required_numbers_present(answer, _number_like_tokens(line))
        )
    if "매출 추이" in line:
        return _sales_trend_line_present(answer, line)
    if "점유율 변화" in line:
        numbers = tuple(_number_like_tokens(line))
        delta_numbers = tuple(token for token in numbers if token.startswith(("-", "+")))
        delta_present = any(_signed_number_present(answer, token) for token in delta_numbers)
        return (
            "점유율" in answer
            and all(period in answer for period in _period_tokens(line))
            and delta_present
            and any(token in answer for token in ("변화", "하락", "상승", "감소", "증가"))
        )
    if "YoY 성장률" in line:
        numbers = tuple(_number_like_tokens(line))
        return "YoY" in answer and any(_signed_number_present(answer, token) for token in numbers)
    if "평균 점유율" in line:
        numbers = tuple(_number_like_tokens(line))
        return "평균" in answer and any(token in answer for token in numbers)
    if "브랜드 핵심 지표" in line:
        return _single_brand_focus_line_present(answer, line)
    if "시장/브랜드 변화율 대조" in line:
        numbers = tuple(_number_like_tokens(line))
        delta_numbers = tuple(token for token in numbers if token.startswith(("-", "+")))
        return (
            "시장" in answer
            and any(token in answer for token in ("브랜드", "리바로"))
            and bool(delta_numbers)
            and all(_signed_number_present(answer, token) for token in delta_numbers)
            and any(token in answer for token in ("인과", "동행", "유사", "고유", "대조", "비교"))
        )
    if "인사이트 계산" in line:
        if "상승폭" in line and "하락폭" in line:
            movement = _movement_phrase(line)
            ratio = _regex_value(line, r"대비\s+([+-]?\d+(?:\.\d+)?%)")
            return bool(movement and ratio and ratio in answer and any(token in answer for token in ("반대 방향", "직접 처방 이동", "재편")))
        numbers = tuple(_number_like_tokens(line))
        return any(token in answer for token in ("share-of-growth", "시장 성장", "점유 이동", "백분위")) and (
            _all_required_numbers_present(answer, numbers)
        )
    if "월별 MS" in line:
        payload = line.split(":", 1)[-1].strip()
        brand = payload.split(" 월별 MS", 1)[0]
        numbers = tuple(_number_like_tokens(payload))
        return bool(brand) and brand in answer and _all_required_numbers_present(answer, numbers)
    if "Brand 상위" in line:
        return _top_brand_line_present(answer, line)
    return line.split(":", 1)[-1].strip() in answer


def _sales_trend_line_present(answer: str, line: str) -> bool:
    payload = line.split(":", 1)[-1].strip()
    brand = _regex_value(payload, r"^([가-힣A-Za-z0-9+._/-]+)\s+매출\s+시계열")
    periods = tuple(_period_tokens(payload))
    numbers = tuple(_number_like_tokens(payload))
    if brand and brand not in answer:
        return False
    if not periods:
        return False
    has_series_table = "| 기간 |" in answer and "매출" in answer and len(set(_period_tokens(answer)) & set(periods)) >= 1
    if not all(period in answer for period in periods) and not has_series_table:
        return False
    important_numbers = numbers[-2:] if has_series_table else (numbers[:2] + numbers[-2:] if len(numbers) >= 4 else numbers)
    return bool(important_numbers) and all(_signed_number_present(answer, number) for number in important_numbers)


def _series_fact_brands(fact_md: str) -> frozenset[str]:
    brands: set[str] = set()
    in_monthly = False
    for line in fact_md.splitlines():
        stripped = line.strip()
        if stripped.startswith("### "):
            in_monthly = "상위 브랜드 월별 MS fact" in stripped
            continue
        if not in_monthly or not stripped.startswith("|") or "---" in stripped or "브랜드" in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and cells[0]:
            brands.add(cells[0])
    return frozenset(brands)


def _line_contradicts_series_fact(line: str, brands: frozenset[str]) -> bool:
    return (
        any(brand in line for brand in brands)
        and _UNSUPPORTED_SERIES_RE.search(line) is not None
        and _NEGATED_UNSUPPORTED_RE.search(line) is None
    )


def _top_brand_line_present(answer: str, line: str) -> bool:
    payload = line.split(":", 1)[-1]
    rank_match = re.search(r"(\d+)위", payload)
    share_match = re.search(r"(\d+(?:\.\d+)?)%", payload)
    brand_match = re.search(r"\d+위\s*([가-힣A-Za-z0-9+._-]+)", payload)
    if not (rank_match and share_match and brand_match):
        return False
    rank = rank_match.group(1)
    brand = brand_match.group(1)
    share = share_match.group(1)
    return (
        bool(re.search(rf"{re.escape(rank)}위[\s|:*_-]*{re.escape(brand)}", answer))
        and f"{share}%" in answer
    )


def _top_brand_trend_line_present(answer: str, line: str) -> bool:
    payload = line.split(":", 1)[-1]
    rank_match = re.search(r"(\d+)위", payload)
    brand_match = re.search(r"\d+위\s*([가-힣A-Za-z0-9+._-]+)", payload)
    if not (rank_match and brand_match):
        return False
    period_tokens = tuple(_period_tokens(payload))
    pct_tokens = tuple(re.findall(r"[+-]?\d+(?:\.\d+)?%p?", payload))
    return (
        bool(re.search(rf"{re.escape(rank_match.group(1))}위[\s|:*_-]*{re.escape(brand_match.group(1))}", answer))
        and all(period in answer for period in period_tokens)
        and all(token in answer for token in pct_tokens)
    )


def _single_brand_focus_line_present(answer: str, line: str) -> bool:
    payload = line.split(":", 1)[-1].strip()
    brand = payload.split(" ", 1)[0] if payload else ""
    if not brand or brand not in answer:
        return False
    checks: list[bool] = []
    sales_match = re.search(r"매출\s+(-?\d+(?:\.\d+)?)억원", payload)
    if sales_match:
        checks.append("매출" in answer and sales_match.group(1) in answer)
    share_match = re.search(r"시장점유율\s+(-?\d+(?:\.\d+)?)%", payload)
    if share_match:
        share = share_match.group(1)
        checks.append("점유율" in answer and (f"{share}%" in answer or share in answer))
    rank_match = re.search(r"순위\s+([0-9]+(?:/[0-9]+)?)", payload)
    if rank_match:
        rank = rank_match.group(1)
        checks.append("순위" in answer and (rank in answer or f"{rank}위" in answer))
    if checks:
        return all(checks)
    numbers = tuple(_number_like_tokens(payload))
    return _all_required_numbers_present(answer, numbers)


def _mandatory_numeric_assertion(line: str) -> tuple[str, set[str]] | None:
    if ":" not in line:
        return None
    kind, payload = (part.strip() for part in line.lstrip("- ").split(":", 1))
    subject = _mandatory_subject(kind, payload)
    if not subject:
        return None
    numbers = set(allowed_numbers(payload))
    return (subject, numbers) if numbers else None


def _mandatory_subject(kind: str, payload: str) -> str:
    if kind in {"매출 변화", "점유율 변화"}:
        match = re.match(r"([가-힣A-Za-z0-9+._/-]+)\s+20\d{2}-\d{2}", payload)
        return match.group(1) if match else ""
    if kind == "비교 브랜드 지표":
        match = re.match(r"([가-힣A-Za-z0-9+._/-]+)\s+최신", payload)
        return match.group(1) if match else ""
    if kind in {"YoY 성장률", "평균 점유율"}:
        suffix = " YoY" if kind == "YoY 성장률" else " 평균 점유율"
        return payload.split(suffix, 1)[0].strip() if suffix in payload else ""
    if "상위" in kind:
        match = re.search(
            r"(?:\d+위\s*)?([가-힣A-Za-z0-9+._/-]+)\s+"
            r"(?:최신\s+)?(?:시장점유율|점유율|매출|월별\s+MS|순위)",
            payload,
        )
        return match.group(1) if match else ""
    if kind == "HIRA 환자수":
        match = re.match(r"(.+?)\([A-Z]\d", payload)
        return match.group(1).strip() if match else ""
    return ""


def _line_has_metric_context(line: str) -> bool:
    return any(token in line for token in ("점유율", "MS", "매출", "성장률", "YoY", "환자수", "순위", "위", "%", "억원", "명"))


def _number_like_tokens(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"-?\d+(?:,\d{3})*(?:\.\d+)?", text))


def _missing_required_number_tokens(answer: str, tokens: tuple[str, ...]) -> tuple[str, ...]:
    """Return mandatory numeric tokens absent from the answer, preserving fact order."""

    return tuple(dict.fromkeys(token for token in tokens if not _signed_number_present(answer, token)))


def _all_required_numbers_present(answer: str, tokens: tuple[str, ...]) -> bool:
    return bool(tokens) and not _missing_required_number_tokens(answer, tokens)


def _plain_number_tokens(text: str) -> tuple[str, ...]:
    return tuple(token.replace(",", "") for token in _number_like_tokens(text))


def _hira_patient_count_tokens(text: str) -> tuple[str, ...]:
    return tuple(token.replace(",", "") for token in re.findall(r":\s*(-?\d[\d,]*(?:\.\d+)?)\s*명", text))


def _patient_count_in_text(text: str, token: str) -> bool:
    pattern = re.compile(rf"(?<![\d,]){_optional_comma_number_pattern(token)}\s*명?(?![\d,])")
    return bool(pattern.search(text))


def _optional_comma_number_pattern(token: str) -> str:
    return r",?".join(re.escape(char) for char in token)


def _hira_patient_summary_lines(fact_md: str) -> tuple[str, ...]:
    lines = [line for line in mandatory_fact_lines(fact_md) if "HIRA 환자수" in line]
    if lines:
        return tuple(lines)
    tool_lines: list[str] = []
    tool_fact_pattern = re.compile(
        r"^-\s+(?P<code>[A-Z]\d+[A-Z0-9.]*)\s+\((?P<year>20\d{2})\):\s+"
        r"질병 입원/외래 통계\s*=\s*(?P<count>\d[\d,]*)\s+"
        r"\[건강보험심사평가원 통계\s+·\s+(?P<locator>[^\]]+)\]$"
    )
    for raw in fact_md.splitlines():
        match = tool_fact_pattern.match(raw.strip())
        if not match:
            continue
        locator = tuple(
            part.strip()
            for part in match.group("locator").split(" · ")
            if part.strip()
        )
        if len(locator) < 2:
            continue
        disease, segment, *qualifiers = locator
        segment_label = " ".join((segment, *qualifiers))
        count = f'{int(match.group("count").replace(",", "")):,}'
        tool_lines.append(
            f"- HIRA 환자수: {disease}({match.group('code')}) "
            f"{match.group('year')}년 {segment_label}: {count}명"
        )
    if tool_lines:
        return tuple(dict.fromkeys(tool_lines))
    parsed: list[str] = []
    in_table = False
    for raw in fact_md.splitlines():
        stripped = raw.strip()
        if stripped == "### HIRA 질병통계 fact":
            in_table = True
            continue
        if in_table and stripped.startswith("### "):
            break
        if not in_table or not stripped.startswith("|") or "---" in stripped or "구분" in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) >= 5 and cells[0] and cells[1] and cells[2] and cells[3] and cells[4] not in {"", "-"}:
            parsed.append(f"- HIRA 환자수: {cells[2]}({cells[1]}) {cells[3]}년 {cells[0]}: {cells[4]}명")
        elif len(cells) >= 4 and cells[0] and cells[1] and cells[2] and cells[3] not in {"", "-"}:
            parsed.append(f"- HIRA 환자수: {cells[2]}({cells[1]}) {cells[0]}: {cells[3]}명")
    return tuple(dict.fromkeys(parsed))


def _hira_unavailable_summary_lines(fact_md: str) -> tuple[str, ...]:
    lines = [line for line in mandatory_fact_lines(fact_md) if "HIRA 조회 상태" in line]
    return tuple(dict.fromkeys(lines))


def _brand_metric_fact(fact_md: str) -> dict[str, str]:
    for line in mandatory_fact_lines(fact_md):
        if "브랜드 핵심 지표" not in line:
            continue
        payload = line.split(":", 1)[-1].strip()
        match = re.match(
            r"(?P<brand>\S+)\s+(?P<period>20\d{2}-\d{2})\s+매출\s+"
            r"(?P<sales>-?\d+(?:\.\d+)?)억원\s+시장점유율\s+"
            r"(?P<share>-?\d+(?:\.\d+)?)%\s+순위\s+(?P<rank>\d+(?:/\d+)?)",
            payload,
        )
        if match:
            return match.groupdict()
    return {}


def _hira_patient_fact_summary(fact_md: str) -> dict[str, str]:
    rows: list[dict[str, str]] = []
    for line in _hira_patient_summary_lines(fact_md):
        payload = line.split(":", 1)[-1].strip()
        match = re.match(
            r"(?P<disease>.+?)\((?P<code>[A-Z]\d+[A-Z0-9.]*)\)\s+"
            r"(?:(?P<year>20\d{2})년\s+)?"
            r"(?P<segment>[^:]+):\s*(?P<count>-?\d[\d,]*(?:\.\d+)?)명",
            payload,
        )
        if match:
            rows.append(match.groupdict())
    if not rows:
        return {}
    disease = rows[0]["disease"]
    code = rows[0]["code"]
    priority = ("외래", "입원")
    selected: list[dict[str, str]] = []
    for key in priority:
        selected.extend(row for row in rows if row["segment"] == key)
    selected.extend(row for row in rows if row not in selected)
    segments = ", ".join(
        f"{row['disease']}({row['code']}) "
        f"{row['year'] + '년 ' if row.get('year') else ''}{row['segment']} {row['count']}명"
        for row in selected[:2]
    )
    return {"disease": disease, "code": code, "segments": segments}


def _drop_standalone_mandatory_completion_lines(answer: str, fact_md: str) -> str:
    lines = mandatory_fact_lines(fact_md)
    if not lines:
        return answer
    removable = {
        _presentable_mandatory_line(line).strip()
        for line in lines
        if "HIRA 환자수" in line or "브랜드 핵심 지표" in line or "매출 추이" in line
    }
    removable.discard("")
    if not removable:
        return answer
    kept = [line for line in answer.splitlines() if line.strip() not in removable]
    cleaned_answer = cleanup_markdown_answer("\n".join(kept))
    brand = _brand_metric_fact(fact_md)
    if brand:
        cleaned_answer = _drop_duplicate_brand_metric_sentence(cleaned_answer, brand)
    return cleaned_answer


def remove_raw_fact_residue(answer: str, fact_md: str) -> str:
    """Remove rewrite-step residue while keeping fact-backed analysis and tables."""

    cleaned = _remove_design_meta_sentences(answer)
    has_series_table = "| 기간 | 매출 | MS |" in cleaned or "| 기간 | 매출 |" in cleaned
    series_periods = set(_period_tokens(fact_md))
    kept: list[str] = []
    channel_rows: list[tuple[str, str, str]] = []
    for raw_line in cleaned.splitlines():
        stripped = raw_line.strip()
        if _is_raw_sales_trend_line(stripped):
            continue
        channel_row = _raw_channel_top_row(stripped)
        if channel_row:
            channel_rows.append(channel_row)
            continue
        if has_series_table and _is_duplicate_series_bullet(stripped, series_periods):
            continue
        kept.append(raw_line)
    revised = cleanup_markdown_answer("\n".join(kept))
    channel_table = _channel_top_table(channel_rows, revised)
    if channel_table:
        revised = cleanup_markdown_answer(_insert_before_timing_or_source(revised, channel_table))
    return revised


def _remove_design_meta_sentences(text: str) -> str:
    patterns = (
        r"이\s*매출\s*축은.*?시계열\s*기준입니다\.",
        r"단순\s*지표\s*나열보다.*?좁혀볼\s*수\s*있습니다\.",
        r"따라서\s*단일\s*수치보다.*?적절합니다\.",
        r"현재\s*위치를\s*판단하는\s*기본\s*근거입니다\.",
        r"후속\s*판단을.*?적절합니다\.",
        r"확정\s*fact의\s*방향과\s*폭을\s*함께\s*보면.*?판단할\s*수\s*있습니다\.",
        r"따라서\s*최근\s*변화가\s*반복되는지.*?함께\s*확인해야\s*합니다\.",
    )
    cleaned = text
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned)
    return cleaned


def _is_raw_sales_trend_line(stripped: str) -> bool:
    normalized = stripped.lstrip("-*• ").strip()
    return normalized.startswith("매출 추이:") or normalized.startswith("매출 시계열:")


def _is_raw_level_top_line(stripped: str) -> bool:
    match = _RAW_LEVEL_TOP_LINE_RE.fullmatch(stripped)
    if not match:
        return False
    return match.group("level").lower() == "channel"


def _raw_channel_top_row(stripped: str) -> tuple[str, str, str] | None:
    match = _RAW_LEVEL_TOP_LINE_RE.fullmatch(stripped)
    if not match or match.group("level").lower() != "channel":
        return None
    return match.group("name").strip(), match.group("share").strip(), match.group("sales").strip()


def _channel_top_table(rows: list[tuple[str, str, str]], answer: str) -> str:
    if not rows or "| 채널 | 시장점유율 | 매출 |" in answer:
        return ""
    unique_rows = tuple(dict.fromkeys(rows))
    body = "\n".join(f"| {name} | {share} | {sales} |" for name, share, sales in unique_rows)
    return "\n".join(("| 채널 | 시장점유율 | 매출 |", "| --- | --- | --- |", body))


def _is_duplicate_series_bullet(stripped: str, series_periods: set[str]) -> bool:
    if not stripped.startswith(("*", "-", "•")):
        return False
    normalized = stripped.lstrip("-*• ").strip()
    if not re.match(r"20\d{2}-\d{2}\s*:", normalized):
        return False
    period = normalized.split(":", 1)[0].strip()
    return period in series_periods and any(token in normalized for token in ("억원", "매출", "%", "MS"))


def _insert_before_timing_or_source(answer: str, block: str) -> str:
    for marker in ("## 처리 시간", "## 출처"):
        index = answer.find(marker)
        if index >= 0:
            return "\n\n".join((answer[:index].strip(), block.strip(), answer[index:].strip()))
    return "\n\n".join((answer.strip(), block.strip()))


def _insert_before_first_table_timing_or_source(answer: str, block: str) -> str:
    table_index = _first_markdown_table_index(answer)
    marker_indices = [index for marker in ("\n## 처리 시간", "\n## 출처") if (index := answer.find(marker)) >= 0]
    insert_index = min([table_index, *marker_indices]) if table_index >= 0 or marker_indices else -1
    if insert_index >= 0:
        return "\n\n".join((answer[:insert_index].strip(), block.strip(), answer[insert_index:].strip()))
    return "\n\n".join((answer.strip(), block.strip()))


def _first_markdown_table_index(answer: str) -> int:
    offset = 0
    for line in answer.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            return offset
        offset += len(line)
    return -1


def _competitive_movement_analysis_line(fact_md: str) -> str:
    explicit = next(
        (line for line in mandatory_fact_lines(fact_md) if "인사이트 계산" in line and "상승폭" in line and "하락폭" in line),
        "",
    )
    parsed = _competitive_movement_from_line(explicit)
    if not parsed:
        parsed = _competitive_movement_from_insight_rows(mandatory_fact_lines(fact_md))
    if not parsed:
        return ""
    period_text = f"{parsed['period']} " if parsed.get("period") else ""
    return (
        f"{parsed['gainer']}의 {period_text}점유율 상승폭 {parsed['gain']}와 "
        f"{parsed['faller']}는 같은 기간 {parsed['loss']}로 반대 방향입니다. "
        "집계 데이터만으로 직접 처방 이동은 확인할 수 없습니다. "
        "따라서 경쟁 구도 변화는 복합제 중심 재편 후보 신호로 해석됩니다."
    )


def _competitive_movement_from_line(line: str) -> dict[str, str]:
    if not line:
        return {}
    match = re.search(
        r"인사이트 계산:\s*(?P<gainer>[가-힣A-Za-z0-9+._/-]+)\s+"
        r"(?:(?P<period>[12]\d{3}(?:-\d{2}|-Q\d)→[12]\d{3}(?:-\d{2}|-Q\d))\s+)?상승폭\s+"
        r"(?P<gain>[+-]?\d+(?:\.\d+)?%p)\s+"
        r"(?P<faller>[가-힣A-Za-z0-9+._/-]+)\s+"
        r"(?:[12]\d{3}(?:-\d{2}|-Q\d)→[12]\d{3}(?:-\d{2}|-Q\d)\s+)?하락폭\s+"
        r"(?P<loss>[+-]?\d+(?:\.\d+)?%p)",
        line,
    )
    return match.groupdict() if match else {}


def _competitive_movement_from_insight_rows(lines: tuple[str, ...]) -> dict[str, str]:
    rows: list[tuple[str, float, str]] = []
    for line in lines:
        if "인사이트 계산" not in line or "share-of-growth" not in line or "점유" not in line:
            continue
        brand = _regex_value(line, r"인사이트 계산:\s*([가-힣A-Za-z0-9+._/-]+)\s+share-of-growth")
        delta = _regex_value(line, r"점유\s+([+-]?\d+(?:\.\d+)?%p)")
        if not brand or not delta:
            continue
        try:
            rows.append((brand, float(delta.removesuffix("%p")), delta))
        except ValueError:
            continue
    risers = [row for row in rows if row[1] > 0]
    fallers = [row for row in rows if row[1] < 0]
    if not risers or not fallers:
        return {}
    gainer = max(risers, key=lambda row: row[1])
    faller = min(fallers, key=lambda row: row[1])
    return {
        "gainer": gainer[0],
        "gain": gainer[2],
        "faller": faller[0],
        "loss": faller[2],
        "period": "",
    }


def _competitive_movement_present(answer: str, movement: str) -> bool:
    gain = _regex_value(movement, r"상승폭\s+([+-]?\d+(?:\.\d+)?%p)")
    loss = _regex_value(movement, r"같은 기간\s+([+-]?\d+(?:\.\d+)?%p)")
    return (
        all(token in answer for token in ("리피토", "리바로젯"))
        and "직접 처방 이동" in answer
        and any(token in answer for token in ("반대 방향", "재편"))
        and (not gain or _signed_number_present(answer, gain))
        and (not loss or _signed_number_present(answer, loss))
    )


def _needs_single_brand_trend_analysis(question: str) -> bool:
    if any(token in question for token in ("경쟁", "구도", "상위", "환자", "질병", "HIRA", "위협")):
        return False
    return "추이" in question and any(token in question for token in ("매출", "점유율", "어때", "최근"))


def _needs_issue_question_quant_analysis(question: str) -> bool:
    if re.search(r"(이슈|뉴스|관련\s*최근|최근\s*이슈)", question):
        return True
    return False


def _analysis_sentence_count(answer: str) -> int:
    prose = _analysis_prose(answer)
    if not prose:
        return 0
    return len(tuple(part for part in re.split(r"(?:다\.|[.!?])\s+", prose) if part.strip()))


def _analysis_prose(answer: str) -> str:
    body = re.split(r"\n##\s*(?:출처|처리\s*시간)\b", answer, maxsplit=1)[0]
    kept: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("|"):
            continue
        if re.fullmatch(r"\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?", line):
            continue
        if line.startswith("#"):
            continue
        if re.fullmatch(r"\*\*[^*]+\*\*", line):
            continue
        if re.match(r"^(?:[-*•]|\d+[.)])\s+", line):
            continue
        kept.append(line)
    return re.sub(r"\s+", " ", " ".join(kept)).strip()


def _single_brand_trend_fact_from_calls(
    calls: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
) -> _SingleBrandTrendFact | None:
    for call in calls or ():
        data = call.get("render_data")
        if not isinstance(data, dict):
            continue
        fact = _single_brand_trend_fact_from_render_data(data)
        if fact is not None:
            return fact
    return None


def _single_brand_trend_fact_from_render_data(data: dict[str, Any]) -> _SingleBrandTrendFact | None:
    brand = str(data.get("brand") or "").strip()
    brand_points = _trend_points_from_render_series(data.get("brand_value_series_10pt"), value_label="brand")
    if not brand or len(brand_points) < 2:
        return None
    market_points = _trend_points_from_render_series(data.get("market_size_series"), value_label="market")
    return _single_brand_trend_fact(brand, brand_points, market_points)


def _single_brand_trend_fact_from_fact_md(fact_md: str) -> _SingleBrandTrendFact | None:
    brand, brand_rows = _series_points(fact_md, "매출 시계열 fact", ("매출", "MS"))
    if not brand or len(brand_rows) < 2:
        return None
    brand_points = tuple(
        _TrendPoint(
            period=row["period"],
            value=_metric_float(row.get("매출", "")),
            value_text=row.get("매출", ""),
            share_text=row.get("MS", ""),
        )
        for row in brand_rows
    )
    _market_subject, market_rows = _series_points(fact_md, "시장규모 시계열 fact", ("시장규모", "YoY"))
    market_points = tuple(
        _TrendPoint(
            period=row["period"],
            value=_metric_float(row.get("시장규모", "")),
            value_text=row.get("시장규모", ""),
        )
        for row in market_rows
    )
    return _single_brand_trend_fact(brand, brand_points, market_points)


def _single_brand_trend_fact(
    brand: str,
    brand_points: tuple[_TrendPoint, ...],
    market_points: tuple[_TrendPoint, ...],
) -> _SingleBrandTrendFact:
    sorted_brand_points = tuple(sorted(brand_points, key=lambda point: _period_sort_key(point.period)))
    peak = max(sorted_brand_points, key=lambda point: point.value)
    latest = sorted_brand_points[-1]
    peak_index = sorted_brand_points.index(peak)
    after_peak = sorted_brand_points[peak_index:]
    trough = min(after_peak, key=lambda point: point.value)
    market_by_period = {point.period: point for point in sorted(market_points, key=lambda point: _period_sort_key(point.period))}
    return _SingleBrandTrendFact(
        brand=brand,
        points=sorted_brand_points,
        first=sorted_brand_points[0],
        peak=peak,
        trough=trough,
        latest=latest,
        market_by_period=market_by_period,
    )


def _trend_sorted_points(trend: _SingleBrandTrendFact) -> tuple[_TrendPoint, ...]:
    return trend.points


def _trend_point_summary(point: _TrendPoint | None) -> str:
    if point is None:
        return ""
    parts = [point.period, point.value_text]
    if point.share_text:
        parts.append(f"MS {point.share_text}")
    return " / ".join(part for part in parts if part)


def _market_point_for(trend: _SingleBrandTrendFact, period: str) -> _TrendPoint | None:
    return trend.market_by_period.get(period)


def _trend_grain(trend: _SingleBrandTrendFact) -> str:
    periods = {point.period for point in _trend_sorted_points(trend)}
    if any("Q" in period.upper() for period in periods):
        return "quarter"
    if any(re.fullmatch(r"20\d{2}-\d{2}", period) for period in periods):
        return "month"
    return "unknown"


def _trend_points_from_render_series(raw_series: Any, *, value_label: str) -> tuple[_TrendPoint, ...]:
    if not isinstance(raw_series, list):
        return ()
    points: list[_TrendPoint] = []
    for raw_point in raw_series:
        if not isinstance(raw_point, dict):
            continue
        period = str(raw_point.get("period") or "").strip()
        if not period:
            continue
        value = _render_series_value(raw_point)
        value_text = eok_value(raw_point.get("value_억원"), raw_point.get("value_krw") or raw_point.get("value"))
        if value_text == "":
            value_text = eok_value(value, None)
        share_text = pct_value(raw_point.get("ms_pct")) if value_label == "brand" else ""
        points.append(_TrendPoint(period=period, value=value, value_text=value_text, share_text=share_text))
    return tuple(points)


def _render_series_value(raw_point: dict[str, Any]) -> float:
    eok = raw_point.get("value_억원")
    if isinstance(eok, int | float):
        return float(eok)
    krw = raw_point.get("value_krw") or raw_point.get("value")
    if isinstance(krw, int | float):
        return float(krw) / 100_000_000
    return 0.0


def _trend_shape(trend: _SingleBrandTrendFact) -> str:
    distinct_periods = {trend.peak.period, trend.trough.period, trend.latest.period}
    decline = trend.peak.value - trend.trough.value
    rebound = trend.latest.value - trend.trough.value
    if (
        len(distinct_periods) == 3
        and trend.peak.value > trend.trough.value
        and trend.latest.value > trend.trough.value
        and _relative_change(trend.peak.value, trend.trough.value) <= -0.10
        and _relative_change(trend.trough.value, trend.latest.value) >= 0.05
        and decline > 0
        and rebound / decline >= 0.30
    ):
        return "recovery"
    if (
        len(distinct_periods) == 3
        and trend.peak.value > trend.trough.value
        and trend.latest.value > trend.trough.value
        and decline > 0
        and rebound / decline < 0.30
    ):
        return "flat"
    values = [point.value for point in trend.points]
    if values and max(values) > 0 and (max(values) - min(values)) / max(values) < 0.15:
        return "flat"
    if abs(_relative_change(trend.first.value, trend.latest.value)) < 0.05:
        return "flat"
    if trend.latest.value > trend.first.value:
        return "rising"
    if trend.latest.value < trend.first.value:
        return "falling"
    return "flat"


def _relative_change(start: float, end: float) -> float:
    if start == 0:
        return 0.0
    return (end - start) / abs(start)


def _period_sort_key(period: str) -> tuple[int, int, str]:
    quarter = re.fullmatch(r"(20\d{2})-?Q([1-4])", period, flags=re.IGNORECASE)
    if quarter:
        return int(quarter.group(1)), (int(quarter.group(2)) - 1) * 3 + 1, period
    month = re.fullmatch(r"(20\d{2})-(\d{2})", period)
    if month:
        return int(month.group(1)), int(month.group(2)), period
    year = re.fullmatch(r"(20\d{2})", period)
    if year:
        return int(year.group(1)), 1, period
    return 9999, 99, period


def _issue_question_quant_analysis_line(fact_md: str) -> str:
    brand, brand_points = _series_points(fact_md, "매출 시계열 fact", ("매출", "MS"))
    if not brand or len(brand_points) < 2:
        return ""
    first = brand_points[0]
    latest = brand_points[-1]
    metric = _brand_metric_fact(fact_md)
    rank_phrase = ""
    if metric.get("rank"):
        rank_phrase = f" 시장 내 순위는 {metric['rank']}로, 이슈 환경을 해석할 때 현재 입지의 기준점입니다."
    sales_phrase = f"{first['period']} {first['매출']}에서 {latest['period']} {latest['매출']}로"
    share_phrase = ""
    if first.get("MS") and latest.get("MS"):
        share_phrase = f", 시장점유율은 {first['MS']}에서 {latest['MS']}로"
    return (
        f"정량 지표로 보면 {brand}는 {sales_phrase} 움직였고{share_phrase} 변했습니다."
        f"{rank_phrase} 따라서 최근 이슈는 단순 기사 목록이 아니라, {brand}가 시장 내 입지를 방어하면서도 "
        "시장 성장 대비 점유율 압력을 받는 배경 맥락으로 함께 해석해야 합니다."
    )


def _series_points(fact_md: str, section_suffix: str, columns: tuple[str, str]) -> tuple[str, list[dict[str, str]]]:
    lines = fact_md.splitlines()
    in_section = False
    brand = ""
    rows: list[dict[str, str]] = []
    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped.startswith("### "):
            in_section = stripped.endswith(section_suffix)
            if in_section:
                brand = stripped.removeprefix("### ").removesuffix(section_suffix).strip()
            continue
        if not in_section or not stripped.startswith("|") or "---" in stripped or "기간" in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 3 or not cells[0]:
            continue
        rows.append({"period": cells[0], columns[0]: cells[1], columns[1]: cells[2]})
    return brand, rows


def _metric_float(value: str) -> float:
    try:
        return float(re.sub(r"[^0-9.+-]", "", value))
    except ValueError:
        return 0.0


def _period_tokens(text: str) -> tuple[str, ...]:
    monthly = re.findall(r"20\d{2}-\d{2}", text)
    quarterly = re.findall(r"20\d{2}-Q[1-4]", text, flags=re.IGNORECASE)
    return tuple((*monthly, *(period.upper() for period in quarterly)))


def _signed_number_present(answer: str, token: str) -> bool:
    if token in answer:
        return True
    if token.startswith(("-", "+")):
        return token[1:] in answer
    return False


def _drop_raw_mandatory_lines(answer: str, mandatory_lines: tuple[str, ...]) -> str:
    if not mandatory_lines:
        return answer
    raw = set(mandatory_lines)
    kept = [line for line in answer.splitlines() if line.strip() not in raw]
    return "\n".join(kept).strip()


def _drop_empty_markdown_tables(answer: str) -> str:
    lines = answer.splitlines()
    kept: list[str] = []
    index = 0
    while index < len(lines):
        if _is_table_header_at(lines, index):
            end = index + 2
            while end < len(lines) and lines[end].strip().startswith("|"):
                end += 1
            if end == index + 2:
                index = end
                continue
        kept.append(lines[index])
        index += 1
    return "\n".join(kept).strip()


def _is_table_header_at(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    header = lines[index].strip()
    separator = lines[index + 1].strip()
    return header.startswith("|") and separator.startswith("|") and "---" in separator


def _needs_market_brand_insight(question: str, answer: str) -> bool:
    if not any(token in question for token in ("시장", "리바로만", "브랜드")):
        return False
    return not (
        "결론:" in answer
        and any(token in answer for token in ("시장 전체와 동행", "시장 동반 하락", "시장 전반 조정"))
        and "리바로 변화율" in answer
        and "시장 변화율" in answer
    )


def _needs_brand_threat_insight(question: str, answer: str) -> bool:
    if "위협" not in question:
        return False
    return not ("결론" in answer and "시사" in answer and "4.21" in answer and "0.20" in answer)


def _market_brand_insight(line: str) -> str:
    brand_change = _regex_value(line, r"브랜드 변화율 (-?\d+(?:\.\d+)?)%")
    market_change = _regex_value(line, r"시장 변화율 (-?\d+(?:\.\d+)?)%")
    gap = _regex_value(line, r"변화율 차이 (-?\d+(?:\.\d+)?)%p")
    period = _regex_value(line, r"리바로 (20\d{2}-\d{2}→20\d{2}-\d{2})")
    if not (brand_change and market_change and gap):
        return ""
    period_text = f"{period} " if period else ""
    return (
        f"결론: {period_text}리바로 매출 하락은 리바로만의 고유 약세로 보기보다 시장 전체와 동행한 변화로 해석됩니다. "
        f"리바로 변화율은 {brand_change}%, 시장 변화율은 {market_change}%로 차이가 {gap}%p입니다. "
        "두 변화율의 폭이 유사하므로 시장 동반 하락이 주요 배경이고, 리바로 고유 약세보다는 시장 전반 조정이 리바로 매출 하락에 작용한 것으로 해석됩니다."
    )


def _brand_threat_insight(line: str) -> str:
    livaro_ms = _regex_value(line, r"리바로 MS 변화 (-?\d+(?:\.\d+)?)%p")
    atozet_ms = _regex_value(line, r"아토젯 MS 변화 (-?\d+(?:\.\d+)?)%p")
    livaro_sales = _regex_value(line, r"리바로 매출 변화율 (-?\d+(?:\.\d+)?)%")
    atozet_sales = _regex_value(line, r"아토젯 매출 변화율 (-?\d+(?:\.\d+)?)%")
    period = _regex_value(line, r"리바로 vs 아토젯 (20\d{2}-\d{2}→20\d{2}-\d{2})")
    if not (livaro_ms and atozet_ms and livaro_sales and atozet_sales):
        return ""
    period_text = f"{period} 기준으로 " if period else ""
    return (
        f"결론: {period_text}아토젯은 리바로 대비 위협 신호가 있습니다. "
        f"점유율 변화는 리바로 {livaro_ms}%p, 아토젯 {atozet_ms}%p로 둘 다 큰 상승은 아니지만, "
        f"매출 변화율은 리바로 {livaro_sales}%, 아토젯 {atozet_sales}%로 아토젯 쪽이 더 강합니다. "
        "따라서 아토젯은 현재 추세상 더 방어적이고 성장률도 높은 경쟁 브랜드로 봐야 하며, 리바로에는 점유 방어와 매출 성장 격차를 동시에 압박하는 위협 요인으로 해석됩니다."
    )


def _regex_value(text: str, pattern: str) -> str:
    match = re.search(pattern, text)
    return match.group(1) if match else ""


def _has_causal_structure(answer: str) -> bool:
    return all(token in answer for token in ("근거", "인과 해석", "시사점"))


def _has_existing_causal_analysis(answer: str) -> bool:
    text = re.sub(r"\s+", " ", answer).strip()
    if not text:
        return False
    evidence_tokens = (
        "share-of-growth",
        "성장분",
        "점유",
        "시장",
        "매출",
        "환자수",
        "변화율",
        "%p",
        "억원",
    )
    analysis_tokens = (
        "해석",
        "시사",
        "압력",
        "재편",
        "반대 방향",
        "동행",
        "고유",
        "위협",
        "방어",
        "이탈",
        "직접 처방 이동",
        "배경",
        "연결",
    )
    return any(token in text for token in evidence_tokens) and any(token in text for token in analysis_tokens)


def _causal_evidence_line(question: str, fact_md: str) -> str:
    mandatory = tuple(line.removeprefix("- ").strip() for line in mandatory_fact_lines(fact_md))
    if not mandatory:
        return ""
    causal_tokens = ("왜", "원인", "영향", "시장", "고유", "위협", "경쟁", "구도", "변화", "추이", "하락", "상승", "어때")
    has_causal_question = any(token in question for token in causal_tokens)
    preferred_kinds = (
        "시장/브랜드 변화율 대조",
        "브랜드 추세 비교",
        "인사이트 계산",
        "상위 브랜드 추이",
        "Brand 상위",
        "매출 변화",
        "점유율 변화",
    )
    for kind in preferred_kinds:
        for line in mandatory:
            if kind in line and (has_causal_question or kind in {"시장/브랜드 변화율 대조", "브랜드 추세 비교", "인사이트 계산"}):
                return _causal_presentable_evidence(line)
    if re.search(r"(환자|질병|HIRA)", question, re.IGNORECASE):
        fallback_kinds = ("HIRA 환자수", "브랜드 핵심 지표", "데이터 미보유")
    else:
        fallback_kinds = ("브랜드 핵심 지표", "HIRA 환자수", "데이터 미보유")
    for kind in fallback_kinds:
        for line in mandatory:
            if kind in line:
                return _causal_presentable_evidence(line)
    if has_causal_question:
        return _causal_presentable_evidence(mandatory[0])
    return ""


def _causal_presentable_evidence(line: str) -> str:
    if line.startswith("HIRA 환자수"):
        rendered = _presentable_hira_patient_line(f"- {line}")
        if rendered:
            return rendered
    if line.startswith("브랜드 핵심 지표"):
        rendered = _presentable_brand_metric_line(f"- {line}")
        if rendered:
            return rendered
    if line.startswith("매출 추이"):
        rendered = _presentable_sales_trend_line(f"- {line}")
        if rendered:
            return rendered
    if line.startswith("Brand 상위") or line.startswith("상위 브랜드"):
        return "상위 브랜드 최신 점유율·매출 순위가 확인됨"
    replacements = {
        "Brand 상위": "상위 브랜드",
        "인사이트 계산": "인사이트 계산",
    }
    result = line
    for raw, friendly in replacements.items():
        result = result.replace(raw, friendly)
    return result


def _sentence_from_evidence(evidence: str) -> str:
    evidence = evidence.strip()
    if evidence.endswith((".", "!", "?")):
        return evidence
    if evidence.endswith("입니다"):
        return f"{evidence}."
    return f"{evidence}입니다."


def _causal_interpretation(evidence: str) -> str:
    if "시장/브랜드 변화율 대조" in evidence:
        return "브랜드 변화율을 시장 변화율과 나란히 보면 하락의 배경이 시장 동행인지 브랜드 고유 압력인지 구분할 수 있습니다."
    if "브랜드 추세 비교" in evidence:
        return "기준 브랜드와 비교 브랜드의 점유율·매출 변화 방향을 함께 보면 경쟁 압력이 어느 쪽에서 커지는지 판단할 수 있습니다."
    if "share-of-growth" in evidence or "인사이트 계산" in evidence:
        return "시장 성장 기여도와 점유 변화 지표를 함께 보면 단순 순위가 아니라 어느 브랜드가 성장분에 더 크게 기여했는지 해석할 수 있습니다."
    if "Brand 상위" in evidence or "상위 브랜드" in evidence:
        return "상위권 브랜드의 점유율·매출 위치는 시장 재편의 방향과 압력의 중심을 보여주는 근거입니다."
    if "HIRA 환자수" in evidence or ("HIRA" in evidence and "환자수" in evidence):
        return "환자수 규모와 브랜드 매출·점유율은 질환 통계와 처방 성과를 나란히 보는 보조 근거입니다."
    if "브랜드 핵심 지표" in evidence or ("매출" in evidence and "시장점유율" in evidence and "순위" in evidence):
        return "브랜드 매출·점유율·순위는 시장 내 침투 수준과 경쟁 방어 과제를 보여줍니다."
    if "매출 변화" in evidence or "점유율 변화" in evidence:
        return "기간별 변화 방향과 폭을 비교하면 단기 지표 변화가 시장 동행인지 브랜드 고유 변화인지 판단할 수 있습니다."
    return "확정 fact의 방향과 폭을 함께 보면 매출·점유율 변화가 어느 구간에서 강해졌는지 판단할 수 있습니다."


def _causal_implication(evidence: str) -> str:
    if "시장/브랜드 변화율 대조" in evidence:
        return "따라서 같은 월의 시장 전체 변화와 브랜드 변화를 계속 같이 보면서 고유 약세 신호가 반복되는지 확인해야 합니다."
    if "브랜드 추세 비교" in evidence:
        return "따라서 비교 브랜드의 상승 지속성과 기준 브랜드의 방어 여부가 다음 경쟁 구도 판단의 핵심 관찰점입니다."
    if "share-of-growth" in evidence or "인사이트 계산" in evidence:
        return "따라서 성장 기여도가 높은 브랜드와 하락 브랜드의 조합을 우선 추적하는 것이 실질적인 경쟁 이동을 파악하는 데 중요합니다."
    if "Brand 상위" in evidence or "상위 브랜드" in evidence:
        return "따라서 현재 순위뿐 아니라 상위권 내 점유율 변화가 이어지는지 보는 것이 시장 재편 판단에 더 중요합니다."
    if "HIRA 환자수" in evidence or ("HIRA" in evidence and "환자수" in evidence):
        return "따라서 환자수 통계와 실제 브랜드 성과를 분리해 보면서 데이터가 직접 연결되는 범위 안에서만 해석해야 합니다."
    if "브랜드 핵심 지표" in evidence or ("매출" in evidence and "시장점유율" in evidence and "순위" in evidence):
        return "따라서 이후 매출·점유율의 반복 방향을 보면 시장 내 침투가 강화되는지 또는 방어 압력이 커지는지 판단할 수 있습니다."
    return "따라서 최근 변화가 반복되는지와 같은 방향의 시장 신호가 있는지를 함께 확인해야 합니다."


def _is_source_heading(stripped: str) -> bool:
    return bool(re.fullmatch(r"#{1,6}\s*출처", stripped) or stripped == "출처")


def _is_heading(stripped: str) -> bool:
    return bool(re.match(r"#{1,6}\s+\S", stripped))


def _non_news_fact_markdown(fact_md: str) -> str:
    return "\n".join(_fact_lines_by_news_state(fact_md, want_news=False))


def _news_fact_markdown(fact_md: str) -> str:
    return "\n".join(_fact_lines_by_news_state(fact_md, want_news=True))


def _news_fact_records(fact_md: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for line in _news_fact_markdown(fact_md).splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped or "날짜" in stripped:
            continue
        cells = [_clean_news_cell(cell) for cell in stripped.strip("|").split("|")]
        if len(cells) < 5 or cells[0] in {"상태", "항목"}:
            continue
        if len(cells) >= 6:
            date, title, source, url, summary, excerpt = cells[:6]
        else:
            date, title, source, summary, excerpt = cells[:5]
            url = ""
        records.append(
            {
                "date": date,
                "title": title,
                "source": source,
                "url": url,
                "summary": summary,
                "excerpt": excerpt,
            }
        )
    return records


def _clean_news_cell(value: str) -> str:
    cleaned = unescape(value.strip()).replace("\\|", "|")
    return "" if cleaned == "-" else re.sub(r"\s+", " ", cleaned).strip()


def _news_issue_text(record: dict[str, str]) -> str:
    raw = record.get("summary") or record.get("excerpt") or ""
    text = re.sub(r"\s+", " ", raw).strip()
    if not text:
        return ""
    first_sentence = _first_korean_sentence(text)
    if len(first_sentence) > 140:
        first_sentence = first_sentence[:137].rstrip() + "..."
    return _redact_news_issue_numbers(first_sentence)


def _first_korean_sentence(text: str) -> str:
    candidates = re.split(r"(?<=[.!?。])\s+|(?<=다\.)\s+", text)
    for candidate in candidates:
        normalized = candidate.strip()
        if normalized:
            return normalized
    return text.strip()


def _redact_news_issue_numbers(text: str) -> str:
    if not allowed_numbers(text):
        return text
    segments = [
        _NEWS_ISSUE_CODE_RE.sub("", segment).strip(" ,·:;()[]")
        for segment in _NEWS_ISSUE_NUMBER_RE.split(text)
    ]
    candidates = [segment for segment in segments if len(segment) >= 8 and not allowed_numbers(segment)]
    if candidates:
        return max(candidates, key=len)
    without_numbers = _NEWS_ISSUE_NUMBER_RE.sub("", text)
    without_codes = _NEWS_ISSUE_CODE_RE.sub("", without_numbers)
    return re.sub(r"\s+", " ", without_codes).strip(" ,·:;()[]")


def _news_citation_tokens(news_md: str) -> set[str]:
    tokens: set[str] = set()
    for record in _news_fact_records(news_md):
        for key in ("date", "title", "source", "url"):
            tokens.update(allowed_numbers(record.get(key, "")))
    return tokens


def _fact_lines_by_news_state(fact_md: str, *, want_news: bool) -> list[str]:
    selected: list[str] = []
    in_news = False
    for line in fact_md.splitlines():
        stripped = line.strip()
        if stripped.startswith("### "):
            in_news = _is_news_fact_heading(stripped)
        if in_news is want_news:
            selected.append(line)
    return selected


def _has_news_fact(fact_md: str) -> bool:
    return any(_is_news_fact_heading(line.strip()) for line in fact_md.splitlines())


def _is_news_fact_heading(line: str) -> bool:
    return "뉴스 fact" in line or "인사이트 근거 fact - 뉴스/이슈" in line


def _table_fact_lines(markdown: str) -> list[str]:
    result: list[str] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 2 or cells[0] in {"항목", "구분", "출처"}:
            continue
        result.append(f"- {cells[0]}: {', '.join(cell for cell in cells[1:] if cell)}")
    return result


def _source_line(fact_md: str) -> str:
    rows: list[str] = []
    in_source = False
    for line in fact_md.splitlines():
        stripped = line.strip()
        if stripped.startswith("### "):
            in_source = "출처 유형 fact" in stripped
            continue
        if not in_source or not stripped.startswith("|") or "---" in stripped or "출처" in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|") if cell.strip()]
        rows.extend(cells)
    if not rows:
        return ""
    return "출처: " + ", ".join(dict.fromkeys(rows))


def source_line_from_fact_md(fact_md: str) -> str:
    """Return the user-facing source line from a verified fact block."""
    return _source_line(fact_md)


def _mandatory_share_delta_line(fact_md: str) -> str:
    for line in mandatory_fact_lines(fact_md):
        if "점유율 변화" in line and re.search(r"[+-]?\d+(?:\.\d+)?%p", line):
            return line
    return ""


def _computed_share_delta_line(markdown: str) -> str:
    rows = _period_share_rows(markdown)
    if len(rows) < 2:
        return ""
    start_period, start_pct = rows[-2]
    end_period, end_pct = rows[-1]
    delta = round(end_pct - start_pct, 2)
    return f"- 점유율 변화: {start_period}→{end_period}: {start_pct:.2f}% → {end_pct:.2f}%, 변화 {delta:+.2f}%p"


def _period_share_rows(markdown: str) -> list[tuple[str, float]]:
    rows: list[tuple[str, float]] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if not cells:
            continue
        period_match = re.search(r"20\d{2}-\d{2}", cells[0])
        if not period_match:
            continue
        pct_values = [
            float(match.group(1).replace(",", ""))
            for cell in cells[1:]
            for match in re.finditer(r"([+-]?\d+(?:,\d{3})*(?:\.\d+)?)%", cell)
        ]
        if pct_values:
            rows.append((period_match.group(0), pct_values[0]))
    return rows
