from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import logging
import re

from jw_chat_agent_poc.resolver import BrandResolver


LOGGER = logging.getLogger(__name__)
WEB_SUBJECT_NOT_MATCHED = "web_subject_not_matched"


@dataclass(frozen=True, slots=True)
class WebRelevanceExclusion:
    rank: int
    url: str
    title: str
    reason_code: str


@dataclass(frozen=True, slots=True)
class WebRelevanceDecision:
    accepted: tuple[tuple[int, Mapping[str, object]], ...]
    exclusions: tuple[WebRelevanceExclusion, ...]
    subject_terms: tuple[str, ...]


def filter_web_results(
    question: str,
    items: Sequence[Mapping[str, object]],
    *,
    identity_values: Sequence[object] = (),
) -> WebRelevanceDecision:
    subject_terms = web_subject_terms(question, identity_values=identity_values)
    accepted: list[tuple[int, Mapping[str, object]]] = []
    excluded: list[WebRelevanceExclusion] = []
    for rank, item in enumerate(items, start=1):
        basis = _normalized_match_text(
            f"{item.get('title') or ''} {item.get('snippet') or item.get('content') or ''}"
        )
        if not subject_terms or any(term in basis for term in subject_terms):
            accepted.append((rank, item))
            continue
        exclusion = WebRelevanceExclusion(
            rank=rank,
            url=str(item.get("url") or "").strip(),
            title=str(item.get("title") or "").strip(),
            reason_code=WEB_SUBJECT_NOT_MATCHED,
        )
        excluded.append(exclusion)
        LOGGER.info(
            "web result excluded reason_code=%s rank=%d url=%s title=%s",
            exclusion.reason_code,
            exclusion.rank,
            exclusion.url,
            exclusion.title,
        )
    return WebRelevanceDecision(tuple(accepted), tuple(excluded), subject_terms)


def web_subject_terms(
    question: str,
    *,
    identity_values: Sequence[object] = (),
) -> tuple[str, ...]:
    terms: list[str] = []
    try:
        resolutions = BrandResolver(mode="fixture").resolve_many(
            question,
            allow_default=False,
        )
    except (LookupError, OSError, TypeError, ValueError):
        resolutions = ()
    for resolution in resolutions:
        terms.extend((resolution.canonical_brand, *resolution.molecule_en, *resolution.atc))
        for market_name in (resolution.market_name, *resolution.market_names):
            if not market_name:
                continue
            terms.append(market_name)
            terms.extend(
                token
                for token in market_name.split()
                if token not in {"시장", "치료제"}
            )
    for value in identity_values:
        _append_term(terms, value)

    normalized = (
        _normalized_match_text(term)
        for term in terms
        if isinstance(term, str) and len(term.strip()) >= 2
    )
    return tuple(dict.fromkeys(term for term in normalized if term))


def _append_term(terms: list[str], value: object) -> None:
    if isinstance(value, str):
        terms.append(value)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        terms.extend(str(item) for item in value if isinstance(item, str))


def _normalized_match_text(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", value.casefold())


__all__ = [
    "WEB_SUBJECT_NOT_MATCHED",
    "WebRelevanceDecision",
    "WebRelevanceExclusion",
    "filter_web_results",
    "web_subject_terms",
]
