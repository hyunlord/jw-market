from __future__ import annotations

import copy
import re
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from .config import ValidatorConfig
from .metric_validator import (
    FORECAST_VIEW_PATH_RE,
    COMPACT_VIEW_TAG_RE,
    _has_view_label,
    _requires_view_label,
    _view_label_parts_from_path,
    build_bundle_path_index,
    extract_numbers,
    find_match_unit_aware,
)


ZERO_DECIMAL_KRW_QTY_RE = re.compile(r"(?<!\d)(\d[\d,]*)\.0+\s*(원|개)")
DECIMAL_KRW_QTY_UNIT_RE = re.compile(
    r"(?<!\d)(?P<number>\d[\d,]*\.\d+)\s*(?P<unit>원|개)"
)
KOREAN_LARGE_UNIT_NUMBER_RE = re.compile(
    r"(?<![\d.])"
    r"(?P<expr>(?:\d[\d,]*(?:\.\d+)?\s*(?:억|만|천)(?![가-힣A-Za-z])\s*)+(?:\d[\d,]*(?:\.\d+)?)?)"
    r"(?P<unit>\s*(?:원|KRW|unit|개|정|캡슐|바이알|포|병|건|Rx|dosage unit|counting unit))?",
    re.IGNORECASE,
)
KOREAN_LARGE_UNIT_TOKEN_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(억|만|천)?(?![가-힣A-Za-z])")
FORECAST_NUMBER_TAG_RE = re.compile(
    r"(?<![\d,.])(?P<number>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<unit>\s*(?:원|KRW|unit|개|정|캡슐|바이알|포|병|건|Rx|dosage unit|counting unit))?"
    r"(?P<tag>\s*\([^)]{1,90}\))?"
    r"(?!\s*(?:년|월|분기|일))",
    re.IGNORECASE,
)
VALID_COMPACT_TAG_RE = re.compile(r"\((?:ML|CD)·(?:IQVIA|UBIST)·[^)·]+·(?:\d{4}-Q\d|\d{4}-\d{2})\)")
SIGNLESS_PERCENT_WITH_COMPACT_TAG_RE = re.compile(
    r"(?<![+-])(?P<number>\d+(?:\.\d+)?)%(?P<tag>\((?:ML|CD)·(?:IQVIA|UBIST)·[^)]{1,80}\))"
)
FORECAST_BASIS_RE = re.compile(
    r"(?<![\d,.])(?P<number>[+-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?|[+-]?\d+(?:\.\d+)?)"
    r"(?P<tag>\((?:ML|CD)·(?:IQVIA|UBIST)·[^)]{1,80}\))"
)
NEGATIVE_TREND_CONTEXT_RE = re.compile(r"(감소|하락|축소|둔화|역성장|마이너스|악화)")
PERIOD_CHANGE_CONTEXT_RE = re.compile(
    r"(전\s*분기\s*대비|전\s*년\s*동기\s*대비|전년\s*대비|\d{4}\s*년\s*\d+\s*분기\s*대비|QoQ|YoY)",
    re.IGNORECASE,
)
SIGNED_TREND_METRIC_PATH_RE = re.compile(r"(qoq_pct|yoy_pct|growth|mom_pct|mat_yoy_pct)")
STAGE_TEXT_PATH_RE = re.compile(
    r"^(phenomenon|cause|prediction|recommendation)\.(title|body|bullets\[\d+\])$"
)


@dataclass(frozen=True, slots=True)
class RepairResult:
    parsed_output: dict[str, Any]
    changes: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def repair_post_llm_output(
    parsed_output: dict[str, Any],
    bundle: dict[str, Any],
    config: ValidatorConfig,
) -> RepairResult:
    """Repair only deterministic format defects before validator policy checks."""

    repaired = copy.deepcopy(parsed_output)
    changes: list[dict[str, Any]] = []
    bundle_index = build_bundle_path_index(bundle)

    def repair_value(value: Any, path: str) -> Any:
        if isinstance(value, str):
            repaired_text = _repair_zero_decimal_units(value, path, changes)
            repaired_text = _repair_decimal_metric_units(repaired_text, path, bundle_index, config, changes)
            repaired_text = _repair_korean_large_unit_numbers(repaired_text, path, bundle_index, config, changes)
            repaired_text = _repair_decimal_metric_units(repaired_text, path, bundle_index, config, changes)
            repaired_text = _repair_forecast_compact_tags(repaired_text, path, bundle, bundle_index, config, changes)
            repaired_text = _repair_signed_percent_polarity(repaired_text, path, bundle_index, config, changes)
            if STAGE_TEXT_PATH_RE.match(path):
                return _repair_view_labels(repaired_text, path, bundle, bundle_index, config, changes)
            return repaired_text
        if isinstance(value, list):
            return [repair_value(item, f"{path}[{idx}]") for idx, item in enumerate(value)]
        if isinstance(value, dict):
            return {key: repair_value(item, f"{path}.{key}" if path else str(key)) for key, item in value.items()}
        return value

    repaired = repair_value(repaired, "")
    _repair_prediction_numeric_evidence(repaired, bundle, bundle_index, config, changes)
    return RepairResult(parsed_output=repaired, changes=changes)


def _repair_zero_decimal_units(text: str, path: str, changes: list[dict[str, Any]]) -> str:
    def replace(match: re.Match[str]) -> str:
        before = match.group(0)
        after = f"{match.group(1)}{match.group(2)}"
        changes.append({"type": "zero_decimal_unit", "path": path, "before": before, "after": after})
        return after

    return ZERO_DECIMAL_KRW_QTY_RE.sub(replace, text)


def _repair_decimal_metric_units(
    text: str,
    path: str,
    bundle_index: dict[float, list[str]],
    config: ValidatorConfig,
    changes: list[dict[str, Any]],
) -> str:
    def replace(match: re.Match[str]) -> str:
        before = match.group(0)
        after = match.group("number")
        next_text = text[match.end() : match.end() + 40]
        has_compact_tag = bool(re.match(r"\s*\((?:ML|CD)·(?:IQVIA|UBIST)·", next_text))
        numeric = float(after.replace(",", ""))
        if not has_compact_tag and find_match_unit_aware(numeric, bundle_index, "currency_krw", config) is None:
            return before
        changes.append({"type": "decimal_metric_unit_removed", "path": path, "before": before, "after": after})
        return after

    return DECIMAL_KRW_QTY_UNIT_RE.sub(replace, text)


def _repair_korean_large_unit_numbers(
    text: str,
    path: str,
    bundle_index: dict[float, list[str]],
    config: ValidatorConfig,
    changes: list[dict[str, Any]],
) -> str:
    def replace(match: re.Match[str]) -> str:
        before = match.group(0)
        if _inside_single_quoted_text(text, match.start()):
            return before
        numeric = _parse_korean_large_number(match.group("expr"))
        if numeric is None:
            return before
        if not match.group("unit") and find_match_unit_aware(float(numeric), bundle_index, "currency_krw", config) is None:
            return before
        after_number = _format_decimal_number(numeric)
        after = f"{after_number}{match.group('unit') or ''}"
        if after == before:
            return before
        changes.append({"type": "korean_large_unit_number", "path": path, "before": before, "after": after})
        return after

    return KOREAN_LARGE_UNIT_NUMBER_RE.sub(replace, text)


def _inside_single_quoted_text(text: str, position: int) -> bool:
    before = text[:position]
    after = text[position:]
    return before.count("'") % 2 == 1 and "'" in after


def _parse_korean_large_number(expr: str) -> Decimal | None:
    total = Decimal("0")
    matched = False
    for token_match in KOREAN_LARGE_UNIT_TOKEN_RE.finditer(expr):
        number = Decimal(token_match.group(1).replace(",", ""))
        unit = token_match.group(2)
        if unit == "억":
            total += number * Decimal("100000000")
            matched = True
        elif unit == "만":
            total += number * Decimal("10000")
            matched = True
        elif unit == "천":
            total += number * Decimal("1000")
            matched = True
        else:
            total += number
    if not matched:
        return None
    return total


def _format_decimal_number(value: Decimal) -> str:
    if value == value.to_integral_value():
        return f"{int(value):,}"
    fixed = format(value, "f").rstrip("0").rstrip(".")
    whole, _, fraction = fixed.partition(".")
    return f"{int(whole):,}.{fraction}" if fraction else f"{int(whole):,}"


def _repair_forecast_compact_tags(
    text: str,
    path: str,
    bundle: dict[str, Any],
    bundle_index: dict[float, list[str]],
    config: ValidatorConfig,
    changes: list[dict[str, Any]],
) -> str:
    if not ((bundle.get("forecast_simulation") or {}).get("available")):
        return text

    def replace(match: re.Match[str]) -> str:
        before = match.group(0)
        raw_number = match.group("number")
        unit = match.group("unit") or ""
        tag = match.group("tag") or ""
        try:
            value = float(raw_number.replace(",", ""))
        except ValueError:
            return before

        matched_path = find_match_unit_aware(value, bundle_index, _forecast_number_type(raw_number, unit + tag), config)
        if not str(matched_path or "").startswith("forecast_simulation.by_view."):
            return before

        compact_tag = _compact_forecast_tag(bundle, str(matched_path))
        if compact_tag is None:
            return before
        if tag and VALID_COMPACT_TAG_RE.fullmatch(tag.strip()) and tag.strip() == compact_tag:
            return before

        after = f"{raw_number}{unit}{compact_tag}"
        if after == before:
            return before
        changes.append(
            {
                "type": "forecast_compact_tag",
                "path": path,
                "before": before,
                "after": after,
                "matched_path": matched_path,
            }
        )
        return after

    return FORECAST_NUMBER_TAG_RE.sub(replace, text)


def _forecast_number_type(raw_number: str, tag: str) -> str:
    if re.search(r"(unit|counting|dosage|처방|개|정|캡슐)", tag or "", re.IGNORECASE):
        return "unit_pack"
    if "." in raw_number and float(raw_number.replace(",", "")) < 1000:
        return "unit_pack"
    return "currency_krw"


def _repair_signed_percent_polarity(
    text: str,
    path: str,
    bundle_index: dict[float, list[str]],
    config: ValidatorConfig,
    changes: list[dict[str, Any]],
) -> str:
    def replace(match: re.Match[str]) -> str:
        before = match.group(0)
        sentence = _sentence_around(text, match.start(), match.end())
        if not (NEGATIVE_TREND_CONTEXT_RE.search(sentence) or PERIOD_CHANGE_CONTEXT_RE.search(sentence)):
            return before
        try:
            value = float(match.group("number"))
        except ValueError:
            return before
        positive_path = find_match_unit_aware(value, bundle_index, "percent", config)
        negative_path = find_match_unit_aware(-value, bundle_index, "percent_signed", config)
        if positive_path is not None and SIGNED_TREND_METRIC_PATH_RE.search(str(positive_path)):
            return before
        if negative_path is None or not SIGNED_TREND_METRIC_PATH_RE.search(str(negative_path)):
            return before
        after = f"-{match.group('number')}%{match.group('tag')}"
        changes.append(
            {
                "type": "signed_percent_polarity",
                "path": path,
                "before": before,
                "after": after,
                "matched_path": negative_path,
            }
        )
        return after

    return SIGNLESS_PERCENT_WITH_COMPACT_TAG_RE.sub(replace, text)


def _sentence_around(text: str, start: int, end: int) -> str:
    left_candidates = [text.rfind(delim, 0, start) for delim in (".", "!", "?", "\n")]
    left = max(left_candidates)
    right_candidates = [idx for idx in (text.find(delim, end) for delim in (".", "!", "?", "\n")) if idx >= 0]
    right = min(right_candidates) if right_candidates else len(text)
    return text[left + 1 : right + 1]


def _repair_prediction_numeric_evidence(
    parsed_output: dict[str, Any],
    bundle: dict[str, Any],
    bundle_index: dict[float, list[str]],
    config: ValidatorConfig,
    changes: list[dict[str, Any]],
) -> None:
    prediction = parsed_output.get("prediction")
    if not isinstance(prediction, dict):
        return
    evidence = prediction.get("evidence")
    if evidence:
        return
    basis = _first_bundle_matched_forecast_basis(str(prediction.get("body") or ""), bundle_index, config)
    if basis is None:
        return
    prediction["evidence"] = [
        {
            "title": "forecast_simulation 수치 근거",
            "basis": basis,
            "stage": "prediction",
        }
    ]
    changes.append({"type": "prediction_numeric_evidence", "path": "prediction.evidence", "basis": basis})


def _first_bundle_matched_forecast_basis(
    text: str,
    bundle_index: dict[float, list[str]],
    config: ValidatorConfig,
) -> str | None:
    for match in FORECAST_BASIS_RE.finditer(text):
        basis = match.group(0)
        if not COMPACT_VIEW_TAG_RE.search(basis):
            continue
        try:
            value = float(match.group("number").replace(",", ""))
        except ValueError:
            continue
        matched_path = find_match_unit_aware(value, bundle_index, _forecast_number_type(match.group("number"), match.group("tag")), config)
        if str(matched_path or "").startswith("forecast_simulation.by_view."):
            return basis
    return None


def _compact_forecast_tag(bundle: dict[str, Any], matched_path: str) -> str | None:
    match = FORECAST_VIEW_PATH_RE.search(matched_path)
    if not match:
        return None
    view_short, source, measure = match.groups()
    horizon_match = re.search(r"\.horizon_(1y|3y|5y|10y)\.", matched_path)
    if not horizon_match:
        return None
    period = _forecast_period(bundle, view_short, source, measure, horizon_match.group(1))
    if not period:
        return None
    return f"({view_short}·{source}·{measure}·{period})"


def _forecast_period(bundle: dict[str, Any], view_short: str, source: str, measure: str, horizon: str) -> str | None:
    forecast = bundle.get("forecast_simulation") or {}
    by_view = forecast.get("by_view") or {}
    candidates = [
        f"{view_short}.{source}.{measure}",
        f"{view_short}.{source}.unit" if measure == "counting_unit" else "",
        f"{view_short}.{source}.counting_unit" if measure == "unit" else "",
    ]
    for key in candidates:
        if not key:
            continue
        horizon_payload = (by_view.get(key) or {}).get(f"horizon_{horizon}") or {}
        period = horizon_payload.get("period")
        if period:
            return str(period)
    return None


def _repair_view_labels(
    text: str,
    path: str,
    bundle: dict[str, Any],
    bundle_index: dict[float, list[str]],
    config: ValidatorConfig,
    changes: list[dict[str, Any]],
) -> str:
    labels: list[str] = []
    for item in extract_numbers(text, config):
        matched_path = find_match_unit_aware(item["value"], bundle_index, item["number_type"], config)
        if matched_path is None:
            continue
        item_with_path = {**item, "matched_path": matched_path}
        if not _requires_view_label(item_with_path):
            continue
        parts = _view_label_parts_from_path(bundle, matched_path)
        if parts is None:
            continue
        display, source = parts
        if _has_view_label(text, display, source):
            continue
        label = f"{display} · {source} 기준"
        if label not in labels:
            labels.append(label)

    if not labels:
        return text

    suffix = " (" + "; ".join(labels) + ")"
    changes.append({"type": "view_source_label", "path": path, "labels": labels})
    return f"{text}{suffix}"
