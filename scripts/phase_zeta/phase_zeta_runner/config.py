from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


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
    cascade_skip_on_fail: bool = False


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

    @classmethod
    def from_yaml(cls, path: str | Path) -> "RunnerConfig":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        root = raw.get("phase_zeta_runner", raw)
        genos_raw = root["genos"]
        validator_raw = root["validator"]
        composer_raw = root["composer"]
        retry_raw = root["retry"]
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
                cascade_skip_on_fail=bool(validator_raw.get("cascade_skip_on_fail", False)),
            ),
            composer=ComposerConfig(
                update_cache_deep_analysis=bool(composer_raw.get("update_cache_deep_analysis", False)),
                db_host=str(composer_raw["db_host"]),
                db_port=int(composer_raw["db_port"]),
                db_user=str(composer_raw["db_user"]),
                db_name=str(composer_raw["db_name"]),
            ),
            retry=RetryConfig(
                max_attempts=int(retry_raw["max_attempts"]),
                backoff_sec=float(retry_raw["backoff_sec"]),
            ),
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
        )
