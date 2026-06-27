from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from jw_chat_agent_poc.service.markdown_cleanup import cleanup_markdown_answer


_SOURCE_HEADING_RE: Final = re.compile(r"(?m)^#{1,6}\s*출처\b")
_SENTENCE_RE: Final = re.compile(r"[^.!?\n。]+(?:[.!?。]|$)")
_STRONG_CAUSAL_RE: Final = re.compile(
    r"(잠식\w*|흡수\w*|대체\w*|전환(?:시켰|시킨|했|한|하는|될|됨|되|됐)?|유발\w*|기인\w*|때문에|견인\w*|주도\w*)"
)
_HIRA_DERIVATIVE_RE: Final = re.compile(
    r"(환자\s*1인당|환자당|침투율|저침투|침투\s*단계|수요\s*여력|질환\s*수요\s*풀|"
    r"환자\s*기반\s*수요|처방\s*성과로\s*전환|수요\s*풀\s*대비)"
)
_HIRA_UNAVAILABLE_RE: Final = re.compile(r"(HIRA\s*조회\s*상태|환자수\s*수치\s*미반환|resultCode\s*=\s*99)")
_COMPETITIVE_MOVEMENT_RE: Final = re.compile(
    r"(?P<gainer>[가-힣A-Za-z0-9+._/-]+)\s+"
    r"(?:(?P<period>[12]\d{3}(?:-\d{2}|-Q\d)→[12]\d{3}(?:-\d{2}|-Q\d))\s+)?"
    r"상승폭\s+(?P<gain>[+-]?\d+(?:\.\d+)?%p)\s+"
    r"(?P<faller>[가-힣A-Za-z0-9+._/-]+)\s+"
    r"(?:[12]\d{3}(?:-\d{2}|-Q\d)→[12]\d{3}(?:-\d{2}|-Q\d)\s+)?"
    r"하락폭\s+(?P<loss>[+-]?\d+(?:\.\d+)?%p)"
    r"(?:\s+대비\s+(?P<ratio>[+-]?\d+(?:\.\d+)?%))?"
)
_PERIOD_RANGE_RE: Final = re.compile(r"[12]\d{3}(?:-\d{2}|-Q\d)→[12]\d{3}(?:-\d{2}|-Q\d)")
_UNSUPPORTED_PERIOD_PHRASE_RE: Final = re.compile(r"(전월|전분기)\s*대비")


@dataclass(frozen=True, slots=True)
class CompetitiveMovement:
    gainer: str
    period: str
    gain: str
    faller: str
    loss: str
    ratio: str


def apply_claim_guardrails(question: str, answer: str, fact_md: str) -> str:
    """Apply minimal claim guardrails before deterministic source rendering."""

    body, sources = _split_sources(answer)
    guarded = _remove_hira_unavailable_derivatives(body, fact_md)
    guarded = _downgrade_strong_causal_claims(guarded, fact_md)
    guarded = _downgrade_unsupported_period_phrases(guarded, fact_md)
    if sources:
        return cleanup_markdown_answer("\n\n".join((guarded.strip(), sources.strip())))
    return cleanup_markdown_answer(guarded.strip())


def _split_sources(answer: str) -> tuple[str, str]:
    match = _SOURCE_HEADING_RE.search(answer)
    if not match:
        return answer, ""
    return answer[: match.start()], answer[match.start() :]


def _remove_hira_unavailable_derivatives(body: str, fact_md: str) -> str:
    if not _HIRA_UNAVAILABLE_RE.search(fact_md):
        return body
    notice = "HIRA 환자수 수치가 미반환되어 침투율이나 환자당 처방액은 산출할 수 없습니다."
    notice_present = False
    removed = False
    kept_lines: list[str] = []
    for raw_line in body.splitlines():
        if _is_non_analysis_line(raw_line):
            kept_lines.append(raw_line)
            continue
        if _is_hira_unavailable_notice(raw_line):
            if not notice_present:
                kept_lines.append(notice)
                notice_present = True
            continue
        revised, line_removed = _drop_matching_sentences(raw_line, _HIRA_DERIVATIVE_RE)
        removed = removed or line_removed
        if revised.strip():
            kept_lines.append(revised)
    if removed and not notice_present:
        kept_lines.append(notice)
    return "\n".join(kept_lines)


def _downgrade_strong_causal_claims(body: str, fact_md: str) -> str:
    movement = _competitive_movement(fact_md)
    notice = _safe_competitive_notice(movement)
    notice_inserted = notice in body
    kept_lines: list[str] = []
    for raw_line in body.splitlines():
        if _is_non_analysis_line(raw_line):
            kept_lines.append(raw_line)
            continue
        revised_parts: list[str] = []
        removed = False
        for sentence in _sentence_parts(raw_line):
            if _STRONG_CAUSAL_RE.search(sentence):
                removed = True
                if not notice_inserted:
                    revised_parts.append(notice)
                    notice_inserted = True
                continue
            revised_parts.append(sentence.strip())
        revised = " ".join(part for part in revised_parts if part).strip()
        if revised:
            kept_lines.append(revised)
        elif removed and not notice_inserted:
            kept_lines.append(notice)
            notice_inserted = True
    return "\n".join(kept_lines)


def _drop_matching_sentences(line: str, pattern: re.Pattern[str]) -> tuple[str, bool]:
    kept: list[str] = []
    removed = False
    for sentence in _sentence_parts(line):
        if pattern.search(sentence):
            removed = True
            continue
        kept.append(sentence.strip())
    return " ".join(part for part in kept if part), removed


def _sentence_parts(line: str) -> tuple[str, ...]:
    decimal_dot = "__CLAIM_GUARD_DECIMAL_DOT__"
    protected = re.sub(r"(?<=\d)\.(?=\d)", decimal_dot, line)
    parts = tuple(
        match.group(0).replace(decimal_dot, ".").strip()
        for match in _SENTENCE_RE.finditer(protected)
        if match.group(0).strip()
    )
    return parts or ((line.strip(),) if line.strip() else ())


def _is_non_analysis_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if stripped.startswith("|") or re.fullmatch(r"\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?", stripped):
        return True
    if re.match(r"^#{1,6}\s+", stripped):
        return True
    if "http://" in stripped or "https://" in stripped:
        return True
    if "「" in stripped and "」" in stripped:
        return True
    return stripped.startswith(("- 뉴스:", "* 뉴스:"))


def _is_hira_unavailable_notice(line: str) -> bool:
    return "환자수 수치" in line and "미반환" in line and "산출" in line


def _competitive_movement(fact_md: str) -> CompetitiveMovement | None:
    match = _COMPETITIVE_MOVEMENT_RE.search(fact_md)
    if not match:
        return None
    return CompetitiveMovement(
        gainer=match.group("gainer"),
        period=match.group("period") or _first_period_range(fact_md),
        gain=match.group("gain"),
        faller=match.group("faller"),
        loss=match.group("loss"),
        ratio=match.group("ratio") or "",
    )


def _safe_competitive_notice(movement: CompetitiveMovement | None) -> str:
    if movement is None:
        return "집계 데이터는 지표 변화만 보여주며, 직접 처방 이동이나 원인은 확인할 수 없습니다."
    period = f" {movement.period}" if movement.period else ""
    return (
        f"같은 기간 {movement.gainer} 점유율은{period} {movement.gain}, {movement.faller}는 같은 기간 {movement.loss}로 "
        "반대 방향으로 변했습니다. 집계 데이터만으로 직접 처방 이동은 확인할 수 없습니다."
    )


def _first_period_range(fact_md: str) -> str:
    match = _PERIOD_RANGE_RE.search(fact_md)
    return match.group(0) if match else ""


def _downgrade_unsupported_period_phrases(body: str, fact_md: str) -> str:
    period = _first_period_range(fact_md)
    if not period or not _UNSUPPORTED_PERIOD_PHRASE_RE.search(body):
        return body
    revised_lines: list[str] = []
    for raw_line in body.splitlines():
        if _is_non_analysis_line(raw_line):
            revised_lines.append(raw_line)
        else:
            revised_lines.append(_UNSUPPORTED_PERIOD_PHRASE_RE.sub(f"{period} 기준", raw_line))
    return "\n".join(revised_lines)
