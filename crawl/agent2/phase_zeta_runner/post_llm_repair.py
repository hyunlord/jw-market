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
COMPACT_TAG_PARTS_RE = re.compile(
    r"\((?P<view>ML|CD)·(?P<source>IQVIA|UBIST)·(?P<measure>[^)·]+)·(?P<period>\d{4}-Q\d|\d{4}-\d{2})\)"
)
SIGNLESS_PERCENT_WITH_COMPACT_TAG_RE = re.compile(
    r"(?<![+-])(?P<number>\d+(?:\.\d+)?)%(?P<tag>\((?:ML|CD)·(?:IQVIA|UBIST)·[^)]{1,80}\))"
)
BARE_SIGNLESS_PERCENT_RE = re.compile(r"(?<![+-])(?P<number>\d+(?:\.\d+)?)%(?!\()")
INVALID_COMPACT_LIKE_TAG_RE = re.compile(
    r"(?P<number>\d+(?:\.\d+)?)%(?P<tag>\((?!(?:ML|CD)·(?:IQVIA|UBIST)·)[^)]*·[^)]*\))"
)
OUT_OF_RANGE_RANK_RE = re.compile(r"(?:순위\s*)?100위권\s*밖")
FORECAST_BASIS_RE = re.compile(
    r"(?<![\d,.])(?P<number>[+-]?\d{1,3}(?:,\d{3})*(?:\.\d+)?|[+-]?\d+(?:\.\d+)?)"
    r"(?P<tag>\((?:ML|CD)·(?:IQVIA|UBIST)·[^)]{1,80}\))"
)
NEGATIVE_TREND_CONTEXT_RE = re.compile(r"(감소|하락|축소|둔화|역성장|마이너스|악화|줄어들|줄었|줄어)")
EVIDENCE_BASIS_CONTEXT_RE = re.compile(r"(근거|basis)", re.IGNORECASE)
PERIOD_CHANGE_CONTEXT_RE = re.compile(
    r"(전\s*월\s*대비|전\s*분기\s*대비|전\s*년\s*동기\s*대비|전년\s*대비|\d{4}\s*년\s*\d+\s*분기\s*대비|MoM|QoQ|YoY)",
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
            repaired_text = _repair_invalid_compact_like_tags(repaired_text, path, changes)
            repaired_text = _repair_out_of_range_rank(repaired_text, path, changes)
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

        number_type = _forecast_number_type(raw_number, unit + tag)
        matched_path = _find_rendered_forecast_match(
            value,
            bundle_index,
            number_type,
            config,
            tag,
            prefer_variant_horizon=path.startswith("prediction."),
        )
        if matched_path is None:
            matched_path = find_match_unit_aware(value, bundle_index, number_type, config)
        if not str(matched_path or "").startswith("forecast_simulation.by_view."):
            return before

        compact_tag = _compact_forecast_tag(bundle, str(matched_path))
        if compact_tag is None:
            return before
        rendered_number = _format_forecast_number(raw_number)
        if tag and VALID_COMPACT_TAG_RE.fullmatch(tag.strip()) and tag.strip() == compact_tag and rendered_number == raw_number:
            return before

        after = f"{rendered_number}{unit}{compact_tag}"
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


def _format_forecast_number(raw_number: str) -> str:
    if "." not in raw_number or len(raw_number.rsplit(".", 1)[1]) <= 2:
        return raw_number
    value = Decimal(raw_number.replace(",", ""))
    quantized = value.quantize(Decimal("0.01"))
    return _format_decimal_number(quantized)


def _forecast_number_type(raw_number: str, tag: str) -> str:
    if re.search(r"(unit|counting|dosage|처방|수량|개|정|캡슐)", tag or "", re.IGNORECASE):
        return "unit_pack"
    if "." in raw_number and float(raw_number.replace(",", "")) < 1000:
        return "unit_pack"
    return "currency_krw"


def _find_rendered_forecast_match(
    value: float,
    bundle_index: dict[float, list[str]],
    number_type: str,
    config: ValidatorConfig,
    tag: str,
    *,
    prefer_variant_horizon: bool,
) -> str | None:
    """Resolve rendered forecast numbers without letting unrelated zero-valued paths win."""

    if not tag and not prefer_variant_horizon:
        return None

    candidates = _forecast_candidate_paths(value, bundle_index, number_type, config)
    if not candidates:
        return None

    tag_parts = _compact_tag_parts(tag)
    required_horizons = _variant_horizon_priority(config.analysis_variant) if prefer_variant_horizon else ()

    def score(path: str) -> tuple[int, int, int, int, str]:
        horizon_score = _horizon_score(path, required_horizons)
        source_score = _source_score(path, tag_parts)
        measure_score = _measure_score(path, tag_parts, number_type)
        base_score = 0 if path.endswith(".base") else 1
        return (horizon_score, source_score, measure_score, base_score, path)

    return min(candidates, key=score)


def _forecast_candidate_paths(
    value: float,
    bundle_index: dict[float, list[str]],
    number_type: str,
    config: ValidatorConfig,
) -> list[str]:
    tolerance = config.tolerance_by_type.get(number_type, config.tolerance_default)
    if number_type == "unit_pack" and value.is_integer():
        tolerance = max(tolerance, 0.52)
    relative_tolerance = float(getattr(config, "relative_tolerance", 0.001))
    candidates: list[str] = []
    for bundle_value, paths in bundle_index.items():
        delta = abs(bundle_value - value)
        matches_absolute = delta <= tolerance
        matches_relative = abs(bundle_value) > 1000 and delta / abs(bundle_value) <= relative_tolerance
        if not (matches_absolute or matches_relative):
            continue
        for candidate in paths:
            if (
                candidate.startswith("forecast_simulation.by_view.")
                and ".model." not in candidate
                and ".horizon_" in candidate
            ):
                candidates.append(candidate)
    return candidates


def _compact_tag_parts(tag: str) -> dict[str, str] | None:
    match = COMPACT_TAG_PARTS_RE.search(tag or "")
    if match is None:
        return None
    return match.groupdict()


def _variant_horizon_priority(analysis_variant: str) -> tuple[str, ...]:
    if analysis_variant == "short":
        return ("1y",)
    if analysis_variant == "long":
        return ("5y",)
    return ()


def _horizon_score(path: str, required_horizons: tuple[str, ...]) -> int:
    if not required_horizons:
        return 0
    for index, horizon in enumerate(required_horizons):
        if f".horizon_{horizon}." in path:
            return index
    return len(required_horizons)


def _source_score(path: str, tag_parts: dict[str, str] | None) -> int:
    if tag_parts is None:
        return 0
    return 0 if f".{tag_parts['source']}." in path else 1


def _measure_score(path: str, tag_parts: dict[str, str] | None, number_type: str) -> int:
    path_measure = _measure_from_forecast_path(path)
    if path_measure is None:
        return 2
    if tag_parts is not None:
        tag_measure = tag_parts["measure"]
        if path_measure == tag_measure:
            return 0
        if tag_measure in {"수량", "처방량"} and path_measure in {"unit", "dosage_unit", "counting_unit", "volume"}:
            return 0
        if tag_measure in {"매출", "sales"} and path_measure == "sales":
            return 0
    if number_type == "unit_pack" and path_measure in {"unit", "dosage_unit", "counting_unit", "volume"}:
        return 1
    if number_type == "currency_krw" and path_measure == "sales":
        return 1
    return 2


def _measure_from_forecast_path(path: str) -> str | None:
    match = FORECAST_VIEW_PATH_RE.search(path)
    if match is None:
        return None
    return match.group(3)


def _repair_signed_percent_polarity(
    text: str,
    path: str,
    bundle_index: dict[float, list[str]],
    config: ValidatorConfig,
    changes: list[dict[str, Any]],
) -> str:
    def replacement_for(match: re.Match[str], *, keep_tag: bool) -> tuple[str, str] | None:
        sentence = _sentence_around(text, match.start(), match.end())
        if not (
            NEGATIVE_TREND_CONTEXT_RE.search(sentence)
            or PERIOD_CHANGE_CONTEXT_RE.search(sentence)
            or EVIDENCE_BASIS_CONTEXT_RE.search(sentence)
        ):
            return None
        try:
            value = float(match.group("number"))
        except ValueError:
            return None
        positive_path = find_match_unit_aware(value, bundle_index, "percent", config)
        negative_path = find_match_unit_aware(-value, bundle_index, "percent_signed", config)
        if positive_path is not None and SIGNED_TREND_METRIC_PATH_RE.search(str(positive_path)):
            return None
        if negative_path is None or not SIGNED_TREND_METRIC_PATH_RE.search(str(negative_path)):
            return None
        tag = match.groupdict().get("tag") if keep_tag else ""
        return f"-{match.group('number')}%{tag or ''}", str(negative_path)

    def replace_tagged(match: re.Match[str]) -> str:
        before = match.group(0)
        replacement = replacement_for(match, keep_tag=True)
        if replacement is None:
            return before
        after, negative_path = replacement
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

    def replace_bare(match: re.Match[str]) -> str:
        before = match.group(0)
        replacement = replacement_for(match, keep_tag=False)
        if replacement is None:
            return before
        after, negative_path = replacement
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

    tagged_repaired = SIGNLESS_PERCENT_WITH_COMPACT_TAG_RE.sub(replace_tagged, text)
    return BARE_SIGNLESS_PERCENT_RE.sub(replace_bare, tagged_repaired)


def _repair_invalid_compact_like_tags(text: str, path: str, changes: list[dict[str, Any]]) -> str:
    def replace(match: re.Match[str]) -> str:
        before = match.group(0)
        after = f"{match.group('number')}%"
        changes.append({"type": "invalid_compact_tag_removed", "path": path, "before": before, "after": after})
        return after

    return INVALID_COMPACT_LIKE_TAG_RE.sub(replace, text)


def _sentence_around(text: str, start: int, end: int) -> str:
    left = max(_sentence_delimiters(text, 0, start), default=-1)
    right = min(_sentence_delimiters(text, end, len(text)), default=len(text))
    return text[left + 1 : right + 1]


def _sentence_delimiters(text: str, start: int, end: int) -> list[int]:
    delimiters: list[int] = []
    for idx in range(start, end):
        char = text[idx]
        if char in "!?。！？\n":
            delimiters.append(idx)
        elif char == "." and not _is_decimal_point(text, idx):
            delimiters.append(idx)
    return delimiters


def _is_decimal_point(text: str, idx: int) -> bool:
    return idx > 0 and idx + 1 < len(text) and text[idx - 1].isdigit() and text[idx + 1].isdigit()


def _repair_out_of_range_rank(text: str, path: str, changes: list[dict[str, Any]]) -> str:
    def replace(match: re.Match[str]) -> str:
        before = match.group(0)
        after = "순위권 밖"
        changes.append({"type": "out_of_range_rank", "path": path, "before": before, "after": after})
        return after

    return OUT_OF_RANGE_RANK_RE.sub(replace, text)


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
