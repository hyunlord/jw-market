from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final

import yaml


ANALYSIS_VARIANTS: Final = frozenset({"legacy", "short", "long"})


class InvalidAnalysisVariantError(ValueError):
    """Raised when a runner variant is outside the supported policy set."""


def require_analysis_variant(value: str) -> str:
    variant = value.strip().lower()
    if variant not in ANALYSIS_VARIANTS:
        raise InvalidAnalysisVariantError(f"Unsupported analysis_variant: {value}")
    return variant


@dataclass(frozen=True)
class GenOSConfig:
    workflow_id: int
    admin_api_url: str
    workflow_api_url: str
    endpoint_path: str
    request_payload_mode: str
    response_output_path: str
    timeout_sec: int


@dataclass(frozen=True)
class ValidatorConfig:
    tolerance_default: float
    tolerance_percent: float
    tolerance_kpi: float
    tolerance_by_type: dict[str, float] = field(
        default_factory=lambda: {
            "currency_krw": 0.01,
            "volume_rx": 0.5,
            "unit_pack": 0.5,
            "percent": 0.05,
            "percent_signed": 0.05,
            "kpi": 0.05,
            "rank": 0.0,
        }
    )
    relative_tolerance: float = 0.001
    cascade_skip_on_fail: bool = False
    bundle_mart_check_enabled: bool = True
    bundle_invariant_check_enabled: bool = True
    narrative_event_check_enabled: bool = True
    narrative_event_warning_only: bool = True
    bundle_invariant_fail_action: str = "fail"


@dataclass(frozen=True)
class ComposerConfig:
    update_cache_deep_analysis: bool
    db_host: str
    db_port: int
    db_user: str
    db_name: str


@dataclass(frozen=True)
class RetryConfig:
    max_attempts: int
    backoff_sec: float


@dataclass(frozen=True)
class RunnerConfig:
    config_version: str
    builder_version: str
    genos: GenOSConfig
    validator: ValidatorConfig
    composer: ComposerConfig
    retry: RetryConfig
    analysis_variant: str = "legacy"

    @classmethod
    def from_yaml(cls, path: str | Path) -> "RunnerConfig":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        root = raw.get("phase_zeta_runner", raw)
        genos_raw = root["genos"]
        validator_raw = root["validator"]
        composer_raw = root["composer"]
        retry_raw = root["retry"]
        analysis_variant = require_analysis_variant(str(root.get("analysis_variant", "legacy")))
        return cls(
            config_version=str(root["config_version"]),
            builder_version=str(root["builder_version"]),
            genos=GenOSConfig(
                workflow_id=int(genos_raw["workflow_id"]),
                admin_api_url=str(genos_raw["admin_api_url"]).rstrip("/"),
                workflow_api_url=str(genos_raw["workflow_api_url"]).rstrip("/"),
                endpoint_path=str(genos_raw["endpoint_path"]),
                request_payload_mode=str(genos_raw.get("request_payload_mode", "root_question")),
                response_output_path=str(genos_raw.get("response_output_path", "auto")),
                timeout_sec=int(genos_raw.get("timeout_sec", 120)),
            ),
            validator=ValidatorConfig(
                tolerance_default=float(validator_raw["tolerance_default"]),
                tolerance_percent=float(validator_raw["tolerance_percent"]),
                tolerance_kpi=float(validator_raw["tolerance_kpi"]),
                tolerance_by_type={
                    str(key): float(value)
                    for key, value in validator_raw.get(
                        "tolerance_by_type",
                        {
                            "currency_krw": validator_raw["tolerance_default"],
                            "volume_rx": 0.5,
                            "unit_pack": 0.5,
                            "percent": validator_raw["tolerance_percent"],
                            "percent_signed": validator_raw["tolerance_percent"],
                            "kpi": validator_raw["tolerance_kpi"],
                            "rank": 0.0,
                        },
                    ).items()
                },
                relative_tolerance=float(validator_raw.get("relative_tolerance", 0.001)),
                cascade_skip_on_fail=bool(validator_raw.get("cascade_skip_on_fail", False)),
                bundle_mart_check_enabled=bool(validator_raw.get("bundle_mart_check_enabled", True)),
                bundle_invariant_check_enabled=bool(validator_raw.get("bundle_invariant_check_enabled", True)),
                narrative_event_check_enabled=bool(validator_raw.get("narrative_event_check_enabled", True)),
                narrative_event_warning_only=bool(validator_raw.get("narrative_event_warning_only", True)),
                bundle_invariant_fail_action=str(validator_raw.get("bundle_invariant_fail_action", "fail")),
            ),
            composer=ComposerConfig(
                update_cache_deep_analysis=bool(composer_raw.get("update_cache_deep_analysis", False)),
                db_host=os.environ.get("DB_HOST", str(composer_raw["db_host"])),
                db_port=int(os.environ.get("DB_PORT", composer_raw["db_port"])),
                db_user=str(composer_raw["db_user"]),
                db_name=os.environ.get("DB_NAME", str(composer_raw["db_name"])),
            ),
            retry=RetryConfig(
                max_attempts=int(retry_raw["max_attempts"]),
                backoff_sec=float(retry_raw["backoff_sec"]),
            ),
            analysis_variant=analysis_variant,
        )

    @classmethod
    def default_for_tests(cls) -> "RunnerConfig":
        return cls(
            config_version="phase_zeta_runner_genos_test",
            builder_version="test",
            genos=GenOSConfig(
                workflow_id=217,
                admin_api_url="http://llmops-admin-api-service.llmops.svc.cluster.local:8080",
                workflow_api_url="http://workflow-217.llmops.svc.cluster.local:8080",
                endpoint_path="/run/v2",
                request_payload_mode="root_question",
                response_output_path="auto",
                timeout_sec=120,
            ),
            validator=ValidatorConfig(tolerance_default=0.01, tolerance_percent=0.05, tolerance_kpi=0.05),
            composer=ComposerConfig(
                update_cache_deep_analysis=False,
                db_host="localhost",
                db_port=3308,
                db_user="root",
                db_name="jw_mart",
            ),
            retry=RetryConfig(max_attempts=2, backoff_sec=0),
            analysis_variant="legacy",
        )

    def with_analysis_variant(self, analysis_variant: str) -> "RunnerConfig":
        variant = require_analysis_variant(analysis_variant)
        config_version = self.config_version if variant == "legacy" else f"{self.config_version}:{variant}"
        return replace(self, analysis_variant=variant, config_version=config_version)
