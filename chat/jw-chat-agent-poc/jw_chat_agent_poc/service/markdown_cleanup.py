from __future__ import annotations

import re
from typing import Final

from jw_chat_agent_poc.orchestrator.provenance_labels import sanitize_internal_provenance_labels


# --- P0-B: single internal-terminology scrub gate ------------------------------
# Rules live here as constants (설정), never hardcoded across renderers, and apply
# to every market. They map internal agent/tool wording to user-facing phrasing so
# no tool name, query id, internal fact heading, verifier jargon or "agent loop"
# reaches the user. All patterns are distinctive snake_case identifiers or exact
# internal phrases → false-positive risk on brand/market names and ordinary prose
# (e.g. the bare word 주의) is nil.
_INTERNAL_TOOL_LABELS: Final[dict[str, str]] = {
    "get_brand_metric": "시장 지표 조회",
    "get_metric": "시장 지표 조회",
    "get_market_scope": "시장 범위 조회",
    "resolve_relative_date": "기간 해석",
    "search_news": "뉴스 검색",
    "get_disease_stats": "질병 통계 조회",
    "get_procedure_stats": "진료행위 통계 조회",
    "search_clinical": "임상 근거 조회",
    "search_patent": "특허 조회",
    "search_drug_info": "허가 정보 조회",
    "csd_activity_trend": "활동량 조회",
    "get_csd_activity_trend": "활동량 조회",
    "web_search": "웹 검색",
    "get_brand_sales": "매출 조회",
    "get_brand_share": "점유율 조회",
    "get_brand_series": "시계열 조회",
    "compare_brands_series": "시계열 비교",
    "get_top_brands": "상위 브랜드 조회",
    "get_brand_channel_breakdown": "채널별 조회",
    "get_brand_specialty_breakdown": "진료과별 조회",
}
_INTERNAL_AXIS_LABELS: Final[dict[str, str]] = {
    "class_1": "Class 1",
    "class_2": "Class 2",
}
# Longest tokens first so get_csd_activity_trend wins over csd_activity_trend, etc.
_TOOL_TOKEN_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z0-9_])("
    + "|".join(re.escape(name) for name in sorted(_INTERNAL_TOOL_LABELS, key=len, reverse=True))
    + r")(?![A-Za-z0-9_])"
)
_VERIFIER_NOTICE_RE: Final[re.Pattern[str]] = re.compile(
    r"숫자\s*검증\s*[:：]\s*근거[^\n]*?제한했습니다\."
)
_INTERNAL_ID_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (re.compile(r"query_result_id\s*[:：]?\s*[0-9A-Za-z_-]*"), ""),
    (re.compile(r"(?<![A-Za-z0-9_])tool_call_id(?![A-Za-z0-9_])"), ""),
    (re.compile(r"(?<![A-Za-z0-9_])qr_\d+(?![A-Za-z0-9_])"), ""),
    (re.compile(r"query\(spec\)"), "조회"),
)
# Internal fact-set section marker. Every internal fact block heading ends in the
# English word "fact" (### … fact); it is parsed structurally inside the pipeline
# but must never surface. "fact" is English-only here so removing the standalone
# word is false-positive-free (word boundary protects factor/artifact/factory).
_INTERNAL_FACT_MARKER_RE: Final[re.Pattern[str]] = re.compile(r"[ \t]*\bfact\b")
# Exact internal phrases, longest/most-specific first.
_INTERNAL_PHRASES: Final[tuple[tuple[str, str], ...]] = (
    ("확정 데이터 기준으로 정리하면 다음과 같습니다.", ""),
    ("반드시 반영할 내용", "내용"),
    ("확정 fact set", "확정 데이터"),
    ("필수 답변 fact", "핵심 데이터"),
    ("provenance fact", "출처 요약"),
    ("fact set", "데이터"),
    ("agent loop step 예산", "분석 단계 예산"),
    ("agent loop를", "분석을"),
    ("agent loop을", "분석을"),
    ("agent loop", "분석"),
    ("agent_loop", "분석"),
)


def scrub_internal_terminology(text: str) -> str:
    """Remove internal agent/tool wording from user-facing text (오탐 0, idempotent).

    This is the single scrub applied at the end of ``cleanup_markdown_answer`` and at
    any answer path that would otherwise bypass the gate (deterministic relay, post-
    cleanup notice appends). It only rewrites internal tokens/phrases and never touches
    numbers, brand/market names, provenance field values, or ordinary Korean prose.
    """
    result = _VERIFIER_NOTICE_RE.sub("표에 있는 확정 수치를 기준으로 정리했습니다.", text)
    for needle, replacement in _INTERNAL_PHRASES:
        result = result.replace(needle, replacement)
    for pattern, replacement in _INTERNAL_ID_PATTERNS:
        result = pattern.sub(replacement, result)
    result = _TOOL_TOKEN_RE.sub(lambda match: _INTERNAL_TOOL_LABELS[match.group(1)], result)
    for internal, public in _INTERNAL_AXIS_LABELS.items():
        result = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(internal)}(?![A-Za-z0-9_])", public, result)
    result = _INTERNAL_FACT_MARKER_RE.sub("", result)
    # Tidy only spaces created by removals; never touch line-leading indentation.
    result = re.sub(r"(?<=\S)[ \t]{2,}(?=\S)", " ", result)
    return re.sub(r"(?<=\S) +(?=[,.)}\]。」])", "", result)


def cleanup_markdown_answer(markdown: str) -> str:
    """Normalize generated markdown without inventing content."""
    text = re.sub(r"(?m)^(#{1,6})([^\s#])", r"\1 \2", markdown.strip())
    text = re.sub(r"(?m)^-\s*(?=[가-힣A-Za-z])", "- ", text)
    text = re.sub(r"(\d+위)(?=[가-힣A-Za-z])", r"\1 ", text)
    text = re.sub(r"(\d+위)\s+(라는|권)", r"\1\2", text)
    text = re.sub(r"(?<=[가-힣A-Za-z0-9])및(?=\s|[가-힣A-Za-z0-9])", " 및", text)
    text = re.sub(r"(?m)^(출처):\s*(\S)", r"\1: \2", text)
    text = _replace_internal_source_labels(text)
    text = re.sub(r"(?<=[,，])(?=[가-힣A-Za-z])", " ", text)
    text = re.sub(r"(?<=\d{4}-\d{2})[:：](?=\d)", ": ", text)
    text = re.sub(r"(?<=[가-힣A-Za-z]):(?=20\d{2}-\d{2})", ": ", text)
    text = re.sub(r"(?<!\d{4}-\d{2})→(?=20\d{2}-\d{2})", "→ ", text)
    text = re.sub(r"(?<=\d{4}-\d{2})(?=[+-]?\d+(?:\.\d+)?(?:%|억원|억\s*원))", " ", text)
    text = re.sub(r"(?<=[가-힣])(?=\d+(?:,\d{3})*(?:\.\d+)?(?:억\s*원|억원|%))", " ", text)
    text = re.sub(r"(?<=억 원)(?=(?:증가|감소|상승|하락))", " ", text)
    text = re.sub(r"(?<=억원)(?=(?:증가|감소|상승|하락))", " ", text)
    text = re.sub(r"(?<=[가-힣A-Za-z0-9])(?=(?:시장점유율|시장규모|매출액|매출변화|매출하락))", " ", text)
    text = text.replace("데이터미보유", "데이터 미보유")
    text = _normalize_korean_particles_preserving_article_titles(text)
    text = _replace_common_korean_typos_preserving_article_titles(text)
    text = text.replace("억원로", "억원으로")
    text = text.replace("억 원로", "억 원으로")
    text = text.replace("해당합니다이며", "해당하며")
    text = re.sub(r"점(?=양해)", "점 ", text)
    lines = [_normalize_table_row(line) for line in text.splitlines()]
    lines = _remove_empty_headings(lines)
    lines = _remove_duplicate_bullets(lines)
    lines = _remove_orphaned_news_headings(lines)
    lines = _renumber_section_headings(lines)
    text = "\n".join(lines).strip()
    text = _remove_adjacent_duplicate_sentences(text)
    text = _remove_duplicate_top_brand_rank_prose(text)
    return scrub_internal_terminology(text)


def _normalize_korean_particles(text: str) -> str:
    """Fix common generated Korean topic particles for single-token subjects."""
    return re.sub(r"([가-힣A-Za-z0-9+._/-]*[가-힣])(은|는)\b", _fix_topic_particle, text)


def _normalize_korean_particles_preserving_article_titles(text: str) -> str:
    """Avoid changing quoted article titles while cleaning generated prose."""
    protected: list[str] = []

    def protect(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"\uE000TITLE{len(protected) - 1}\uE001"

    masked = re.sub(r"「[^」]+」", protect, text)
    normalized = _normalize_korean_particles(masked)
    for index, original in enumerate(protected):
        normalized = normalized.replace(f"\uE000TITLE{index}\uE001", original)
    return normalized


def _replace_common_korean_typos_preserving_article_titles(text: str) -> str:
    """Fix narrow generated Korean typos without altering quoted article titles."""
    protected: list[str] = []

    def protect(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"\uE000TITLE{len(protected) - 1}\uE001"

    masked = re.sub(r"「[^」]+」", protect, text)
    fixed = masked.replace("있은", "있는")
    for index, original in enumerate(protected):
        fixed = fixed.replace(f"\uE000TITLE{index}\uE001", original)
    return fixed


def _fix_topic_particle(match: re.Match[str]) -> str:
    token, particle = match.groups()
    return token + ("은" if _has_jongseong(token) else "는")


def _has_jongseong(token: str) -> bool:
    for char in reversed(token):
        code = ord(char)
        if 0xAC00 <= code <= 0xD7A3:
            return (code - 0xAC00) % 28 != 0
    return False


def _replace_internal_source_labels(text: str) -> str:
    result = re.sub(r"(?<![A-Za-z0-9_])deep_analysis_events(?![A-Za-z0-9_])", "뉴스/이슈", text)
    result = re.sub(r"(?<![A-Za-z0-9_])cache(?![A-Za-z0-9_])", "UBIST", result)
    result = result.replace("내부 UBIST", "UBIST").replace("내부 심층분석", "뉴스/이슈")
    return sanitize_internal_provenance_labels(result)


def _normalize_table_row(line: str) -> str:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return line
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    if not cells:
        return line
    if all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells):
        return "| " + " | ".join("---" for _ in cells) + " |"
    return "| " + " | ".join(cells) + " |"


def _remove_empty_headings(lines: list[str]) -> list[str]:
    dropped: set[int] = set()
    for index, line in enumerate(lines):
        if not _is_heading_line(line):
            continue
        if "웹 검색 결과" in line:
            continue
        next_index = index + 1
        section_indexes: list[int] = []
        while next_index < len(lines) and not _is_heading_line(lines[next_index]):
            if _is_non_content_boundary(lines[next_index]):
                break
            section_indexes.append(next_index)
            next_index += 1
        has_content = any(
            lines[item].strip() and not _is_thematic_break(lines[item])
            for item in section_indexes
        )
        if not has_content:
            dropped.add(index)
            dropped.update(item for item in section_indexes if _is_thematic_break(lines[item]))
    return [line for index, line in enumerate(lines) if index not in dropped]


def _is_thematic_break(line: str) -> bool:
    return bool(re.fullmatch(r"\s*(?:-{3,}|\*{3,}|_{3,})\s*", line))


def _remove_duplicate_bullets(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    kept: list[str] = []
    for line in lines:
        match = re.match(r"^\s*[-*]\s+(?P<content>\S.*)$", line)
        if not match:
            kept.append(line)
            continue
        content = match.group("content").strip()
        if content in seen:
            continue
        seen.add(content)
        kept.append(line)
    return kept


def _remove_orphaned_news_headings(lines: list[str]) -> list[str]:
    kept: list[str] = []
    for index, line in enumerate(lines):
        if not (_is_heading_line(line) and "뉴스" in line):
            kept.append(line)
            continue
        if "웹 검색 결과" in line:
            kept.append(line)
            continue
        next_index = index + 1
        while next_index < len(lines) and not lines[next_index].strip():
            next_index += 1
        if next_index >= len(lines):
            continue
        next_text = lines[next_index]
        if any(token in next_text for token in ("뉴스", "기사", "이슈", "발췌", "약업신문", "의학신문")):
            kept.append(line)
    return kept


def _renumber_section_headings(lines: list[str]) -> list[str]:
    """Renumber generated section headings without touching tables or prose lists."""
    renumbered: list[str] = []
    next_number = 1
    for line in lines:
        markdown_heading = re.match(r"^(?P<prefix>#{1,6}\s+)(?P<number>\d+)\.\s+(?P<title>.+)$", line)
        if markdown_heading:
            renumbered.append(f"{markdown_heading.group('prefix')}{next_number}. {markdown_heading.group('title')}")
            next_number += 1
            continue
        bold_heading = re.match(r"^\*\*(?P<number>\d+)\.\s+(?P<title>[^*]+)\*\*$", line.strip())
        if bold_heading:
            renumbered.append(f"**{next_number}. {bold_heading.group('title')}**")
            next_number += 1
            continue
        renumbered.append(line)
    return renumbered


def _is_heading_line(line: str) -> bool:
    stripped = line.strip()
    return bool(re.match(r"^#{1,6}\s+\S", stripped) or re.fullmatch(r"\*\*[^*]+\*\*", stripped))


def _is_non_content_boundary(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("출처:") or stripped.startswith("## 처리 시간")


def _remove_adjacent_duplicate_sentences(markdown: str) -> str:
    paragraphs = re.split(r"(\n{2,})", markdown)
    cleaned: list[str] = []
    for paragraph in paragraphs:
        if not paragraph.strip() or paragraph.startswith("\n"):
            cleaned.append(paragraph)
            continue
        if _is_structured_markdown_block(paragraph):
            cleaned.append(paragraph)
            continue
        cleaned.append(_remove_adjacent_duplicate_sentence_in_paragraph(paragraph))
    text = "".join(cleaned).strip()
    return _remove_adjacent_duplicate_sentence_across_paragraphs(text)


def _is_structured_markdown_block(paragraph: str) -> bool:
    return any(line.lstrip().startswith(("#", "|", "-", "*")) for line in paragraph.splitlines())


def _remove_adjacent_duplicate_sentence_in_paragraph(paragraph: str) -> str:
    sentence_pattern = re.compile(r"(?P<sentence>[^\n]+?(?:입니다|합니다|됩니다|있습니다|없습니다)\.)\s+(?P=sentence)(?=\s|$)")
    previous = ""
    cleaned = paragraph
    while previous != cleaned:
        previous = cleaned
        cleaned = sentence_pattern.sub(r"\g<sentence>", cleaned)
    return cleaned


def _remove_adjacent_duplicate_sentence_across_paragraphs(markdown: str) -> str:
    sentence_pattern = re.compile(
        r"(?P<sentence>[^\n#|*\-][^\n]*?(?:입니다|합니다|됩니다|있습니다|없습니다)\.)\n{2,}(?P=sentence)(?=\s)"
    )
    previous = ""
    cleaned = markdown
    while previous != cleaned:
        previous = cleaned
        cleaned = sentence_pattern.sub(r"\g<sentence>", cleaned)
    return cleaned


def _remove_duplicate_top_brand_rank_prose(markdown: str) -> str:
    paragraphs = re.split(r"\n{2,}", markdown)
    kept: list[str] = []
    for index, paragraph in enumerate(paragraphs):
        later = "\n\n".join(paragraphs[index + 1 :])
        if _is_duplicate_top_brand_rank_paragraph(paragraph, later):
            continue
        kept.append(paragraph)
    return "\n\n".join(kept).strip()


def _is_duplicate_top_brand_rank_paragraph(paragraph: str, later: str) -> bool:
    stripped = paragraph.strip()
    if not stripped or stripped.startswith(("*", "-", "|", "#")):
        return False
    if "점유율" not in stripped:
        return False
    mentions = _rank_brand_mentions(stripped)
    if len(mentions) >= 2:
        repeated = sum(1 for rank, brand in mentions if _has_rank_brand_mention(later, rank, brand))
        if repeated >= 2:
            return True
    share_mentions = _brand_share_mentions(stripped)
    return sum(1 for brand, share in share_mentions if _has_brand_share_mention(later, brand, share)) >= 2


def _rank_brand_mentions(text: str) -> tuple[tuple[str, str], ...]:
    return tuple(re.findall(r"(\d+)위\s*([가-힣A-Za-z0-9][가-힣A-Za-z0-9+._-]*)", text))


def _has_rank_brand_mention(text: str, rank: str, brand: str) -> bool:
    return bool(re.search(rf"{re.escape(rank)}위[\s|:*_-]*{re.escape(brand)}", text))


def _brand_share_mentions(text: str) -> tuple[tuple[str, str], ...]:
    return tuple(
        (_normalize_brand_token(brand), share)
        for brand, share in re.findall(
            r"([가-힣A-Za-z0-9][가-힣A-Za-z0-9+._-]{1,})[은는이가\s()]*"
            r"(\d+(?:\.\d+)?)%",
            text,
        )
        if not re.fullmatch(r"20\d{2}[-./]\d{1,2}", brand)
    )


def _has_brand_share_mention(text: str, brand: str, share: str) -> bool:
    return brand in text and f"{share}%" in text


def _normalize_brand_token(token: str) -> str:
    if len(token) > 2 and token[-1] in {"은", "는", "이", "가"}:
        return token[:-1]
    return token
