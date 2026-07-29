from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

EXPECTED_DETAIL_HEADING: Final = "보험인정기준 상세내용"
EMPTY_RAW_TEXT_REASON: Final = "ingress:empty_raw_text"
MALFORMED_HTML_REASON: Final = "ingress:malformed_html"
MISSING_EXPECTED_STRUCTURE_REASON: Final = "ingress:missing_expected_structure"
HTTP_ERROR_PAGE_REASON: Final = "ingress:http_error_page"

_HTTP_ERROR_PAGE_RE: Final = re.compile(
    r"(?:"
    r"internal\s+server\s+error|"
    r"http\s+status\s+5\d{2}|"
    r"bad\s+gateway|"
    r"service\s+unavailable|"
    r"요청하신\s+페이지를\s+찾을\s+수\s+없|"
    r"서비스\s+이용에\s+불편을\s+드려"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class DetailIngressEvidence:
    """Evidence required to distinguish a valid clause-free notice from broken input."""

    raw_text: str
    h1_headings: tuple[str, ...]
    structural_html_valid: bool
    content_container_present: bool


def failed_ingress_reason(evidence: DetailIngressEvidence) -> str | None:
    """Return a typed ingress failure; absence of benefit clauses is not a failure."""

    if not evidence.raw_text:
        return EMPTY_RAW_TEXT_REASON
    if _HTTP_ERROR_PAGE_RE.search(evidence.raw_text) is not None:
        return HTTP_ERROR_PAGE_REASON
    if not evidence.structural_html_valid:
        return MALFORMED_HTML_REASON
    if not evidence.content_container_present:
        return MISSING_EXPECTED_STRUCTURE_REASON
    if EXPECTED_DETAIL_HEADING not in evidence.h1_headings:
        return MISSING_EXPECTED_STRUCTURE_REASON
    body_text = evidence.raw_text.removeprefix(EXPECTED_DETAIL_HEADING).strip()
    if not body_text:
        return MISSING_EXPECTED_STRUCTURE_REASON
    return None
