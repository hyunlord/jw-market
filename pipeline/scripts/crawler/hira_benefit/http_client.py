from __future__ import annotations

import http.cookiejar
import json
import random
import socket
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from email.message import Message
from urllib.error import HTTPError, URLError
from urllib.request import Request

LIST_SLOW_RESPONSE_SECONDS = 2.212
DETAIL_SLOW_RESPONSE_SECONDS = 0.636


@dataclass(frozen=True, slots=True)
class HiraRequestPolicy:
    """F18-approved host pacing and transient-failure policy."""

    delay_after_response_seconds: float = 2.0
    request_jitter_seconds: float = 0.5
    maximum_attempts: int = 3
    backoff_seconds: tuple[float, ...] = (5.0, 15.0, 45.0)
    backoff_jitter_ratio: float = 0.2
    circuit_failure_limit: int = 3
    circuit_pause_seconds: int = 1800
    slow_response_seconds: float = DETAIL_SLOW_RESPONSE_SECONDS
    slow_response_limit: int = 5
    timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        if self.maximum_attempts < 0:
            raise ValueError("maximum_attempts cannot be negative")
        if len(self.backoff_seconds) < self.maximum_attempts:
            raise ValueError("one backoff value is required per retry")


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str] | Message
    body: bytes


@dataclass(frozen=True, slots=True)
class RequestEvent:
    """Secret-free request telemetry for F18 pacing and retry evidence."""

    url: str
    status: int | None
    elapsed_seconds: float
    retry_count: int
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class CircuitOpenError(RuntimeError):
    reason: str
    retry_after_seconds: int

    def __str__(self) -> str:
        return (
            f"HIRA circuit open: {self.reason}; "
            f"resume_after={self.retry_after_seconds}s"
        )


Transport = Callable[[Request, float], HttpResponse]
Observer = Callable[[RequestEvent], None]


def _print_request_event(event: RequestEvent) -> None:
    print(
        json.dumps(
            {
                "event": "hira_http_request",
                "url": event.url,
                "status": event.status,
                "elapsed_seconds": round(event.elapsed_seconds, 6),
                "retry_count": event.retry_count,
                "error_type": event.error_type,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )


@dataclass(slots=True)
class UrllibTransport:
    """Stateful urllib transport that preserves the HIRA session cookie."""

    opener: urllib.request.OpenerDirector = field(
        default_factory=lambda: urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
    )

    def __call__(self, request: Request, timeout: float) -> HttpResponse:
        with self.opener.open(request, timeout=timeout) as response:
            return HttpResponse(
                status=int(response.status),
                headers=dict(response.headers.items()),
                body=response.read(),
            )


@dataclass(slots=True)
class HiraHttpClient:
    """Single-host client enforcing F18 pacing, retries, and circuit breaking."""

    policy: HiraRequestPolicy = field(default_factory=HiraRequestPolicy)
    user_agent: str = (
        "JWHealth-HIRA-InsuranceCriteriaBot/1.0 "
        "(approved-internal-sync; monitored-contact-required)"
    )
    transport: Transport = field(default_factory=UrllibTransport)
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    uniform: Callable[[float, float], float] = random.uniform
    observe: Observer = _print_request_event
    _last_response_at: float | None = None
    _consecutive_failed_requests: int = 0
    _consecutive_slow_responses: int = 0

    def get_text(self, url: str) -> str:
        return self._request_text(Request(url, headers=self._headers()))

    def post_form_text(self, url: str, form: Mapping[str, str]) -> str:
        payload = urllib.parse.urlencode(form).encode("ascii")
        return self._request_text(
            Request(
                url,
                data=payload,
                method="POST",
                headers={
                    **self._headers(),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
        )

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml",
        }

    def _request_text(self, request: Request) -> str:
        if not request.full_url.startswith("https://www.hira.or.kr/"):
            raise ValueError("HIRA client refuses non-approved origins")
        self._wait_for_rate_limit()
        response = self._send_with_retry(request)
        self._last_response_at = self.monotonic()
        charset = "utf-8"
        if isinstance(response.headers, Message):
            charset = response.headers.get_content_charset() or charset
        return response.body.decode(charset, errors="replace")

    def _wait_for_rate_limit(self) -> None:
        if self._last_response_at is None:
            return
        elapsed = self.monotonic() - self._last_response_at
        target = self.policy.delay_after_response_seconds + self.uniform(
            0.0,
            self.policy.request_jitter_seconds,
        )
        if target > elapsed:
            self.sleep(target - elapsed)

    def _send_with_retry(self, request: Request) -> HttpResponse:
        for attempt in range(self.policy.maximum_attempts + 1):
            started = self.monotonic()
            try:
                response = self.transport(request, self.policy.timeout_seconds)
            except HTTPError as error:
                self.observe(
                    RequestEvent(
                        url=request.full_url,
                        status=error.code,
                        elapsed_seconds=self.monotonic() - started,
                        retry_count=attempt,
                        error_type=type(error).__name__,
                    )
                )
                if error.code not in {429, 500, 502, 503, 504}:
                    raise
                if attempt == self.policy.maximum_attempts:
                    self._record_failed_request(f"http_{error.code}")
                    raise
                self.sleep(self._retry_delay(attempt, error.headers))
                continue
            except (TimeoutError, socket.timeout, URLError) as error:
                self.observe(
                    RequestEvent(
                        url=request.full_url,
                        status=None,
                        elapsed_seconds=self.monotonic() - started,
                        retry_count=attempt,
                        error_type=type(error).__name__,
                    )
                )
                if attempt == self.policy.maximum_attempts:
                    self._record_failed_request(type(error).__name__)
                    raise
                self.sleep(self._retry_delay(attempt, None))
                continue
            elapsed_seconds = self.monotonic() - started
            self.observe(
                RequestEvent(
                    url=request.full_url,
                    status=response.status,
                    elapsed_seconds=elapsed_seconds,
                    retry_count=attempt,
                )
            )
            self._record_success(elapsed_seconds)
            return response
        raise AssertionError("retry loop exhausted without returning or raising")

    def _retry_delay(
        self,
        retry_index: int,
        headers: Mapping[str, str] | Message | None,
    ) -> float:
        retry_after = headers.get("Retry-After") if headers is not None else None
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        base = self.policy.backoff_seconds[retry_index]
        return base * (
            1.0 + self.uniform(0.0, self.policy.backoff_jitter_ratio)
        )

    def _record_failed_request(self, reason: str) -> None:
        self._consecutive_failed_requests += 1
        if self._consecutive_failed_requests >= self.policy.circuit_failure_limit:
            raise CircuitOpenError(
                reason=reason,
                retry_after_seconds=self.policy.circuit_pause_seconds,
            )

    def _record_success(self, elapsed_seconds: float) -> None:
        self._consecutive_failed_requests = 0
        if elapsed_seconds > self.policy.slow_response_seconds:
            self._consecutive_slow_responses += 1
        else:
            self._consecutive_slow_responses = 0
        if self._consecutive_slow_responses >= self.policy.slow_response_limit:
            raise CircuitOpenError(
                reason=f"slow_response={elapsed_seconds:.3f}s",
                retry_after_seconds=self.policy.circuit_pause_seconds,
            )
