from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from .models import FieldParseStatus, ParseStatus

_FIELD_HEADINGS: Final = {
    "target_condition": (
        "투여대상",
        "투여 대상",
        "급여대상",
        "대상환자",
        "인정기준",
        "대상 조건",
    ),
    "exclusion_rule": (
        "제외기준",
        "제외 기준",
        "투여제외",
        "급여제외",
        "급여제외 대상",
        "급여 제외 대상",
        "제외대상",
        "제외 대상",
        "금기환자",
        "금기증은 아래와 같으며 요양급여를 인정하지 아니함.",
        "금기증은 아래와 같으며, 요양급여를 인정하지 아니함.",
        "금기증은 아래와 같으며 요양급여를 인정하지 아니함",
        "금기증은 아래와 같으며, 요양급여를 인정하지 아니함",
    ),
    "dosage_limit": (
        "투여용량",
        "투여용량 및 기간",
        "인정용량",
        "인정 용량",
        "용량제한",
        "용량 제한",
        "용법·용량",
        "용법 및 용량",
        "용법용량",
        "투여기간",
        "용량",
    ),
}
_FIELD_ORDER: Final = ("target_condition", "exclusion_rule", "dosage_limit")
_AMBIGUOUS_INLINE_ALIASES: Final = frozenset({"인정기준"})
_NUMBERED_PREFIX_RE: Final = re.compile(r"^\s*(?:(?:\d+|[가-하])[.)])\s*")
_ATTACHMENT_ONLY_DOCUMENT_RE: Final = re.compile(
    r"(?:(?!첨부파일).){0,80}"
    r"첨부파일(?:에서|을(?: 통해)?).{0,100}(?:확인|참조)"
    r"(?:하십시오|하시기 바랍니다)?[.\s]*"
)
_INLINE_ALIASES: Final = tuple(
    sorted(
        {
            alias
            for aliases in _FIELD_HEADINGS.values()
            for alias in aliases
            if alias not in _AMBIGUOUS_INLINE_ALIASES
        },
        key=len,
        reverse=True,
    )
)
_INLINE_LABEL_RE: Final = re.compile(
    r"(?:(?<!\S)(?P<marker>(?:\d+|[가-하])[.)])\s*|(?<!\S))"
    rf"(?P<label>{'|'.join(re.escape(alias) for alias in _INLINE_ALIASES)})"
    r"(?:\s*\((?:금기증|금기사항)\))?"
    r"\s*(?P<colon>[:：])?\s*",
    re.IGNORECASE,
)
_TERMINAL_BOUNDARY_RE: Final = re.compile(
    r"\s(?=■|☞|\*\s*(?:시행일|종전고시|변경사유)|닫기\b)"
)
_KOREAN_DOT_BOUNDARY_RE: Final = re.compile(r"\s(?=[가-하]\.\s+\S)")


@dataclass(frozen=True, slots=True)
class StructuredParseResult:
    target_condition: str | None
    exclusion_rule: str | None
    dosage_limit: str | None
    parse_status: ParseStatus
    failed_fields: tuple[str, ...]
    target_status: FieldParseStatus
    exclusion_status: FieldParseStatus
    dosage_status: FieldParseStatus


def _clean(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def _normalize_label(value: str) -> str:
    without_number = _NUMBERED_PREFIX_RE.sub("", _clean(value))
    without_qualifier = re.sub(
        r"\s*\((?:금기증|금기사항)\)\s*$",
        "",
        without_number,
    )
    return without_qualifier.strip(" \t:：-–—").casefold()


def _field_for_label(value: str, *, allow_ambiguous: bool) -> str | None:
    normalized = _normalize_label(value)
    for field_name, aliases in _FIELD_HEADINGS.items():
        for alias in aliases:
            if not allow_ambiguous and alias in _AMBIGUOUS_INLINE_ALIASES:
                continue
            if normalized == alias.casefold():
                return field_name
    return None


def _extract_labeled_block(value: str) -> tuple[str, str] | None:
    cleaned = _clean(value)
    match = re.match(
        r"^\s*(?:(?:\d+|[가-하])[.)]\s*)?([^:：]{2,20})\s*[:：]\s*(.+)$",
        cleaned,
    )
    if match is None:
        return None
    field_name = _field_for_label(match.group(1), allow_ambiguous=False)
    extracted = _clean(match.group(2))
    if field_name is None or not extracted:
        return None
    return field_name, extracted


def _extract_inline_values(value: str) -> tuple[tuple[str, str], ...]:
    matches = tuple(
        match
        for match in _INLINE_LABEL_RE.finditer(value)
        if match.group("marker") is not None or match.group("colon") is not None
    )
    extracted: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        field_name = _field_for_label(match.group("label"), allow_ambiguous=False)
        boundaries = [len(value)]
        if index + 1 < len(matches):
            boundaries.append(matches[index + 1].start())
        marker = match.group("marker")
        terminal = _TERMINAL_BOUNDARY_RE.search(value, match.end())
        if terminal is not None:
            boundaries.append(terminal.start())
        if marker and marker[:-1].isdigit():
            sibling = re.search(r"\s(?=\d+[.)]\s+\S)", value[match.end() :])
        elif marker:
            sibling = re.search(r"\s(?=[가-하][.)]\s+\S)", value[match.end() :])
        else:
            sibling = _KOREAN_DOT_BOUNDARY_RE.search(value, match.end())
        if sibling is not None:
            boundaries.append(match.end() + sibling.start())
        end = min(boundaries)
        field_value = _clean(value[match.end() : end])
        if field_name is not None and field_value:
            extracted.append((field_name, field_value))
    return tuple(extracted)


def _structural_fields(value: str) -> tuple[str, ...]:
    fields: list[str] = []
    for match in _INLINE_LABEL_RE.finditer(value):
        if match.group("marker") is None and match.group("colon") is None:
            continue
        field_name = _field_for_label(match.group("label"), allow_ambiguous=False)
        if field_name is not None and field_name not in fields:
            fields.append(field_name)
    return tuple(fields)


def extract_structured(
    *,
    raw_text: str,
    headings: list[tuple[str, str, list[str]]],
    table_rows: list[tuple[str, ...]],
    blocks: list[str],
) -> StructuredParseResult:
    values: dict[str, str | None] = {field_name: None for field_name in _FIELD_ORDER}
    attempted_fields: set[str] = set()

    for level, heading, section_values in headings:
        if level == "h1":
            continue
        field_name = _field_for_label(heading, allow_ambiguous=True)
        value = _clean(" ".join(section_values))
        if field_name is not None:
            attempted_fields.add(field_name)
            if value and values[field_name] is None:
                values[field_name] = value

    for cells in table_rows:
        field_name = _field_for_label(cells[0], allow_ambiguous=False)
        value = _clean(" ".join(cells[1:]))
        if field_name is not None:
            attempted_fields.add(field_name)
            if value and values[field_name] is None:
                values[field_name] = value

    for block in blocks:
        attempted_fields.update(_structural_fields(block))
        inline_values = _extract_inline_values(block)
        extracted = _extract_labeled_block(block)
        candidates = inline_values or ((extracted,) if extracted is not None else ())
        for field_name, value in candidates:
            if values[field_name] is None:
                values[field_name] = value

    unresolved_fields = tuple(
        field_name
        for field_name in _FIELD_ORDER
        if field_name in attempted_fields and values[field_name] is None
    )
    present_count = sum(value is not None for value in values.values())
    attachment_only = _ATTACHMENT_ONLY_DOCUMENT_RE.fullmatch(raw_text) is not None
    if unresolved_fields:
        status = ParseStatus.PARTIAL if present_count else ParseStatus.FAILED
        failed_fields = unresolved_fields
    elif present_count:
        status = ParseStatus.OK
        failed_fields = ()
    elif attachment_only:
        status = ParseStatus.FAILED
        failed_fields = _FIELD_ORDER
    else:
        status = ParseStatus.NOT_APPLICABLE
        failed_fields = ()
    field_statuses = {
        field_name: (
            FieldParseStatus.EXTRACTED
            if values[field_name] is not None
            else FieldParseStatus.FAILED
            if field_name in attempted_fields or attachment_only
            else FieldParseStatus.NOT_APPLICABLE
        )
        for field_name in _FIELD_ORDER
    }
    return StructuredParseResult(
        target_condition=values["target_condition"],
        exclusion_rule=values["exclusion_rule"],
        dosage_limit=values["dosage_limit"],
        parse_status=status,
        failed_fields=failed_fields,
        target_status=field_statuses["target_condition"],
        exclusion_status=field_statuses["exclusion_rule"],
        dosage_status=field_statuses["dosage_limit"],
    )


def parse_stored_raw_text(raw_text: str) -> StructuredParseResult:
    """Reparse a persisted normalized notice body without external I/O."""

    cleaned = _clean(raw_text)
    return extract_structured(
        raw_text=cleaned,
        headings=[],
        table_rows=[],
        blocks=[cleaned],
    )
