from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any

from .config import RunnerConfig
from .prompt_builder import build_question_string


STAGES = ("phenomenon", "cause", "prediction", "recommendation")
BULLET_RANGES_BY_MODE = {
    "full": (2, 3),
    "compact": (2, 3),
    "recap": (1, 2),
}


def _bullet_range_for_mode(mode: str) -> tuple[int, int]:
    return BULLET_RANGES_BY_MODE.get(mode, BULLET_RANGES_BY_MODE["full"])


def validate_genos_output(parsed: dict[str, Any], mode: str = "full") -> dict[str, Any]:
    min_bullets, max_bullets = _bullet_range_for_mode(mode)
    errors: list[str] = []
    for stage in STAGES:
        stage_data = parsed.get(stage)
        if not isinstance(stage_data, dict):
            errors.append(f"{stage} is missing or not an object")
            continue
        for key in ("title", "body", "bullets"):
            if key not in stage_data:
                errors.append(f"{stage}.{key} missing")
        bullets = stage_data.get("bullets")
        if not isinstance(bullets, list):
            errors.append(f"{stage}.bullets is not a list")
        elif not min_bullets <= len(bullets) <= max_bullets:
            errors.append(f"{stage}.bullets has {len(bullets)} items; expected {min_bullets}-{max_bullets}")
    return {"valid": not errors, "errors": errors}


def _payload(question: str, mode: str) -> dict[str, Any]:
    if mode == "data_question":
        return {"data": {"question": question}}
    if mode == "variables_question":
        return {"variables": {"question": question}}
    return {"question": question}


def _strip_markdown_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return cleaned


def _has_required_stages(obj: Any) -> bool:
    return isinstance(obj, dict) and all(stage in obj for stage in STAGES)


def _json_candidates_from_text(text: str) -> list[Any]:
    candidates: list[Any] = []
    cleaned = _strip_markdown_fence(text)
    for candidate in (cleaned,):
        try:
            candidates.append(json.loads(candidate))
        except json.JSONDecodeError:
            pass

    # Some Flowise nodes wrap the model answer in surrounding text. Keep this
    # fallback narrow by requiring the first/last braces and a successful parse.
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            candidates.append(json.loads(cleaned[start : end + 1]))
        except json.JSONDecodeError:
            pass
    return candidates


def _find_stage_object(obj: Any) -> dict[str, Any] | None:
    if _has_required_stages(obj):
        return obj
    if isinstance(obj, str):
        for candidate in _json_candidates_from_text(obj):
            found = _find_stage_object(candidate)
            if found is not None:
                return found
    elif isinstance(obj, dict):
        # Prefer likely LLM-output fields before walking request/config fields.
        for key in ("output", "result", "response", "answer", "content", "text", "message"):
            if key in obj:
                found = _find_stage_object(obj[key])
                if found is not None:
                    return found
        for key, value in obj.items():
            if key.lower() in {"input", "state", "messages", "prompt", "systemmessage", "agentmodelconfig"}:
                continue
            found = _find_stage_object(value)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in reversed(obj):
            found = _find_stage_object(value)
            if found is not None:
                return found
    return None


def _sum_token_keys(obj: Any, keys: tuple[str, ...]) -> int:
    total = 0
    if isinstance(obj, dict):
        for key, value in obj.items():
            normalized = key.lower()
            if normalized in keys and isinstance(value, (int, float)):
                total += int(value)
            else:
                total += _sum_token_keys(value, keys)
    elif isinstance(obj, list):
        for value in obj:
            total += _sum_token_keys(value, keys)
    return total


def _extract_tokens(response_json: dict[str, Any]) -> tuple[int, int]:
    input_keys = (
        "prompttokens",
        "prompt_tokens",
        "inputtokens",
        "input_tokens",
        "prompttokencount",
        "prompt_token_count",
    )
    output_keys = (
        "completiontokens",
        "completion_tokens",
        "outputtokens",
        "output_tokens",
        "candidatestokencount",
        "candidates_token_count",
    )
    return _sum_token_keys(response_json, input_keys), _sum_token_keys(response_json, output_keys)


def parse_genos_response(response_json: dict[str, Any], mode: str = "full") -> dict[str, Any]:
    parsed = _find_stage_object(response_json)
    if parsed is None:
        raise ValueError("GenOS response does not contain the required 4-stage JSON object")
    validation = validate_genos_output(parsed, mode)
    if not validation["valid"]:
        raise ValueError("; ".join(validation["errors"]))
    return parsed


def call_genos_workflow(question: str, config: RunnerConfig, mode: str = "full") -> dict[str, Any]:
    start = time.time()
    url = f"{config.genos.workflow_api_url}{config.genos.endpoint_path}"
    payload = _payload(question, config.genos.request_payload_mode)
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=config.genos.timeout_sec) as response:
            raw_response = response.read().decode("utf-8")
            response_json = json.loads(raw_response)
        if int(response_json.get("code", 0)) != 0:
            raise ValueError(response_json.get("errMsg") or response_json)
        parsed_output = parse_genos_response(response_json, mode)
        tokens_in, tokens_out = _extract_tokens(response_json)
        return {
            "success": True,
            "parsed_output": parsed_output,
            "raw_response": json.dumps(response_json, ensure_ascii=False),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "duration_sec": time.time() - start,
            "error": None,
        }
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {
            "success": False,
            "parsed_output": {},
            "raw_response": body,
            "tokens_in": 0,
            "tokens_out": 0,
            "duration_sec": time.time() - start,
            "error": f"HTTPError {exc.code}: {body[:1000]}",
        }
    except Exception as exc:
        return {
            "success": False,
            "parsed_output": {},
            "raw_response": locals().get("raw_response", ""),
            "tokens_in": 0,
            "tokens_out": 0,
            "duration_sec": time.time() - start,
            "error": f"{type(exc).__name__}: {exc}",
        }


__all__ = [
    "build_question_string",
    "call_genos_workflow",
    "parse_genos_response",
    "validate_genos_output",
]
