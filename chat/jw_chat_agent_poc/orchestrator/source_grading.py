from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
import re
from typing import Final
from urllib.parse import urlparse


class SourceGrade(StrEnum):
    AUTHORITATIVE = "AUTHORITATIVE"
    SUPPLEMENTARY = "SUPPLEMENTARY"
    UNVERIFIED = "UNVERIFIED"


OFFICIAL_WEB_DOMAINS_BY_SOURCE: Final[dict[str, tuple[str, ...]]] = {
    "hira": ("hira.or.kr",),
    "regulatory": ("mfds.go.kr", "nedrug.mfds.go.kr"),
    "clinical_trials": ("clinicaltrials.gov", "cris.nih.go.kr"),
    "government": ("go.kr",),
    "academic": ("ac.kr",),
}

_GRADE_A_DOMAINS: Final[tuple[str, ...]] = (
    "hira.or.kr",
    "mfds.go.kr",
    "clinicaltrials.gov",
)
_GRADE_B_DOMAINS: Final[tuple[str, ...]] = (
    "ac.kr",
    "or.kr",
    "go.kr",
    "snuh.org",
    "snubh.org",
    "amc.seoul.kr",
)

_EXPLICIT_AUTHORITY_PATTERNS: Final[dict[str, re.Pattern[str]]] = {
    "hira": re.compile(
        r"(?:(?<![A-Za-z0-9_])HIRA(?![A-Za-z0-9_])|심평원|건강보험심사평가원)",
        re.IGNORECASE,
    ),
    "regulatory": re.compile(
        r"(?:(?<![A-Za-z0-9_])MFDS(?![A-Za-z0-9_])|식약처|식품의약품안전처)",
        re.IGNORECASE,
    ),
    "clinical_trials": re.compile(
        r"(?:ClinicalTrials\.gov|(?<![A-Za-z0-9_])CRIS(?![A-Za-z0-9_]))",
        re.IGNORECASE,
    ),
}

_AUTHORITATIVE_SOURCE_TOKENS: Final[tuple[str, ...]] = (
    "hira",
    "건강보험심사평가원",
    "mfds",
    "식품의약품안전처",
    "clinicaltrials",
    "cris",
    "iqvia",
    "ubist",
    "mart",
)

_AUTHORITATIVE_TOOL_PREFIXES: Final[tuple[str, ...]] = (
    "get_brand_",
    "get_market_",
    "get_filtered_",
    "hira_",
    "mfds_",
    "clinicaltrials_",
    "ct_",
)

_AUTHORITATIVE_DERIVED_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "agent_calculation",
        "market_scope",
        "market_summary",
        "get_market_landscape",
    }
)


def grade_web_url(url: str) -> SourceGrade:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not hostname:
        return SourceGrade.UNVERIFIED
    if any(_hostname_matches(hostname, domain) for domain in _GRADE_A_DOMAINS):
        return SourceGrade.AUTHORITATIVE
    if any(_hostname_matches(hostname, domain) for domain in _GRADE_B_DOMAINS):
        return SourceGrade.SUPPLEMENTARY
    return SourceGrade.UNVERIFIED


def is_official_web_url(url: str, *, source_domain: str | None = None) -> bool:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not hostname:
        return False
    domains = (
        OFFICIAL_WEB_DOMAINS_BY_SOURCE.get(source_domain, ())
        if source_domain
        else tuple(
            domain
            for configured in OFFICIAL_WEB_DOMAINS_BY_SOURCE.values()
            for domain in configured
        )
    )
    return any(_hostname_matches(hostname, domain) for domain in domains)


def official_web_domains(source_domain: str) -> tuple[str, ...]:
    return OFFICIAL_WEB_DOMAINS_BY_SOURCE.get(source_domain, ())


def requested_authority_source_explicit(question: str, *, source_domain: str) -> bool:
    pattern = _EXPLICIT_AUTHORITY_PATTERNS.get(source_domain)
    return bool(pattern and pattern.search(question))


def grade_evidence_source(*, tool: str, source: str, url: str = "") -> SourceGrade:
    normalized_tool = tool.strip().lower()
    normalized_source = source.strip().lower()
    if normalized_tool == "web_search" or normalized_source == "web_search" or url:
        return grade_web_url(url)
    if normalized_tool.startswith(_AUTHORITATIVE_TOOL_PREFIXES):
        return SourceGrade.AUTHORITATIVE
    if normalized_tool in _AUTHORITATIVE_DERIVED_TOOLS:
        return SourceGrade.AUTHORITATIVE
    if any(token in normalized_source for token in _AUTHORITATIVE_SOURCE_TOKENS):
        return SourceGrade.AUTHORITATIVE
    return SourceGrade.UNVERIFIED


def is_web_search_call(call: Mapping[str, object]) -> bool:
    return (
        str(call.get("tool") or "").strip().lower() == "web_search"
        or str(call.get("source") or "").strip().lower() == "web_search"
    )


def _hostname_matches(hostname: str, domain: str) -> bool:
    return hostname == domain or hostname.endswith(f".{domain}")
