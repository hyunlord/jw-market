from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from jw_chat_agent_poc.resolver import BrandResolver


LOGGER = logging.getLogger(__name__)
WEB_SUBJECT_NOT_MATCHED = "web_subject_not_matched"
WEB_MEDICAL_DOMAIN_NOT_MATCHED = "web_medical_domain_not_matched"
_DISEASE_CODE_RE = re.compile(r"(?<![0-9A-Za-z])([A-Za-z]\d{2}(?:\.?\d)?)(?![0-9A-Za-z])")
_ATC3_CODE_RE = re.compile(r"(?<![0-9A-Za-z])([A-Za-z]\d{2}[A-Za-z])(?![0-9A-Za-z])")
_MEDICAL_DOMAIN_TERMS = (
    "환자",
    "상병",
    "진료",
    "급여",
    "질병",
    "입원",
    "외래",
    "내원",
    "의료",
)
_PHARMACEUTICAL_DOMAIN_TERMS = (
    "atc",
    "의약품",
    "약품",
    "약물",
    "제제",
    "성분",
    "처방",
    "비타민",
    "pharma",
    "drug",
    "medicine",
)


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
    disease_codes = tuple(
        dict.fromkeys(
            _normalized_match_text(match.group(1))
            for match in _DISEASE_CODE_RE.finditer(question)
        )
    )
    atc_codes = tuple(
        dict.fromkeys(
            _normalized_match_text(match.group(1))
            for match in _ATC3_CODE_RE.finditer(question)
        )
    )
    accepted: list[tuple[int, Mapping[str, object]]] = []
    excluded: list[WebRelevanceExclusion] = []
    for rank, item in enumerate(items, start=1):
        basis = _normalized_match_text(
            f"{item.get('title') or ''} {item.get('snippet') or item.get('content') or ''}"
        )
        subject_matched = not subject_terms or any(term in basis for term in subject_terms)
        medical_domain_matched = (
            (not disease_codes or any(term in basis for term in _MEDICAL_DOMAIN_TERMS))
            and (
                not atc_codes
                or any(term in basis for term in _PHARMACEUTICAL_DOMAIN_TERMS)
            )
        )
        if subject_matched and medical_domain_matched:
            accepted.append((rank, item))
            continue
        reason_code = (
            WEB_MEDICAL_DOMAIN_NOT_MATCHED
            if subject_matched and not medical_domain_matched
            else WEB_SUBJECT_NOT_MATCHED
        )
        exclusion = WebRelevanceExclusion(
            rank=rank,
            url="",
            title="",
            reason_code=reason_code,
        )
        excluded.append(exclusion)
        LOGGER.info(
            "web result excluded reason_code=%s rank=%d",
            exclusion.reason_code,
            exclusion.rank,
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
    terms.extend(match.group(1) for match in _DISEASE_CODE_RE.finditer(question))
    terms.extend(match.group(1) for match in _ATC3_CODE_RE.finditer(question))

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
    "WEB_MEDICAL_DOMAIN_NOT_MATCHED",
    "WEB_SUBJECT_NOT_MATCHED",
    "WebRelevanceDecision",
    "WebRelevanceExclusion",
    "filter_web_results",
    "web_subject_terms",
]
