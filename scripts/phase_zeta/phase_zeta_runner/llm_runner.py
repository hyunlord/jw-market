from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import Any

from .config import RunnerConfig
from .prompt_builder import UnifiedPrompt


STAGES = ("phenomenon", "cause", "prediction", "recommendation")


@dataclass
class GeminiResult:
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


def _is_retryable(exc: Exception) -> bool:
    text = str(exc).lower()
    retryable_fragments = (
        "429",
        "quota",
        "rate",
        "timeout",
        "temporarily unavailable",
        "503",
        "deadline",
        "json",
        "missing required",
    )
    return any(fragment in text for fragment in retryable_fragments)


def _raw_response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if text:
        return str(text)

    candidates = getattr(response, "candidates", None) or []
    parts: list[str] = []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", []) or []:
            part_text = getattr(part, "text", None)
            if part_text:
                parts.append(str(part_text))
    return "\n".join(parts)


def _usage_counts(response: Any) -> tuple[int, int]:
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return 0, 0
    tokens_in = int(getattr(usage, "prompt_token_count", 0) or 0)
    tokens_out = int(getattr(usage, "candidates_token_count", 0) or 0)
    return tokens_in, tokens_out


def _validate_parsed_output(parsed: dict[str, Any]) -> None:
    missing = [stage for stage in STAGES if stage not in parsed]
    if missing:
        raise ValueError(f"missing required stages: {', '.join(missing)}")
    for stage in STAGES:
        stage_obj = parsed.get(stage)
        if not isinstance(stage_obj, dict):
            raise ValueError(f"{stage} must be an object")
        for key in ("title", "body", "bullets"):
            if key not in stage_obj:
                raise ValueError(f"{stage}.{key} is missing")


def _init_vertex_ai(config: RunnerConfig) -> None:
    import vertexai  # type: ignore

    # In the GKE dry-test pod, Workload Identity supplies credentials through
    # the metadata server. Do not pass explicit credentials here.
    vertexai.init(project=config.llm.project_id, location=config.llm.region)


def _call_once(prompt: UnifiedPrompt, config: RunnerConfig) -> tuple[dict[str, Any], str, int, int]:
    from vertexai.generative_models import (  # type: ignore
        GenerationConfig,
        GenerativeModel,
        HarmBlockThreshold,
        HarmCategory,
    )

    _init_vertex_ai(config)

    model = GenerativeModel(
        model_name=config.llm.model_id,
        system_instruction=prompt.system_instruction,
    )
    generation_config = GenerationConfig(
        temperature=config.llm.temperature,
        top_p=config.llm.top_p,
        top_k=config.llm.top_k,
        max_output_tokens=config.llm.max_output_tokens,
        response_mime_type=config.llm.response_mime_type,
        response_schema=prompt.response_schema,
    )
    safety_settings = {
        HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
    }

    response = model.generate_content(
        [prompt.user_message],
        generation_config=generation_config,
        safety_settings=safety_settings,
    )
    raw_response = _raw_response_text(response)
    parsed = json.loads(raw_response)
    _validate_parsed_output(parsed)
    tokens_in, tokens_out = _usage_counts(response)
    return parsed, raw_response, tokens_in, tokens_out


def call_gemini(prompt: UnifiedPrompt, config: RunnerConfig) -> GeminiResult:
    start = time.time()
    attempts = max(1, config.retry.max_attempts)
    last_error: str | None = None
    last_attempt = 0

    for attempt in range(attempts):
        last_attempt = attempt
        try:
            parsed, raw_response, tokens_in, tokens_out = _call_once(prompt, config)
            cost_usd = (
                tokens_in * config.pricing.input_per_million_usd / 1_000_000
                + tokens_out * config.pricing.output_per_million_usd / 1_000_000
            )
            return GeminiResult(
                success=True,
                parsed_output=parsed,
                raw_response=raw_response,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
                duration_sec=time.time() - start,
                model_version=config.llm.model_id,
                retry_count=attempt,
                error=None,
            )
        except Exception as exc:  # pragma: no cover - exercised by integration dry-runs
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt + 1 < attempts and _is_retryable(exc):
                time.sleep(config.retry.backoff_sec)
                continue
            break

    return GeminiResult(
        success=False,
        parsed_output={},
        raw_response="",
        tokens_in=0,
        tokens_out=0,
        cost_usd=0.0,
        duration_sec=time.time() - start,
        model_version=config.llm.model_id,
        retry_count=last_attempt,
        error=last_error,
    )
