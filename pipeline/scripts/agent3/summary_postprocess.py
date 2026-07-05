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
DISPLAY_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?:억원|원|%|건|개|명)")


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


def validate_display_number_narratives(summary: dict[str, Any], candidates: list[dict[str, Any]]) -> list[str]:
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
        display_values = {
            str(value)
            for value in (candidate.get("display_numbers") or {}).values()
            if value not in (None, "")
        }
        for token in DISPLAY_NUMBER_RE.findall(narrative):
            if token not in display_values:
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
