from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any

from .config import RunnerConfig
from .genos_caller import STAGES, call_genos_workflow
from .prompt_builder import build_question_string


@dataclass
class LLMResult:
    success: bool
    parsed_output: dict[str, Any]
    raw_response: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    duration_sec: float
    model_version: str
    retry_count: int
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Backwards-compatible name used by output_composer and Stage 3-C audit code.
GeminiResult = LLMResult


def _is_retryable(error: str | None) -> bool:
    text = (error or "").lower()
    return any(fragment in text for fragment in ("429", "quota", "rate", "timeout", "temporarily", "503"))


def call_llm(bundle: dict[str, Any], config: RunnerConfig) -> LLMResult:
    """Call PL-managed GenOS workflow 217 for the 4-stage Phase ζ analysis."""

    start = time.time()
    processing_mode = str((bundle.get("bundle_meta") or {}).get("processing_mode") or "full")
    question = build_question_string(bundle, config, mode=processing_mode)
    attempts = max(1, config.retry.max_attempts)
    last_result: dict[str, Any] | None = None
    last_attempt = 0

    for attempt in range(attempts):
        last_attempt = attempt
        result = call_genos_workflow(question, config, mode=processing_mode)
        last_result = result
        if result["success"]:
            return LLMResult(
                success=True,
                parsed_output=result["parsed_output"],
                raw_response=result["raw_response"],
                tokens_in=int(result.get("tokens_in") or 0),
                tokens_out=int(result.get("tokens_out") or 0),
                cost_usd=0.0,
                duration_sec=float(result.get("duration_sec") or (time.time() - start)),
                model_version=f"genos_workflow_{config.genos.workflow_id}",
                retry_count=attempt,
                error=None,
            )
        if attempt + 1 < attempts and _is_retryable(result.get("error")):
            time.sleep(config.retry.backoff_sec)
            continue
        break

    return LLMResult(
        success=False,
        parsed_output={},
        raw_response=str((last_result or {}).get("raw_response", "")),
        tokens_in=0,
        tokens_out=0,
        cost_usd=0.0,
        duration_sec=time.time() - start,
        model_version=f"genos_workflow_{config.genos.workflow_id}",
        retry_count=last_attempt,
        error=str((last_result or {}).get("error") or "unknown GenOS workflow error"),
    )


def call_gemini(bundle: dict[str, Any], config: RunnerConfig) -> LLMResult:
    """Compatibility shim: Stage 3-C-genos routes all LLM calls through GenOS."""

    return call_llm(bundle, config)
