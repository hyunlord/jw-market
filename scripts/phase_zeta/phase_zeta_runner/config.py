from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class LlmConfig:
    provider: str
    project_id: str
    region: str
    model_id: str
    temperature: float
    top_p: float
    top_k: int
    max_output_tokens: int
    response_mime_type: str


@dataclass(frozen=True)
class AuthConfig:
    method: str = "workload_identity"


@dataclass(frozen=True)
class PricingConfig:
    input_per_million_usd: float
    output_per_million_usd: float


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
    llm: LlmConfig
    auth: AuthConfig
    pricing: PricingConfig
    validator: ValidatorConfig
    composer: ComposerConfig
    retry: RetryConfig

    @classmethod
    def from_yaml(cls, path: str | Path) -> "RunnerConfig":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        root = raw.get("phase_zeta_runner", raw)
        llm_raw = root["llm"]
        auth_raw = root.get("auth", {})
        pricing_raw = root["pricing"]
        validator_raw = root["validator"]
        composer_raw = root["composer"]
        retry_raw = root["retry"]
        return cls(
            config_version=str(root["config_version"]),
            builder_version=str(root["builder_version"]),
            llm=LlmConfig(
                provider=str(llm_raw["provider"]),
                project_id=str(llm_raw["project_id"]),
                region=str(llm_raw["region"]),
                model_id=str(llm_raw["model_id"]),
                temperature=float(llm_raw["temperature"]),
                top_p=float(llm_raw["top_p"]),
                top_k=int(llm_raw["top_k"]),
                max_output_tokens=int(llm_raw["max_output_tokens"]),
                response_mime_type=str(llm_raw["response_mime_type"]),
            ),
            auth=AuthConfig(
                method=str(auth_raw.get("method", "workload_identity")),
            ),
            pricing=PricingConfig(
                input_per_million_usd=float(pricing_raw["input_per_million_usd"]),
                output_per_million_usd=float(pricing_raw["output_per_million_usd"]),
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
            config_version="phase_zeta_runner_test",
            builder_version="test",
            llm=LlmConfig(
                provider="vertex_ai",
                project_id="test-project",
                region="asia-northeast3",
                model_id="gemini-2.0-flash-001",
                temperature=0.1,
                top_p=0.95,
                top_k=40,
                max_output_tokens=4096,
                response_mime_type="application/json",
            ),
            auth=AuthConfig(method="workload_identity"),
            pricing=PricingConfig(input_per_million_usd=0.075, output_per_million_usd=0.30),
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
