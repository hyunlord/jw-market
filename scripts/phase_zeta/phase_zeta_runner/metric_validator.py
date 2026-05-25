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
        "tolerance_key": "tolerance_default",
        "low_priority": False,
    },
    {
        "name": "percent",
        "pattern": re.compile(r"([+-]?\d+(?:\.\d+)?)\s*%"),
        "type": "float",
        "tolerance_key": "tolerance_percent",
        "low_priority": False,
    },
    {
        "name": "rank",
        "pattern": re.compile(r"(?:rank|순위)\s*[:=]?\s*(\d+)\s*(?:위)?|(?<!\d)(\d+)\s*위"),
        "type": "int",
        "tolerance_key": "zero",
        "low_priority": False,
    },
    {
        "name": "kpi_value",
        "pattern": re.compile(r"(?:EI|Momentum|CAGR|HHI)\s*[:=]?\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE),
        "type": "float",
        "tolerance_key": "tolerance_kpi",
        "low_priority": False,
    },
    {
        "name": "plain_int",
        "pattern": re.compile(r"(?<![\d,.])(\d{1,6})(?![\d,.])"),
        "type": "int",
        "tolerance_key": "zero",
        "low_priority": True,
    },
]


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


def _tolerance(spec: dict[str, Any], config: ValidatorConfig | None = None) -> float:
    key = spec["tolerance_key"]
    if key == "zero":
        return 0.0
    if config is None:
        config = ValidatorConfig(tolerance_default=0.01, tolerance_percent=0.05, tolerance_kpi=0.05)
    return float(getattr(config, key))


def extract_numbers(text: str, config: ValidatorConfig | None = None) -> list[dict[str, Any]]:
    extracted: list[dict[str, Any]] = []
    for spec in NUMBER_PATTERNS:
        for match in spec["pattern"].finditer(text or ""):
            value_text = _match_group(match)
            try:
                value = _coerce_number(value_text, spec["type"])
            except ValueError:
                continue
            extracted.append(
                {
                    "value": value,
                    "raw_text": match.group(0),
                    "pattern": spec["name"],
                    "tolerance": _tolerance(spec, config),
                    "low_priority": bool(spec.get("low_priority", False)),
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
            matched_path = find_match(item["value"], bundle_index, item["tolerance"])
            record = {
                "stage": stage,
                "context": context_name,
                "value": item["value"],
                "raw_text": item["raw_text"],
                "pattern": item["pattern"],
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
