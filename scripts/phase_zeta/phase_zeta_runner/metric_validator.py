from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from .config import ValidatorConfig


NUMBER_PATTERNS = [
    {
        "name": "comma_raw_value",
        "pattern": re.compile(r"(?<![.\d])(\d{1,3}(?:,\d{3})+(?:\.\d+)?)(?!\d)"),
        "type": "float",
        "low_priority": False,
    },
    {
        "name": "percent",
        "pattern": re.compile(r"([+-]?\d+(?:\.\d+)?)\s*%"),
        "type": "float",
        "low_priority": False,
    },
    {
        "name": "rank",
        "pattern": re.compile(r"(?:rank|순위)\s*[:=]?\s*(\d+)\s*(?:위)?|(?<!\d)(\d+)\s*위"),
        "type": "int",
        "low_priority": False,
    },
    {
        "name": "kpi_value",
        "pattern": re.compile(r"(?:EI|Momentum|CAGR|HHI)\s*[:=]?\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE),
        "type": "float",
        "low_priority": False,
    },
    {
        "name": "plain_int",
        "pattern": re.compile(r"(?<![\d,.])(\d{1,6})(?![\d,.])"),
        "type": "int",
        "low_priority": True,
    },
]


DEFAULT_TOLERANCE_BY_TYPE = {
    "currency_krw": 0.01,
    "volume_rx": 0.5,
    "unit_pack": 0.5,
    "percent": 0.05,
    "percent_signed": 0.05,
    "kpi": 0.05,
    "rank": 0.0,
}


@dataclass
class StageValidation:
    valid: bool
    extracted: list[dict[str, Any]]
    unmatched: list[dict[str, Any]]
    warnings: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationResult:
    valid: bool
    stage_results: dict[str, StageValidation]
    total_numbers_extracted: int
    total_numbers_matched: int
    unmatched_numbers: list[dict[str, Any]]
    warnings: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "stage_results": {stage: result.to_dict() for stage, result in self.stage_results.items()},
            "total_numbers_extracted": self.total_numbers_extracted,
            "total_numbers_matched": self.total_numbers_matched,
            "unmatched_numbers": self.unmatched_numbers,
            "warnings": self.warnings,
        }


def _coerce_number(value: str, value_type: str) -> float | int:
    cleaned = value.replace(",", "")
    if value_type == "int":
        return int(float(cleaned))
    return float(cleaned)


def _match_group(match: re.Match[str]) -> str:
    for group in match.groups():
        if group is not None:
            return group
    return match.group(0)


def _default_config() -> ValidatorConfig:
    return ValidatorConfig(tolerance_default=0.01, tolerance_percent=0.05, tolerance_kpi=0.05)


def _nearby_context(raw_text: str, full_context: str, before: int = 30, after: int = 40) -> str:
    pos = (full_context or "").find(raw_text)
    if pos < 0:
        return full_context or ""
    return full_context[max(0, pos - before) : pos + len(raw_text) + after]


def _is_qualified_threshold(raw_text: str, full_context: str) -> bool:
    """Return true for approximate threshold phrases like "20% 이상"."""

    nearby = _nearby_context(raw_text, full_context, before=8, after=12)
    after = nearby.split(raw_text, 1)[1] if raw_text in nearby else ""
    return bool(re.match(r"^\s*(이상|이하|초과|미만|내외|대)", after))


def classify_number_context(raw_text: str, full_context: str, value: float | int) -> str:
    """Classify a rendered number so matching can use the right tolerance."""

    text = full_context or ""
    nearby = _nearby_context(raw_text, text)
    after = nearby.split(raw_text, 1)[1] if raw_text in nearby else nearby
    before = nearby.split(raw_text, 1)[0] if raw_text in nearby else nearby

    if "%" in raw_text:
        return "percent_signed" if raw_text.strip().startswith(("+", "-")) else "percent"

    if "위" in raw_text or re.search(r"(rank|순위)\s*[:=]?\s*$", before, flags=re.IGNORECASE):
        return "rank"
    if re.match(r"^\s*위", after):
        return "rank"

    if re.search(r"(EI|Momentum|CAGR|HHI)\s*[:=]?\s*$", before, flags=re.IGNORECASE):
        return "kpi"

    if re.match(r"^\s*(Rx|처방량|처방건수|건)", after):
        return "volume_rx"

    if re.match(r"^\s*(정|캡슐|바이알|포|병|개)", after):
        return "unit_pack"

    if re.match(r"^\s*(KRW|원|₩)", after):
        return "currency_krw"

    if any(ind in nearby for ind in ("Rx", "처방량", "처방 ", "처방건수")) and not any(
        ind in after[:20] for ind in ("KRW", "₩")
    ):
        return "volume_rx"

    if any(ind in nearby for ind in ("KRW", "₩")):
        return "currency_krw"

    if re.search(r"(EI|Momentum|CAGR|HHI)", nearby, flags=re.IGNORECASE):
        return "kpi"

    if "," in raw_text and float(value) >= 1000:
        return "currency_krw"

    return "currency_krw"


def _tolerance_for_type(number_type: str, config: ValidatorConfig | None = None) -> float:
    if config is None:
        config = _default_config()
    table = getattr(config, "tolerance_by_type", None) or DEFAULT_TOLERANCE_BY_TYPE
    if number_type in table:
        return float(table[number_type])
    return float(getattr(config, "tolerance_default", DEFAULT_TOLERANCE_BY_TYPE["currency_krw"]))


def extract_numbers(text: str, config: ValidatorConfig | None = None) -> list[dict[str, Any]]:
    if config is None:
        config = _default_config()
    extracted: list[dict[str, Any]] = []
    for spec in NUMBER_PATTERNS:
        for match in spec["pattern"].finditer(text or ""):
            value_text = _match_group(match)
            try:
                value = _coerce_number(value_text, spec["type"])
            except ValueError:
                continue
            raw_text = match.group(0)
            number_type = classify_number_context(raw_text, text or "", value)
            extracted.append(
                {
                    "value": value,
                    "raw_text": raw_text,
                    "pattern": spec["name"],
                    "number_type": number_type,
                    "tolerance": _tolerance_for_type(number_type, config),
                    "low_priority": bool(spec.get("low_priority", False))
                    or _is_qualified_threshold(raw_text, text or ""),
                }
            )
    return extracted


def build_bundle_path_index(bundle: dict) -> dict[float, list[str]]:
    index: dict[float, list[str]] = {}

    def traverse(obj: Any, path: str = "") -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                next_path = f"{path}.{key}" if path else str(key)
                traverse(value, next_path)
        elif isinstance(obj, list):
            for idx, value in enumerate(obj):
                traverse(value, f"{path}[{idx}]")
        elif isinstance(obj, bool):
            return
        elif isinstance(obj, (int, float)):
            numeric = float(obj)
            index.setdefault(numeric, []).append(path)
        elif isinstance(obj, str):
            for item in extract_numbers(obj):
                index.setdefault(float(item["value"]), []).append(f"{path}::{item['raw_text']}")

    traverse(bundle)
    return index


def find_match(value: float | int, bundle_index: dict[float, list[str]], tolerance: float) -> str | None:
    numeric = float(value)
    best_path: str | None = None
    best_delta: float | None = None
    for bundle_value, paths in bundle_index.items():
        delta = abs(bundle_value - numeric)
        if delta <= tolerance and (best_delta is None or delta < best_delta):
            best_delta = delta
            best_path = paths[0]
    return best_path


def find_match_unit_aware(
    value: float | int,
    bundle_index: dict[float, list[str]],
    number_type: str,
    config: ValidatorConfig | None = None,
) -> str | None:
    if config is None:
        config = _default_config()

    numeric = float(value)
    tolerance = _tolerance_for_type(number_type, config)
    relative_tolerance = float(getattr(config, "relative_tolerance", 0.001))
    best_path: str | None = None
    best_delta: float | None = None

    for bundle_value, paths in bundle_index.items():
        delta = abs(bundle_value - numeric)
        matches_absolute = delta <= tolerance
        matches_relative = abs(bundle_value) > 1000 and delta / abs(bundle_value) <= relative_tolerance
        if (matches_absolute or matches_relative) and (best_delta is None or delta < best_delta):
            best_delta = delta
            best_path = paths[0]
    return best_path


def _stage_contexts(stage_dict: dict[str, Any]) -> dict[str, str]:
    contexts = {
        "title": str(stage_dict.get("title", "")),
        "body": str(stage_dict.get("body", "")),
    }
    for idx, bullet in enumerate(stage_dict.get("bullets", []) or []):
        contexts[f"bullets[{idx}]"] = str(bullet)
    return contexts


def validate_stage_output(
    stage: str,
    stage_dict: dict[str, Any],
    bundle_index: dict[float, list[str]],
    config: ValidatorConfig,
) -> StageValidation:
    extracted: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    for context_name, text in _stage_contexts(stage_dict).items():
        for item in extract_numbers(text, config):
            matched_path = find_match_unit_aware(item["value"], bundle_index, item["number_type"], config)
            record = {
                "stage": stage,
                "context": context_name,
                "value": item["value"],
                "raw_text": item["raw_text"],
                "pattern": item["pattern"],
                "number_type": item["number_type"],
                "tolerance": item["tolerance"],
                "matched_path": matched_path,
                "low_priority": item["low_priority"],
            }
            extracted.append(record)
            if matched_path is None:
                if item["low_priority"]:
                    warnings.append(record)
                else:
                    unmatched.append(record)

    return StageValidation(valid=not unmatched, extracted=extracted, unmatched=unmatched, warnings=warnings)


def validate_output(parsed_output: dict, bundle: dict, config: ValidatorConfig) -> ValidationResult:
    bundle_index = build_bundle_path_index(bundle)
    stage_results: dict[str, StageValidation] = {}
    all_unmatched: list[dict[str, Any]] = []
    all_warnings: list[dict[str, Any]] = []
    total_extracted = 0
    total_matched = 0

    for stage in ["phenomenon", "cause", "prediction", "recommendation"]:
        stage_result = validate_stage_output(stage, parsed_output.get(stage, {}) or {}, bundle_index, config)
        stage_results[stage] = stage_result
        all_unmatched.extend(stage_result.unmatched)
        all_warnings.extend(stage_result.warnings)
        total_extracted += len(stage_result.extracted)
        total_matched += sum(1 for item in stage_result.extracted if item.get("matched_path"))

    return ValidationResult(
        valid=not all_unmatched,
        stage_results=stage_results,
        total_numbers_extracted=total_extracted,
        total_numbers_matched=total_matched,
        unmatched_numbers=all_unmatched,
        warnings=all_warnings,
    )
