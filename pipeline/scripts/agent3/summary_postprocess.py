from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any


@dataclass(frozen=True, slots=True)
class CandidateMatchError(RuntimeError):
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class SummaryValidationError(RuntimeError):
    brand: str
    errors: list[str]

    def __str__(self) -> str:
        return f"wf316 summary validation failed for {self.brand}: {self.errors}"


NUMBER_KEYS = ("value_current", "value_baseline", "delta_abs", "delta_pct")
RAW_DECIMAL_RE = re.compile(r"\d+\.\d{5,}")
DISPLAY_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])[-+]?(?:\d[\d,]*(?:\.\d+)?)\s*(?:억원|만원|원|%|건|개|명|MG|MCG|G|ML|IU)",
    re.IGNORECASE,
)
MONEY_RE = re.compile(r"^(?P<number>[-+]?\d[\d,]*(?:\.\d+)?)\s*(?P<unit>억원|만원|원)$")
PERCENT_RE = re.compile(r"^(?P<number>[-+]?\d+(?:\.\d+)?)\s*%$")


def inject_candidate_numbers(summary: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    enriched = deepcopy(summary)
    items = enriched.get("strength_items")
    if not isinstance(items, list):
        raise CandidateMatchError("strength_items must be a list")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise CandidateMatchError(f"strength_items[{index}] must be an object")
        candidate = _match_candidate(item, candidates)
        item["numbers"] = {key: candidate.get(key) for key in NUMBER_KEYS}
    return enriched


def validate_display_number_narratives(
    summary: dict[str, Any],
    candidates: list[dict[str, Any]],
    profile: dict[str, Any] | None = None,
) -> list[str]:
    errors: list[str] = []
    items = summary.get("strength_items")
    if not isinstance(items, list):
        return ["strength_items must be a list"]
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"item {index} must be an object")
            continue
        narrative = str(item.get("narrative") or "")
        if RAW_DECIMAL_RE.search(narrative):
            errors.append(f"item {index} narrative contains raw decimal: {narrative}")
            continue
        try:
            candidate = _match_candidate(item, candidates)
        except CandidateMatchError as exc:
            errors.append(str(exc))
            continue
        display_values = _display_number_tokens(candidate)
        label_values = _label_number_tokens(candidate, profile or summary.get("profile_display"))
        for token in DISPLAY_NUMBER_RE.findall(narrative):
            if (
                _normalize_token(token) not in {_normalize_token(value) for value in label_values}
                and not _token_matches_display_number(token, display_values)
            ):
                errors.append(f"item {index} narrative number is not in display_numbers: {token}")
    return errors


def _match_candidate(item: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    index_value = item.get("candidate_index")
    if isinstance(index_value, int) and 0 <= index_value < len(candidates):
        return candidates[index_value]
    item_slice = item.get("slice")
    item_metric = item.get("metric")
    matches = [
        candidate
        for candidate in candidates
        if candidate.get("slice") == item_slice and candidate.get("metric") == item_metric
    ]
    if len(matches) == 1:
        return matches[0]
    raise CandidateMatchError(f"candidate match failed for slice={item_slice!r}, metric={item_metric!r}")


def _label_number_tokens(candidate: dict[str, Any], profile: Any) -> set[str]:
    """Return numeric tokens that are labels, not metric values."""
    sources = [candidate.get("slice"), *_profile_strings(profile)]
    return {token for source in sources if isinstance(source, str) for token in DISPLAY_NUMBER_RE.findall(source)}


def _display_number_tokens(candidate: dict[str, Any]) -> set[str]:
    tokens = {
        str(value)
        for value in (candidate.get("display_numbers") or {}).values()
        if value not in (None, "")
    }
    for value in (candidate.get("display_number_aliases") or {}).values():
        if isinstance(value, list):
            tokens.update(str(item) for item in value if item not in (None, ""))
        elif value not in (None, ""):
            tokens.add(str(value))
    return tokens


def _token_matches_display_number(token: str, display_values: set[str]) -> bool:
    normalized = _normalize_token(token)
    normalized_values = {_normalize_token(value) for value in display_values}
    if normalized in normalized_values:
        return True
    parsed = _parse_display_value(token)
    if parsed is None:
        return False
    return any(parsed == _parse_display_value(value) for value in display_values)


def _normalize_token(token: str) -> str:
    return re.sub(r"\s+", "", token)


def _parse_display_value(token: str) -> tuple[str, int] | tuple[str, float] | None:
    compact = _normalize_token(token)
    money = MONEY_RE.match(compact)
    if money:
        number = float(money.group("number").replace(",", ""))
        unit = money.group("unit")
        multipliers = {"원": 1, "만원": 10_000, "억원": 100_000_000}
        return ("money", round(number * multipliers[unit]))
    percent = PERCENT_RE.match(compact)
    if percent:
        return ("percent", round(float(percent.group("number")), 6))
    return None


def _profile_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        strings: list[str] = []
        for nested in value.values():
            strings.extend(_profile_strings(nested))
        return strings
    if isinstance(value, list):
        strings = []
        for nested in value:
            strings.extend(_profile_strings(nested))
        return strings
    return []
