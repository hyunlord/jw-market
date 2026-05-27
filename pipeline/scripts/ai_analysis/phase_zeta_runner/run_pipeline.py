from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .bundle_invariant_validator import validate_bundle_invariants
from .bundle_mart_validator import validate_bundle_against_mart
from .config import RunnerConfig
from .metric_validator import StageValidation, validate_output
from .narrative_event_validator import validate_narrative_events


@dataclass
class FullValidationResult:
    valid: bool
    stage_results: dict[str, StageValidation]
    total_numbers_extracted: int
    total_numbers_matched: int
    unmatched_numbers: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    layers: dict[str, Any]
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "stage_results": {stage: result.to_dict() for stage, result in self.stage_results.items()},
            "total_numbers_extracted": self.total_numbers_extracted,
            "total_numbers_matched": self.total_numbers_matched,
            "unmatched_numbers": self.unmatched_numbers,
            "warnings": self.warnings,
            "layers": self.layers,
            "summary": self.summary,
        }


def _disabled_result(name: str) -> dict[str, Any]:
    return {"valid": True, "disabled": True, "name": name}


def run_full_validation(parsed_output: dict[str, Any], bundle: dict[str, Any], db_conn: Any, config: RunnerConfig) -> FullValidationResult:
    layer1 = validate_output(parsed_output, bundle, config.validator)
    layer2 = (
        validate_bundle_against_mart(bundle, db_conn, config.validator)
        if config.validator.bundle_mart_check_enabled
        else _disabled_result("bundle_mart")
    )
    layer3 = (
        validate_bundle_invariants(bundle, config.validator)
        if config.validator.bundle_invariant_check_enabled
        else _disabled_result("bundle_invariant")
    )
    layer4 = (
        validate_narrative_events(parsed_output, bundle, config.validator)
        if config.validator.narrative_event_check_enabled
        else _disabled_result("narrative_events")
    )

    layer2_valid = bool(layer2.get("valid"))
    layer3_valid = bool(layer3.get("valid"))
    layer4_valid = bool(layer4.get("valid"))
    layer3_blocks = config.validator.bundle_invariant_fail_action == "fail"
    layer4_blocks = not config.validator.narrative_event_warning_only
    all_valid = (
        layer1.valid
        and layer2_valid
        and (layer3_valid or not layer3_blocks)
        and (layer4_valid or not layer4_blocks)
    )
    warnings = list(layer1.warnings)
    if not layer4_valid and config.validator.narrative_event_warning_only:
        warnings.extend({"stage": "layer4_narrative_events", **item} for item in layer4.get("unmatched_dates", []))

    layers = {
        "layer1_metric_validator": layer1.to_dict(),
        "layer2_bundle_mart": layer2,
        "layer3_bundle_invariant": layer3,
        "layer4_narrative_events": layer4,
    }
    summary = {
        "layer1_valid": layer1.valid,
        "layer2_valid": layer2_valid,
        "layer3_valid": layer3_valid,
        "layer4_valid": layer4_valid,
        "layer3_blocks": layer3_blocks,
        "layer4_blocks": layer4_blocks,
        "all_valid": all_valid,
        "verdict": "PASS" if all_valid else "FAIL",
    }

    return FullValidationResult(
        valid=all_valid,
        stage_results=layer1.stage_results,
        total_numbers_extracted=layer1.total_numbers_extracted,
        total_numbers_matched=layer1.total_numbers_matched,
        unmatched_numbers=layer1.unmatched_numbers,
        warnings=warnings,
        layers=layers,
        summary=summary,
    )
