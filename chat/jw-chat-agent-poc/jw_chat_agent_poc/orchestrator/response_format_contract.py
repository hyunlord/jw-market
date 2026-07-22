from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from jw_chat_agent_poc.orchestrator.provenance_calls import provenance_rows_from_calls
from jw_chat_agent_poc.orchestrator.provenance_model import MISSING_LABEL, PROVENANCE_HEADERS
from jw_chat_agent_poc.orchestrator.source_fusion_policy import source_family


class ResponseFormatMode(StrEnum):
    OFF = "OFF"
    SHADOW = "SHADOW"
    ENFORCE = "ENFORCE"


class ClaimBindingLookup(Protocol):
    def has_binding(self, claim_text: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class ResponseFormatViolation:
    code: str
    detail: str
    origin: str = "assembler"

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "detail": self.detail, "origin": self.origin}


@dataclass(frozen=True, slots=True)
class ResponseFormatReport:
    mode: ResponseFormatMode
    violations: tuple[ResponseFormatViolation, ...] = ()
    actions: tuple[str, ...] = ()
    blocked: bool = False
    blocked_codes: tuple[str, ...] = ()
    binding_state: str = "not_required"

    @property
    def violation_codes(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.violations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "violations": tuple(item.to_dict() for item in self.violations),
            "actions": self.actions,
            "blocked": self.blocked,
            "blocked_codes": self.blocked_codes,
            "binding_state": self.binding_state,
        }


@dataclass(frozen=True, slots=True)
class ResponseFormatResult:
    answer: str
    report: ResponseFormatReport


_MODE_ENV = "JW_CHAT_RESPONSE_FORMAT_CONTRACT"
_PROVENANCE_MISSING_RATIO_ENV = "JW_CHAT_RESPONSE_FORMAT_PROVENANCE_MISSING_RATIO"
_DEFAULT_PROVENANCE_MISSING_RATIO = 0.70
_BLOCKED_ANSWER = (
    "요청하신 응답을 형식 계약에 맞게 안전하게 구성하지 못했습니다. "
    "근거를 분리해 다시 조회해 주세요."
)
_TABLE_ONLY_REQUEST_RE = re.compile(r"(?:표\s*만|표로|목록(?:만)?|나열|리스트|테이블)", re.IGNORECASE)
_CHANGE_RE = re.compile(
    r"증가|감소|상승|하락|확대|축소|전환|역전|정체|유지|변화|개선|악화|회복|둔화|가속|감속"
)
_CAUSAL_RE = re.compile(r"때문에|원인은|영향으로|기인(?:해|하여|한다|했다)?|유발|견인|주도")
_CAUSAL_SAFE_RE = re.compile(r"단정하지|확인할 수 없|근거가 없|가능성|후보|관측|동시")
_AGGREGATE_RE = re.compile(r"합산|총합|합계|평균|통합|더(?:한|해|하면)|합친")
_NUMBER_RE = re.compile(r"(?<![A-Za-z])[-+]?\d[\d,.]*(?:\s*(?:%|원|억원|조원|건|명))?")
_KNOWN_SOURCE_RE = re.compile(
    r"IQVIA(?:\s+NSA)?|UBIST|CSD|HIRA|뉴스/이슈|event_brand_scores|deep_analysis|업로드\s*파일",
    re.IGNORECASE,
)
_PLACEHOLDER_VALUES = frozenset(
    {
        "해당 없음",
        "없음",
        "이슈 없음",
        "특이사항 없음",
        "확인된 이슈 없음",
        "-",
        MISSING_LABEL,
    }
)
_MISSING_PROVENANCE_VALUES = frozenset(
    {"", "-", MISSING_LABEL, "해당 없음", "없음", "미확인", "N/A", "n/a", "null", "None"}
)
_DATA_ABSENCE_MARKERS = (
    "미보유 데이터 처리",
    "미보유 데이터",
    "현재 가능한 proxy",
    "해석 가능한 상한선",
    "확인 필요 데이터",
    "확보 시 수행할 분석",
)


def evaluate_response_format_contract(
    question: str,
    answer: str,
    *,
    tool_calls: Sequence[Mapping[str, Any]] = (),
    sources: Sequence[str] = (),
    claim_bindings: ClaimBindingLookup | None = None,
) -> ResponseFormatReport:
    violations: list[ResponseFormatViolation] = []

    if _is_table_or_list_only(answer) and not _TABLE_ONLY_REQUEST_RE.search(question):
        violations.append(
            ResponseFormatViolation(
                "C1_TABLE_ONLY",
                "표 또는 목록만 있고 변화 중심 서술이 없습니다.",
            )
        )

    empty_sections = _empty_issue_sections(answer)
    if empty_sections:
        violations.append(
            ResponseFormatViolation(
                "C2_EMPTY_ISSUE_SECTION",
                f"내용 없는 이슈 섹션 {len(empty_sections)}개가 렌더됐습니다.",
            )
        )

    causal_claims = _unbound_causal_claims(answer, claim_bindings)
    binding_state = "not_required"
    if causal_claims:
        binding_state = "available" if claim_bindings is not None else "unavailable_shadow"
        violations.append(
            ResponseFormatViolation(
                "C3_UNBOUND_CAUSAL",
                f"claim binding이 확인되지 않은 인과 표현 {len(causal_claims)}개가 있습니다.",
                origin="claim_binding",
            )
        )
    elif _causal_claims(answer):
        binding_state = "available" if claim_bindings is not None else "unavailable_shadow"

    if _has_cross_source_aggregation(answer, sources):
        violations.append(
            ResponseFormatViolation(
                "C4_CROSS_SOURCE_AGGREGATION",
                "서로 다른 소스가 단일 수치로 합산 또는 평균됐습니다.",
                origin="source_fusion",
            )
        )

    # A typed absence must not invent provenance fields merely to satisfy C5.
    # Its five-step absence contract is the evidence that no factual row exists.
    if not _is_data_absence_section(answer):
        provenance_violation = _incomplete_provenance_violation(answer, tool_calls, sources)
        if provenance_violation is not None:
            violations.append(provenance_violation)

    return ResponseFormatReport(
        mode=ResponseFormatMode.SHADOW,
        violations=tuple(violations),
        binding_state=binding_state,
    )


def apply_response_format_contract(
    question: str,
    answer: str,
    *,
    mode: ResponseFormatMode | str | None = None,
    tool_calls: Sequence[Mapping[str, Any]] = (),
    sources: Sequence[str] = (),
    claim_bindings: ClaimBindingLookup | None = None,
) -> ResponseFormatResult:
    active_mode = _response_format_mode(mode)
    if active_mode is ResponseFormatMode.OFF:
        return ResponseFormatResult(answer, ResponseFormatReport(mode=active_mode))

    evaluated = evaluate_response_format_contract(
        question,
        answer,
        tool_calls=tool_calls,
        sources=sources,
        claim_bindings=claim_bindings,
    )
    if active_mode is ResponseFormatMode.SHADOW:
        return ResponseFormatResult(
            answer,
            ResponseFormatReport(
                mode=active_mode,
                violations=evaluated.violations,
                binding_state=evaluated.binding_state,
            ),
        )

    actions: list[str] = []
    enforced_answer = answer
    if "C2_EMPTY_ISSUE_SECTION" in evaluated.violation_codes:
        enforced_answer = _omit_empty_issue_sections(enforced_answer)
        actions.append("omit_empty_issue_section")

    blocking = {"C1_TABLE_ONLY", "C4_CROSS_SOURCE_AGGREGATION", "C5_PROVENANCE_INCOMPLETE"}
    if claim_bindings is not None:
        blocking.add("C3_UNBOUND_CAUSAL")
    blocked_codes = tuple(code for code in evaluated.violation_codes if code in blocking)
    blocked = bool(blocked_codes)
    if blocked:
        enforced_answer = _BLOCKED_ANSWER
        actions.append("block_invalid_response")

    return ResponseFormatResult(
        enforced_answer,
        ResponseFormatReport(
            mode=active_mode,
            violations=evaluated.violations,
            actions=tuple(actions),
            blocked=blocked,
            blocked_codes=blocked_codes,
            binding_state=evaluated.binding_state,
        ),
    )


def _response_format_mode(mode: ResponseFormatMode | str | None) -> ResponseFormatMode:
    if isinstance(mode, ResponseFormatMode):
        return mode
    raw = str(mode if mode is not None else os.getenv(_MODE_ENV, ResponseFormatMode.OFF.value))
    try:
        return ResponseFormatMode(raw.strip().upper())
    except ValueError:
        return ResponseFormatMode.OFF


def _is_table_or_list_only(answer: str) -> bool:
    body = _without_provenance_section(answer)
    if _is_data_absence_section(body):
        return False
    has_table_or_list = False
    prose: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if line.startswith("|"):
            has_table_or_list = True
            continue
        if re.match(r"^(?:[-*+]\s+|\d+[.)]\s+)", line):
            has_table_or_list = True
            continue
        prose.append(line)
    if not has_table_or_list:
        return False
    return not _CHANGE_RE.search(" ".join(prose))


def _without_provenance_section(answer: str) -> str:
    lines = answer.splitlines()
    kept: list[str] = []
    in_source = False
    source_level = 0
    for line in lines:
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line.strip())
        if heading:
            level = len(heading.group(1))
            title = heading.group(2).strip()
            if title == "출처":
                in_source = True
                source_level = level
                continue
            if in_source and level <= source_level:
                in_source = False
        if not in_source:
            kept.append(line)
    return "\n".join(kept)


def _empty_issue_sections(answer: str) -> tuple[tuple[int, int], ...]:
    lines = answer.splitlines()
    headings: list[tuple[int, int, str]] = []
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line.strip())
        if match:
            headings.append((index, len(match.group(1)), match.group(2).strip()))
    empty: list[tuple[int, int]] = []
    for position, (start, level, title) in enumerate(headings):
        end = len(lines)
        for next_start, next_level, _ in headings[position + 1 :]:
            if next_level <= level:
                end = next_start
                break
        section = "\n".join(lines[start:end]).strip()
        if title == "출처" or _is_data_absence_section(section):
            continue
        body = [line.strip() for line in lines[start + 1 : end] if line.strip()]
        if len(body) == 1 and body[0].rstrip(".。") in _PLACEHOLDER_VALUES:
            empty.append((start, end))
    return tuple(empty)


def _is_data_absence_section(section: str) -> bool:
    return any(marker in section for marker in _DATA_ABSENCE_MARKERS)


def _omit_empty_issue_sections(answer: str) -> str:
    lines = answer.splitlines()
    for start, end in reversed(_empty_issue_sections(answer)):
        del lines[start:end]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _causal_claims(answer: str) -> tuple[str, ...]:
    claims: list[str] = []
    for sentence in re.split(r"(?<=[.!?。])\s+|\n+", _without_provenance_section(answer)):
        text = sentence.strip()
        if text and _CAUSAL_RE.search(text) and not _CAUSAL_SAFE_RE.search(text):
            claims.append(text)
    return tuple(claims)


def _unbound_causal_claims(
    answer: str,
    claim_bindings: ClaimBindingLookup | None,
) -> tuple[str, ...]:
    claims = _causal_claims(answer)
    if claim_bindings is None:
        return claims
    return tuple(claim for claim in claims if not claim_bindings.has_binding(claim))


def _has_cross_source_aggregation(answer: str, sources: Sequence[str]) -> bool:
    fallback_families = {source_family(source) for source in sources if source_family(source)}
    for sentence in re.split(r"(?<=[.!?。])\s+|\n+", _without_provenance_section(answer)):
        if not _AGGREGATE_RE.search(sentence) or not _NUMBER_RE.search(sentence):
            continue
        mentioned = {source_family(match.group(0)) for match in _KNOWN_SOURCE_RE.finditer(sentence)}
        families = mentioned or fallback_families
        if len(families) > 1:
            return True
    return False


def _incomplete_provenance_violation(
    answer: str,
    tool_calls: Sequence[Mapping[str, Any]],
    sources: Sequence[str],
) -> ResponseFormatViolation | None:
    rendered_rows = _provenance_table_rows(answer)
    evidence_expected = bool(tool_calls or sources)
    if not rendered_rows:
        if not evidence_expected:
            return None
        return ResponseFormatViolation(
            "C5_PROVENANCE_INCOMPLETE",
            "근거가 있는 응답에 7열 출처 표가 없습니다.",
            origin="assembler",
        )

    threshold = _provenance_missing_ratio()
    missing_counts = tuple(sum(cell in _MISSING_PROVENANCE_VALUES for cell in row) for row in rendered_rows)
    violating_rows = tuple(count for count in missing_counts if count / len(PROVENANCE_HEADERS) > threshold)
    if not violating_rows:
        return None

    origin = "upstream"
    if tool_calls or sources:
        structured_rows = provenance_rows_from_calls(tool_calls, sources)
        structured_missing = tuple(
            sum(cell in _MISSING_PROVENANCE_VALUES for cell in row.as_tuple()) for row in structured_rows
        )
        if structured_missing and all(
            count / len(PROVENANCE_HEADERS) <= threshold for count in structured_missing
        ):
            origin = "assembler"
    return ResponseFormatViolation(
        "C5_PROVENANCE_INCOMPLETE",
        f"출처 표 {len(violating_rows)}개 행이 7열 충족률 기준을 넘지 못했습니다.",
        origin=origin,
    )


def _provenance_missing_ratio() -> float:
    try:
        value = float(os.getenv(_PROVENANCE_MISSING_RATIO_ENV, str(_DEFAULT_PROVENANCE_MISSING_RATIO)))
    except ValueError:
        return _DEFAULT_PROVENANCE_MISSING_RATIO
    return min(1.0, max(0.0, value))


def _provenance_table_rows(answer: str) -> tuple[tuple[str, ...], ...]:
    lines = answer.splitlines()
    rows: list[tuple[str, ...]] = []
    header_index: int | None = None
    for index, line in enumerate(lines):
        cells = _markdown_cells(line)
        if cells == PROVENANCE_HEADERS:
            header_index = index
            break
    if header_index is None:
        return ()
    for line in lines[header_index + 1 :]:
        cells = _markdown_cells(line)
        if not cells:
            if rows:
                break
            continue
        if all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells):
            continue
        if len(cells) != len(PROVENANCE_HEADERS):
            break
        rows.append(cells)
    return tuple(rows)


def _markdown_cells(line: str) -> tuple[str, ...]:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return ()
    return tuple(cell.strip().replace("\\|", "|") for cell in stripped.strip("|").split("|"))
