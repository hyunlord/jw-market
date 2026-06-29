from __future__ import annotations

import copy
import re
from dataclasses import asdict, dataclass
from typing import Any

from .config import ValidatorConfig
from .metric_validator import (
    _has_view_label,
    _requires_view_label,
    _view_label_parts_from_path,
    build_bundle_path_index,
    extract_numbers,
    find_match_unit_aware,
)


ZERO_DECIMAL_KRW_QTY_RE = re.compile(r"(?<!\d)(\d[\d,]*)\.0+\s*(원|개)")
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
            if STAGE_TEXT_PATH_RE.match(path):
                return _repair_view_labels(repaired_text, path, bundle, bundle_index, config, changes)
            return repaired_text
        if isinstance(value, list):
            return [repair_value(item, f"{path}[{idx}]") for idx, item in enumerate(value)]
        if isinstance(value, dict):
            return {key: repair_value(item, f"{path}.{key}" if path else str(key)) for key, item in value.items()}
        return value

    repaired = repair_value(repaired, "")
    return RepairResult(parsed_output=repaired, changes=changes)


def _repair_zero_decimal_units(text: str, path: str, changes: list[dict[str, Any]]) -> str:
    def replace(match: re.Match[str]) -> str:
        before = match.group(0)
        after = f"{match.group(1)}{match.group(2)}"
        changes.append({"type": "zero_decimal_unit", "path": path, "before": before, "after": after})
        return after

    return ZERO_DECIMAL_KRW_QTY_RE.sub(replace, text)


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
