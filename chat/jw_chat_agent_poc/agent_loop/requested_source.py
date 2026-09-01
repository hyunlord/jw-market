from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, Final

_SOURCE_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (
        "ubist",
        re.compile(r"(?<![A-Za-z0-9_])UBIST(?![A-Za-z0-9_])|유비스트", re.IGNORECASE),
    ),
    (
        "iqvia_nsa",
        re.compile(
            r"(?<![A-Za-z0-9_])IQVIA(?:\s+NSA)?(?![A-Za-z0-9_])|아이큐비아",
            re.IGNORECASE,
        ),
    ),
)
_SOURCE_ALIASES: Final[dict[str, str]] = {
    "ubist": "ubist",
    "유비스트": "ubist",
    "iqvia": "iqvia_nsa",
    "iqvia_nsa": "iqvia_nsa",
    "iqvia nsa": "iqvia_nsa",
    "아이큐비아": "iqvia_nsa",
}
SOURCE_BASIS_LABEL: Final[dict[str, str]] = {
    "ubist": "원외 처방(UBIST)",
    "iqvia_nsa": "제조사 출하(IQVIA NSA)",
}
SOURCE_DOMAIN_NOTE: Final[str] = (
    "이 시장은 {available} 기준으로 정의돼 있습니다. "
    "측정 대상이 다른 {missing} 기준과는 값이 서로 다르며, "
    "두 기준 사이에는 유통 재고, 병원 직거래, 반품, 원내 처방이 있습니다."
)


def extract_requested_sources(question: str) -> tuple[str, ...]:
    matches: list[tuple[int, str]] = []
    for source, pattern in _SOURCE_PATTERNS:
        matches.extend((match.start(), source) for match in pattern.finditer(question))
    return tuple(dict.fromkeys(source for _, source in sorted(matches)))


def normalize_source(value: object) -> str | None:
    normalized = str(value or "").strip().lower().replace("-", "_")
    return _SOURCE_ALIASES.get(normalized)


def requested_source_for_query(
    requested_sources: tuple[str, ...],
    available_sources: Iterable[str] | None,
) -> str | None:
    del available_sources
    if len(requested_sources) != 1:
        return None
    return requested_sources[0]


def served_source_from_calls(calls: Iterable[Mapping[str, Any]]) -> str | None:
    sources = served_sources_from_calls(calls)
    return sources[0] if len(sources) == 1 else None


def served_sources_from_calls(calls: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    sources: list[str] = []
    for call in calls:
        if str(call.get("status") or "").lower() in {
            "error",
            "no_data",
            "query_failed",
            "unsupported",
        }:
            continue
        render_data = call.get("render_data")
        query_spec = (
            render_data.get("query_spec")
            if isinstance(render_data, Mapping)
            else None
        )
        raw_source = (
            query_spec.get("source")
            if isinstance(query_spec, Mapping)
            else None
        )
        if raw_source is None:
            arguments = call.get("arguments")
            raw_source = (
                arguments.get("source")
                if isinstance(arguments, Mapping)
                else call.get("source")
            )
        source = normalize_source(raw_source)
        if source is not None:
            sources.append(source)
    return tuple(dict.fromkeys(sources))


def source_substitution(
    requested_sources: tuple[str, ...],
    calls: Iterable[Mapping[str, Any]],
) -> tuple[str, str] | None:
    if len(requested_sources) != 1:
        return None
    requested = requested_sources[0]
    served_sources = served_sources_from_calls(calls)
    substituted = next(
        (source for source in served_sources if source != requested),
        None,
    )
    if substituted is None:
        return None
    return requested, substituted


def source_substitution_message(requested_source: str, served_source: str) -> str:
    requested = SOURCE_BASIS_LABEL.get(requested_source, requested_source)
    served = SOURCE_BASIS_LABEL.get(served_source, served_source)
    return (
        f"요청하신 {requested} 기준과 확인된 {served} 기준이 다릅니다. "
        f"{served} 값으로 대체하지 않습니다."
    )


def requested_source_unavailable_message(requested_source: str) -> str:
    requested = SOURCE_BASIS_LABEL.get(requested_source, requested_source)
    return (
        f"요청하신 {requested} 기준은 현재 지원되지 않아 "
        "다른 소스 값으로 대체하지 않습니다."
    )


def source_basis_notice(source: str) -> str | None:
    label = SOURCE_BASIS_LABEL.get(source)
    return f"{label} 기준으로 답합니다." if label is not None else None


def source_domain_note(missing_sources: tuple[str, ...]) -> str | None:
    known = [source for source in missing_sources if source in SOURCE_BASIS_LABEL]
    if len(known) != 1 or len(missing_sources) != 1:
        return None
    available = next(source for source in SOURCE_BASIS_LABEL if source != known[0])
    return SOURCE_DOMAIN_NOTE.format(
        available=SOURCE_BASIS_LABEL[available],
        missing=SOURCE_BASIS_LABEL[known[0]],
    )


def source_mismatch_notice(requested_source: str, served_source: str) -> str | None:
    requested = SOURCE_BASIS_LABEL.get(requested_source)
    served = SOURCE_BASIS_LABEL.get(served_source)
    if requested is None or served is None or requested_source == served_source:
        return None
    return (
        f"요청은 {requested} 기준이며, 이 응답은 {served} 기준입니다. "
        "측정 대상이 다른 두 기준은 유통 재고, 병원 직거래, 반품, "
        "원내 처방의 영향으로 값이 서로 다를 수 있습니다."
    )
