from __future__ import annotations

# noqa: SIZE_OK - Secret-safe GenOS watchdog and retry boundary stays cohesive for transport-failure auditability.

import hashlib
import json
import multiprocessing as mp
import os
import queue
import socket
import time
from dataclasses import dataclass
from typing import Final

import httpx2

from .models import CallLog, JsonValue, KeywordRow, ModelSpec
from .privacy import estimate_tokens
from .response import parse_model_json


DEFAULT_CONNECT_TIMEOUT_S = 10.0
DEFAULT_READ_TIMEOUT_S = 120.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_CALL_PACING_MS = 750
DIRECT_SERVING_BACKEND: Final = "direct_serving"
DEFAULT_DIRECT_BASE_URL: Final = "https://jwai-dev.jwhealthcare.com"
DEFAULT_DIRECT_MAX_TOKENS = 4096
DEFAULT_GATEWAY_CHAT_PATH_TEMPLATE: Final = "/api/gateway/rep/serving/{serving_id}/chat/completions"
DIRECT_MODEL_ENV_BY_KEY: Final = {
    "pro": "GENOS_DIRECT_MODEL_PRO",
    "flash": "GENOS_DIRECT_MODEL_FLASH",
    "lite": "GENOS_DIRECT_MODEL_LITE",
}
DIRECT_MODEL_DEFAULT_BY_KEY: Final = {
    "pro": "genos-pro",
    "flash": "genos-flash",
    "lite": "genos-flash",
}
_LITE_SERVING: Final = "163"
MODEL_SPECS = {
    "pro": ModelSpec("pro", _LITE_SERVING, "GenOS flash-lite / serving 163 (unified)"),
    "flash": ModelSpec("flash", _LITE_SERVING, "GenOS flash-lite / serving 163 (unified)"),
    "lite": ModelSpec("lite", _LITE_SERVING, "GenOS flash-lite / serving 163"),
}


@dataclass(frozen=True, slots=True)
class GenosTimeouts:
    """Configurable GenOS timeout budgets used for one bounded call."""

    connect_s: float
    read_s: float
    watchdog_s: float


@dataclass(frozen=True, slots=True)
class LlmBackendConfig:
    """Resolved direct-serving endpoint without secrets or raw prompt data."""

    backend_key: str
    base_url: str
    serving_id: str
    model_id: str
    endpoint: str


@dataclass(frozen=True, slots=True)
class DirectServingClient:
    """OpenAI-compatible client for the cluster-internal model serving proxy."""

    base_url: str
    token: str
    serving_id: str
    model_id: str
    timeout_s: float = 90.0
    connect_timeout_s: float = 10.0

    def chat(self, messages: list[dict[str, str]]) -> dict[str, JsonValue]:
        """Call the direct serving endpoint and return sanitized metadata."""
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        path = _gateway_chat_path(self.serving_id)
        endpoint = f"{self.base_url.rstrip('/')}{path}"
        payload: dict[str, JsonValue] = {
            # GenOS Gateway selects the model from the serving_id path and may overwrite this field.
            "model": self.model_id,
            "messages": messages,
            "stream": False,
            "temperature": 0.0,
        }
        max_tokens = _int_env("GENOS_DIRECT_MAX_TOKENS", DEFAULT_DIRECT_MAX_TOKENS)
        if max_tokens > 0:
            payload["max_tokens"] = max_tokens
        start = time.perf_counter()
        phase = "connect"
        ttfb_ms = 0
        read_ms = 0
        try:
            with _direct_http_client(self.base_url, self.timeout_s, self.connect_timeout_s, headers) as client:
                with client.stream("POST", path, json=payload) as response:
                    ttfb_ms = int((time.perf_counter() - start) * 1000)
                    phase = "ttfb"
                    response.raise_for_status()
                    read_start = time.perf_counter()
                    content = response.read()
                    read_ms = int((time.perf_counter() - read_start) * 1000)
                    phase = "read"
                    body = json.loads(content)
        except (httpx2.HTTPError, json.JSONDecodeError) as exc:
            return {
                "status": "error",
                "serving_id": self.serving_id,
                "backend": DIRECT_SERVING_BACKEND,
                "endpoint": endpoint,
                "model_id": self.model_id,
                "latency_ms": int((time.perf_counter() - start) * 1000),
                "ttfb_ms": ttfb_ms,
                "read_ms": read_ms,
                "phase": phase,
                "content": "",
                "usage": {},
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:500],
            }
        return {
            "status": "ok",
            "serving_id": self.serving_id,
            "backend": DIRECT_SERVING_BACKEND,
            "endpoint": endpoint,
            "model_id": self.model_id,
            "latency_ms": int((time.perf_counter() - start) * 1000),
            "ttfb_ms": ttfb_ms,
            "read_ms": read_ms,
            "phase": "complete",
            "content": _extract_openai_content(body),
            "usage": _extract_openai_usage(body),
            "error_type": "",
            "error_message": "",
        }


class CallWatchdogTimeout(TimeoutError):
    """Raised when one GenOS call exceeds the outer process-level budget."""


class GenosChildError(RuntimeError):
    """Raised when the isolated GenOS child exits without a call result."""


def call_genos_json(
    *,
    token: str,
    spec: ModelSpec,
    task: str,
    scope_id: str,
    atc4: str,
    brand: str,
    messages: list[dict[str, str]],
    rows: list[KeywordRow],
    input_hash: str,
) -> tuple[dict[str, JsonValue], CallLog]:
    """Call GenOS once and return parsed JSON with sanitized call metadata."""
    timeouts = _timeouts_from_env()
    backend = _backend_from_env(spec)
    start = time.perf_counter()
    retry_reasons: list[str] = []
    max_retries = _int_env("GENOS_MAX_RETRIES", DEFAULT_MAX_RETRIES)
    attempts = max_retries + 1
    call: dict[str, JsonValue] = {"status": "error", "serving_id": spec.serving_id, "latency_ms": 0, "ttfb_ms": 0, "read_ms": 0, "phase": "not_started", "content": "", "usage": {}, "error_type": "not_started", "error_message": ""}
    for attempt in range(1, attempts + 1):
        _pace_call()
        try:
            call = _chat_with_process_watchdog(
                backend=backend,
                token=token,
                messages=messages,
                timeouts=timeouts,
            )
        except (CallWatchdogTimeout, GenosChildError) as exc:
            call = {
                "status": "error",
                "serving_id": spec.serving_id,
                "backend": backend.backend_key,
                "endpoint": backend.endpoint,
                "model_id": backend.model_id,
                "latency_ms": int((time.perf_counter() - start) * 1000),
                "ttfb_ms": 0,
                "read_ms": 0,
                "phase": "watchdog_timeout",
                "content": "",
                "usage": {},
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:500],
            }
        if call.get("status") == "ok" or attempt == attempts or not _is_retryable_call(call):
            attempts = attempt
            break
        retry_reasons.append(_retry_reason(call))
        time.sleep(_retry_delay_s(input_hash, attempt))
    payload = parse_model_json(call["content"])
    status = _status(call["status"], payload)
    payload["status"] = status
    if call["status"] != "ok":
        payload["error_type"] = call.get("error_type", "")
        payload["error_message"] = str(call.get("error_message", ""))[:500]
        payload["phase"] = call.get("phase", "")
    usage = call["usage"]
    measured_latency_ms = int((time.perf_counter() - start) * 1000)
    log = CallLog(
        task=task,
        model_key=spec.model_key,
        serving_id=spec.serving_id,
        scope_id=scope_id,
        atc4=atc4,
        brand=brand,
        status=status,
        latency_ms=measured_latency_ms,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        total_tokens=int(usage.get("total_tokens") or 0),
        estimated_input_tokens=sum(estimate_tokens(row.keyword_text) for row in rows),
        input_hash=input_hash,
        output_sha256=hashlib.sha256(call["content"].encode("utf-8")).hexdigest() if call["content"] else "",
        output_length=len(call["content"]),
        error_type=call.get("error_type", ""),
        error_message=str(call.get("error_message", ""))[:500],
        phase=str(call.get("phase", "")),
        ttfb_ms=int(call.get("ttfb_ms") or 0),
        read_ms=int(call.get("read_ms") or 0),
        connect_timeout_s=timeouts.connect_s,
        read_timeout_s=timeouts.read_s,
        watchdog_timeout_s=timeouts.watchdog_s,
        attempts=attempts,
        retry_count=max(0, attempts - 1),
        retry_reasons=tuple(retry_reasons),
        backend=str(call.get("backend") or backend.backend_key),
        endpoint=str(call.get("endpoint") or backend.endpoint),
        model_id=str(call.get("model_id") or backend.model_id),
    )
    return payload, log


def call_log_to_json(log: CallLog) -> dict[str, JsonValue]:
    """Serialize a call log without raw prompt or raw response content."""
    return {
        "task": log.task,
        "model_key": log.model_key,
        "serving_id": log.serving_id,
        "scope_id": log.scope_id,
        "atc4": log.atc4,
        "brand": log.brand,
        "status": log.status,
        "latency_ms": log.latency_ms,
        "usage": {
            "prompt_tokens": log.prompt_tokens,
            "completion_tokens": log.completion_tokens,
            "total_tokens": log.total_tokens,
        },
        "estimated_input_tokens": log.estimated_input_tokens,
        "input_hash": log.input_hash,
        "raw_output_sha256": log.output_sha256,
        "raw_output_length": log.output_length,
        "error_type": log.error_type,
        "error_message": log.error_message,
        "phase": log.phase,
        "timing": {
            "ttfb_ms": log.ttfb_ms,
            "read_ms": log.read_ms,
            "connect_timeout_s": log.connect_timeout_s,
            "read_timeout_s": log.read_timeout_s,
            "watchdog_timeout_s": log.watchdog_timeout_s,
        },
        "retry": {
            "attempts": log.attempts,
            "retry_count": log.retry_count,
            "reasons": list(log.retry_reasons),
        },
        "backend": log.backend,
        "endpoint": log.endpoint,
        "model_id": log.model_id,
    }


def _status(call_status: str, payload: dict[str, JsonValue]) -> str:
    """Map transport/parser status into audit-safe execution status."""
    if call_status != "ok":
        return "error"
    if "_invalid" in payload:
        return "quarantined_invalid_json"
    return "ok"


def _timeouts_from_env() -> GenosTimeouts:
    """Read GenOS timeout settings while preserving the requested defaults."""
    connect_s = _float_env("GENOS_CONNECT_TIMEOUT_S", DEFAULT_CONNECT_TIMEOUT_S)
    read_s = _float_env("GENOS_READ_TIMEOUT_S", DEFAULT_READ_TIMEOUT_S)
    watchdog_s = _float_env("GENOS_WATCHDOG_TIMEOUT_S", connect_s + read_s + 30.0)
    return GenosTimeouts(connect_s=connect_s, read_s=read_s, watchdog_s=watchdog_s)


def _float_env(name: str, default: float) -> float:
    """Parse a positive float from environment or fall back to a safe default."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _int_env(name: str, default: int) -> int:
    """Parse a non-negative integer from environment or fall back to a safe default."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def _pace_call() -> None:
    """Apply small inter-call pacing to reduce GenOS 429 pressure."""
    pacing_ms = _int_env("GENOS_CALL_PACING_MS", DEFAULT_CALL_PACING_MS)
    if pacing_ms > 0:
        time.sleep(pacing_ms / 1000)


def _is_retryable_call(call: dict[str, JsonValue]) -> bool:
    """Return whether a sanitized GenOS error merits a bounded retry."""
    if call.get("status") == "ok":
        return False
    error_type = str(call.get("error_type") or "")
    error_message = str(call.get("error_message") or "")
    phase = str(call.get("phase") or "")
    return "429" in error_message or "Too Many Requests" in error_message or error_type in {"CallWatchdogTimeout", "GenosChildError"} or phase in {"watchdog_timeout", "child_exception"}


def _retry_reason(call: dict[str, JsonValue]) -> str:
    """Return a compact retry reason without raw response content."""
    error_message = str(call.get("error_message") or "")
    if "429" in error_message or "Too Many Requests" in error_message:
        return "http_429"
    return str(call.get("error_type") or call.get("phase") or "retryable_error")


def _retry_delay_s(input_hash: str, attempt: int) -> float:
    """Calculate deterministic exponential backoff with bounded jitter."""
    jitter = int(hashlib.sha256(f"{input_hash}:{attempt}".encode("utf-8")).hexdigest()[:4], 16) / 65535
    return min(12.0, (2 ** (attempt - 1)) + jitter)


def _backend_from_env(spec: ModelSpec) -> LlmBackendConfig:
    """Resolve the retained serving-direct backend."""
    base_url = os.environ.get("GENOS_DIRECT_BASE_URL", DEFAULT_DIRECT_BASE_URL).rstrip("/")
    model_id = os.environ.get(DIRECT_MODEL_ENV_BY_KEY.get(spec.model_key, ""), "") or DIRECT_MODEL_DEFAULT_BY_KEY.get(spec.model_key, spec.serving_id)
    return LlmBackendConfig(
        backend_key=DIRECT_SERVING_BACKEND,
        base_url=base_url,
        serving_id=spec.serving_id,
        model_id=model_id,
        endpoint=f"{base_url}{_gateway_chat_path(spec.serving_id)}",
    )


def _gateway_chat_path(serving_id: str) -> str:
    """Return the GenOS Gateway OpenAI-compatible chat path for one serving."""
    return _gateway_chat_path_template().format(serving_id=serving_id)


def _gateway_chat_path_template() -> str:
    """Return the environment-selectable chat path template for internal or external Gateway use."""
    return os.environ.get("GENOS_GATEWAY_CHAT_PATH_TEMPLATE", DEFAULT_GATEWAY_CHAT_PATH_TEMPLATE)


def _chat_with_process_watchdog(*, backend: LlmBackendConfig, token: str, messages: list[dict[str, str]], timeouts: GenosTimeouts) -> dict[str, JsonValue]:
    """Run one GenOS request in a child process that the parent can terminate."""
    context = mp.get_context("fork" if hasattr(os, "fork") else "spawn")
    result_queue = context.Queue(maxsize=1)
    process = context.Process(
        target=call_child_chat,
        args=(backend, token, timeouts.read_s, timeouts.connect_s, messages, result_queue),
    )
    process.start()
    process.join(timeouts.watchdog_s)
    if process.is_alive():
        process.terminate()
        process.join(5.0)
        if process.is_alive():
            process.kill()
            process.join(5.0)
        raise CallWatchdogTimeout(f"GenOS call exceeded watchdog timeout {timeouts.watchdog_s:.1f}s")
    try:
        value = result_queue.get_nowait()
    except queue.Empty as exc:
        raise GenosChildError(f"GenOS child exited without a result, exitcode={process.exitcode}") from exc
    return value if isinstance(value, dict) else {"status": "error", "content": "", "usage": {}, "error_type": "GenosChildError", "error_message": "non-dict child result"}


def call_child_chat(
    backend: LlmBackendConfig,
    token: str,
    read_timeout_s: float,
    connect_timeout_s: float,
    messages: list[dict[str, str]],
    result_queue: mp.Queue,
) -> None:
    """Execute the blocking GenOS client call inside the terminable child."""
    try:
        result_queue.put(_chat_for_backend(backend, token, read_timeout_s, connect_timeout_s, messages))
    except Exception as exc:  # noqa: BROAD_EXCEPT_OK - child process boundary marshals unknown GenOS/client failures to the parent.
        result_queue.put(
            {
                "status": "error",
                "serving_id": backend.serving_id,
                "backend": backend.backend_key,
                "endpoint": backend.endpoint,
                "model_id": backend.model_id,
                "latency_ms": 0,
                "ttfb_ms": 0,
                "read_ms": 0,
                "phase": "child_exception",
                "content": "",
                "usage": {},
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:500],
            }
        )


def _chat_for_backend(backend: LlmBackendConfig, token: str, read_timeout_s: float, connect_timeout_s: float, messages: list[dict[str, str]]) -> dict[str, JsonValue]:
    """Dispatch one chat call to the retained direct-serving backend."""
    client = DirectServingClient(backend.base_url, token, backend.serving_id, backend.model_id, timeout_s=read_timeout_s, connect_timeout_s=connect_timeout_s)
    return client.chat(messages)


def _direct_http_client(base_url: str, timeout_s: float, connect_timeout_s: float, headers: dict[str, str]) -> httpx2.Client:
    """Create the tuned sync HTTP client used for direct serving calls."""
    limits = httpx2.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=30.0)
    timeout = httpx2.Timeout(connect=connect_timeout_s, read=timeout_s, write=10.0, pool=10.0)
    transport = httpx2.HTTPTransport(
        http2=True,
        retries=3,
        limits=limits,
        socket_options=[(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)],
    )
    return httpx2.Client(
        base_url=base_url.rstrip("/"),
        headers=headers,
        timeout=timeout,
        transport=transport,
        follow_redirects=True,
    )


def _extract_openai_content(payload: JsonValue) -> str:
    """Return assistant content from an OpenAI-compatible response."""
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"]
            text = first.get("text")
            if isinstance(text, str):
                return text
    return ""


def _extract_openai_usage(payload: JsonValue) -> dict[str, JsonValue]:
    """Return token usage fields from an OpenAI-compatible response."""
    if not isinstance(payload, dict):
        return {}
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return {}
    return {
        key: value
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if isinstance((value := usage.get(key)), int)
    }
