from __future__ import annotations

import re


SOURCE_LABELS = {
    "mart": "내부 데이터마트",
    "nedrug": "식품의약품안전처",
    "hira": "건강보험심사평가원",
    "openfda": "FDA",
    "clinicaltrials": "ClinicalTrials.gov",
    "web": "웹 뉴스",
    "patent": "식품의약품안전처 의약품 특허목록",
    "patent:kr_primary": "식품의약품안전처 의약품 특허목록",
    "patent:us_secondary": "FDA Orange Book",
    "patent:news": "특허·분쟁 동향 (웹 뉴스)",
}
PATENT_LANES = ("kr_primary", "us_secondary", "news")

_SOURCE_ALIASES = {
    "mart": ("내부 데이터마트", "mart"),
    "nedrug": ("식품의약품안전처", "식약처", "nedrug"),
    "hira": ("건강보험심사평가원", "hira"),
    "openfda": ("fda", "openfda"),
    "clinicaltrials": ("clinicaltrials.gov", "clinicaltrials"),
    "web": ("웹 뉴스", "웹 자료", "web", "web_search", "tavily"),
    "patent": (
        "식품의약품안전처 의약품 특허목록",
        "특허 자료",
        "patent",
        "mfds_patent",
    ),
}

_INLINE_SOURCE_RE = re.compile(r"\[출처:\s*([^\]]+?)\s*\]")
_INTERNAL_TOOL_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])"
    r"(?P<tool>web_search|tavily(?:_[a-z0-9_]+)?|"
    r"mfds_patent(?:_[a-z0-9_]+)?|mcp_[a-z0-9_]+)"
    r"(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)


def public_source_label(source: str) -> str:
    return SOURCE_LABELS[source]


def public_source_aliases(source: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys((SOURCE_LABELS[source], *_SOURCE_ALIASES[source])))


def patent_lane_label(lane: str) -> str:
    return SOURCE_LABELS[f"patent:{lane}"]


def normalize_public_source_surface(text: str) -> tuple[str, int]:
    """Replace release-only source identifiers with the shared public labels."""

    alias_to_label = {
        alias.casefold(): SOURCE_LABELS[source]
        for source, aliases in _SOURCE_ALIASES.items()
        for alias in aliases
    }
    replacements = 0

    def replace_tool(match: re.Match[str]) -> str:
        nonlocal replacements
        replacements += 1
        tool = match.group("tool").casefold()
        if tool.startswith(("web_search", "tavily")):
            return SOURCE_LABELS["web"]
        if tool.startswith("mfds_patent"):
            return SOURCE_LABELS["patent"]
        return "외부 근거"

    normalized = _INTERNAL_TOOL_TOKEN_RE.sub(replace_tool, text)

    def replace_inline_source(match: re.Match[str]) -> str:
        nonlocal replacements
        raw = " ".join(match.group(1).split())
        label = alias_to_label.get(raw.casefold(), raw)
        replacements += int(label != raw)
        return f"[출처: {label}]"

    return _INLINE_SOURCE_RE.sub(replace_inline_source, normalized), replacements


__all__ = [
    "PATENT_LANES",
    "SOURCE_LABELS",
    "normalize_public_source_surface",
    "patent_lane_label",
    "public_source_aliases",
    "public_source_label",
]
