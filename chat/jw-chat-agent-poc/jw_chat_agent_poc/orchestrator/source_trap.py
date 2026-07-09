from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RequestedSource:
    key: str
    label: str
    tokens: tuple[str, ...]
    alternative_label: str


REQUESTED_SOURCE_REGISTRY: tuple[RequestedSource, ...] = (
    RequestedSource(
        key="cortellis",
        label="Cortellis",
        tokens=("cortellis", "코텔리스"),
        alternative_label="ClinicalTrials/MFDS",
    ),
    RequestedSource(
        key="datamonitor",
        label="Datamonitor",
        tokens=("datamonitor", "데이터모니터"),
        alternative_label="UBIST/IQVIA",
    ),
    RequestedSource(
        key="kol",
        label="KOL 자문",
        tokens=("kol", "전문가", "자문", "의견"),
        alternative_label="보유 내부 지표/뉴스",
    ),
    RequestedSource(
        key="nccn",
        label="NCCN/가이드라인",
        tokens=("nccn", "가이드라인", "치료 지침", "guideline"),
        alternative_label="HIRA/보유 내부 지표",
    ),
)

_SOURCE_TRAP_MARKER = "요청 소스 미보유"
_ALT_REFERENCE_HEADING = "### 대체 참고"
_CSD_AGGREGATE_TOKENS = ("영업활동", "영업 활동", "상기 콜", "콜 수", "콜수", "활동량", "디테일링")
_CSD_DETAIL_TOKENS = ("impact level", "impact", "HCP", "hcp", "의사별", "의사 별", "기관별", "기관 별", "병원별", "병원 별")
_CLINICAL_REFERENCE_RE = re.compile(r"(?:ClinicalTrials|MFDS|NCT\d+|식약처|임상시험|임상)", re.IGNORECASE)
_CORTELLIS_UNSUPPORTED_CLAIM_RE = re.compile(
    r"(?:Venetoclax|GSK2402968|DEB025|NCT\d+).*(?:리바로|이상지질혈증|적응증\s*확장|상업|경쟁\s*압력|위협)",
    re.IGNORECASE,
)
_SOURCE_HEADING_RE = re.compile(r"\n##\s*(?:출처|처리\s*시간)\b")


def requested_unavailable_source(question: str) -> RequestedSource | None:
    question_lower = question.lower()
    for source in REQUESTED_SOURCE_REGISTRY:
        if any(token.lower() in question_lower for token in source.tokens):
            return source
    return None


def requested_csd_aggregate(question: str) -> bool:
    return any(token in question for token in _CSD_AGGREGATE_TOKENS)


def requested_csd_unsupported_detail(question: str) -> bool:
    lowered = question.lower()
    return any(token.lower() in lowered for token in _CSD_DETAIL_TOKENS)


def apply_requested_source_trap_gate(question: str, answer: str) -> str:
    """Keep unavailable requested-source questions from masquerading alternate sources.

    The gate is intentionally answer-path only: it does not mutate tool payloads or
    source facts. Requested external sources listed in REQUESTED_SOURCE_REGISTRY are
    not connected production data sources, so any ClinicalTrials/MFDS material must
    remain an alternate reference and must not inherit the requested source label.
    """

    source = requested_unavailable_source(question)
    if source is None:
        return answer.strip()
    text = _remove_unsupported_cortellis_packaging(answer.strip(), source)
    text = _rewrite_requested_source_labels(text, source)
    text = _structure_alternative_reference_tables(text, source)
    text = _ensure_first_sentence(text, source)
    text = _ensure_alternative_reference_note(text, source)
    text = _compact_when_unavailable_layer_exists(text, source)
    return _cleanup(text)


def _ensure_first_sentence(answer: str, source: RequestedSource) -> str:
    first_sentence = f"{source.label} 데이터는 현재 운영 데이터에 미보유입니다."
    stripped = answer.strip()
    if stripped.startswith(first_sentence):
        return stripped
    return "\n\n".join(part for part in (first_sentence, stripped) if part)


def _rewrite_requested_source_labels(answer: str, source: RequestedSource) -> str:
    text = answer
    label_pattern = re.escape(source.label)
    if source.key == "nccn":
        label_pattern = r"(?:NCCN|가이드라인|치료\s*지침)"
    elif source.key == "kol":
        label_pattern = r"(?:KOL|전문가|자문)"
    text = re.sub(
        rf"{label_pattern}\s*기준",
        f"{source.alternative_label} 대체 참고({source.label} 아님)",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        rf"(?m)^(#{1,6}\s*){label_pattern}\s*(?:현황|표|결과)",
        rf"\1{source.alternative_label} 대체 참고({source.label} 아님)",
        text,
        flags=re.IGNORECASE,
    )
    return text


def _remove_unsupported_cortellis_packaging(answer: str, source: RequestedSource) -> str:
    if source.key != "cortellis":
        return answer
    kept: list[str] = []
    for line in answer.splitlines():
        if _CORTELLIS_UNSUPPORTED_CLAIM_RE.search(line):
            continue
        if "적응증 확장 가능성" in line or "상업 경쟁 압력" in line:
            continue
        kept.append(line)
    return "\n".join(kept)


def _ensure_alternative_reference_note(answer: str, source: RequestedSource) -> str:
    if _ALT_REFERENCE_HEADING in answer:
        return answer
    if not _has_alternative_reference(answer, source):
        return answer
    note = "\n".join(
        (
            _ALT_REFERENCE_HEADING,
            f"- {source.alternative_label} 결과는 {source.label} 데이터가 아니므로 요청 소스 기준 결론으로 승격하지 않습니다.",
        )
    )
    match = _SOURCE_HEADING_RE.search(answer)
    if match is None:
        return "\n\n".join((answer, note))
    return "\n\n".join((answer[: match.start()].strip(), note, answer[match.start() :].strip()))


def _structure_alternative_reference_tables(answer: str, source: RequestedSource) -> str:
    if source.key != "cortellis" or _ALT_REFERENCE_HEADING in answer:
        return answer
    marker = "\n### 임상시험"
    if marker not in answer:
        return answer
    note = (
        "\n"
        f"{_ALT_REFERENCE_HEADING}: {source.alternative_label} ({source.label} 아님)\n"
        f"- {source.alternative_label} 결과는 {source.label} 데이터가 아니므로 요청 소스 기준 결론으로 승격하지 않습니다.\n"
    )
    return answer.replace(marker, f"{note}{marker}", 1)


def _has_alternative_reference(answer: str, source: RequestedSource) -> bool:
    if source.key == "cortellis":
        return bool(_CLINICAL_REFERENCE_RE.search(answer))
    if source.key == "datamonitor":
        return any(token in answer for token in ("UBIST", "IQVIA", "매출", "시장점유율"))
    if source.key == "kol":
        return any(token in answer for token in ("UBIST", "IQVIA", "뉴스", "시장점유율", "매출"))
    if source.key == "nccn":
        return any(token in answer for token in ("HIRA", "UBIST", "IQVIA", "환자", "매출"))
    return False


def _compact_when_unavailable_layer_exists(answer: str, source: RequestedSource) -> str:
    five_step = _extract_block(answer, "### 미보유 데이터 처리", ("\n### 대체 참고", "\n## 출처", "\n## 처리 시간"))
    if not five_step:
        return answer
    sources = _extract_block(answer, "## 출처", ("\n## 처리 시간",))
    parts = [
        f"{source.label} 데이터는 현재 운영 데이터에 미보유입니다.",
        five_step,
        _alternative_reference_note(source),
        sources,
    ]
    return "\n\n".join(part for part in parts if part.strip())


def _alternative_reference_note(source: RequestedSource) -> str:
    return "\n".join(
        (
            _ALT_REFERENCE_HEADING,
            f"- {source.alternative_label} 결과는 {source.label} 데이터가 아니므로 요청 소스 결론으로 승격하지 않습니다.",
        )
    )


def _extract_block(answer: str, start_marker: str, end_markers: tuple[str, ...]) -> str:
    start = answer.find(start_marker)
    if start < 0:
        return ""
    end_candidates = [index for marker in end_markers if (index := answer.find(marker, start + len(start_marker))) >= 0]
    end = min(end_candidates) if end_candidates else len(answer)
    return answer[start:end].strip()


def _cleanup(markdown: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", markdown.strip())
    return text.strip()
