from __future__ import annotations

from email.message import Message
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from pipeline.scripts.crawler.hira_benefit.http_client import (
    DETAIL_SLOW_RESPONSE_SECONDS,
    LIST_SLOW_RESPONSE_SECONDS,
    CircuitOpenError,
    HiraHttpClient,
    HiraRequestPolicy,
    HttpResponse,
    RequestEvent,
)


def _response(body: str = "ok") -> HttpResponse:
    return HttpResponse(status=200, headers=Message(), body=body.encode())


def _http_error(status: int) -> HTTPError:
    return HTTPError(
        url="https://www.hira.or.kr/test",
        code=status,
        msg="transient",
        hdrs=Message(),
        fp=None,
    )


def test_f18_policy_defaults_are_exact() -> None:
    policy = HiraRequestPolicy()

    assert policy.delay_after_response_seconds == 2.0
    assert policy.request_jitter_seconds == 0.5
    assert policy.maximum_attempts == 3
    assert policy.backoff_seconds == (5.0, 15.0, 45.0)
    assert policy.backoff_jitter_ratio == 0.2
    assert policy.circuit_failure_limit == 3
    assert policy.circuit_pause_seconds == 1800
    assert DETAIL_SLOW_RESPONSE_SECONDS == 0.636
    assert LIST_SLOW_RESPONSE_SECONDS == 2.212
    assert policy.slow_response_seconds == DETAIL_SLOW_RESPONSE_SECONDS


def test_client_applies_two_seconds_plus_jitter_between_requests() -> None:
    sleeps: list[float] = []
    requests: list[Request] = []

    def transport(request: Request, _timeout: float) -> HttpResponse:
        requests.append(request)
        return _response()

    client = HiraHttpClient(
        policy=HiraRequestPolicy(),
        transport=transport,
        sleep=sleeps.append,
        monotonic=lambda: 0.0,
        uniform=lambda _start, _end: 0.5,
    )

    client.get_text("https://www.hira.or.kr/one")
    client.get_text("https://www.hira.or.kr/two")

    assert [request.full_url for request in requests] == [
        "https://www.hira.or.kr/one",
        "https://www.hira.or.kr/two",
    ]
    assert sleeps == [2.5]


def test_transient_http_error_retries_with_f18_backoff() -> None:
    sleeps: list[float] = []
    results: list[HttpResponse | HTTPError] = [
        _http_error(500),
        _http_error(503),
        _response("recovered"),
    ]

    def transport(_request: Request, _timeout: float) -> HttpResponse:
        result = results.pop(0)
        if isinstance(result, HTTPError):
            raise result
        return result

    client = HiraHttpClient(
        policy=HiraRequestPolicy(delay_after_response_seconds=0.0),
        transport=transport,
        sleep=sleeps.append,
        monotonic=lambda: 0.0,
        uniform=lambda _start, _end: 0.0,
    )

    assert client.get_text("https://www.hira.or.kr/retry") == "recovered"
    assert sleeps == [5.0, 15.0]


def test_transient_failure_uses_exactly_three_f18_retries() -> None:
    sleeps: list[float] = []
    calls = 0

    def transport(_request: Request, _timeout: float) -> HttpResponse:
        nonlocal calls
        calls += 1
        raise _http_error(503)

    client = HiraHttpClient(
        policy=HiraRequestPolicy(delay_after_response_seconds=0.0),
        transport=transport,
        sleep=sleeps.append,
        monotonic=lambda: 0.0,
        uniform=lambda _start, _end: 0.0,
        observe=lambda _event: None,
    )

    with pytest.raises(HTTPError):
        client.get_text("https://www.hira.or.kr/exhausted")

    assert calls == 4
    assert sleeps == [5.0, 15.0, 45.0]


def test_non_transient_http_error_is_not_retried() -> None:
    calls = 0

    def transport(_request: Request, _timeout: float) -> HttpResponse:
        nonlocal calls
        calls += 1
        raise _http_error(404)

    client = HiraHttpClient(
        transport=transport,
        sleep=lambda _seconds: None,
        monotonic=lambda: 0.0,
        observe=lambda _event: None,
    )

    with pytest.raises(HTTPError):
        client.get_text("https://www.hira.or.kr/not-found")

    assert calls == 1


def test_retry_after_overrides_calculated_backoff() -> None:
    sleeps: list[float] = []
    headers = Message()
    headers["Retry-After"] = "17"
    results: list[HttpResponse | HTTPError] = [
        HTTPError(
            url="https://www.hira.or.kr/test",
            code=429,
            msg="rate limited",
            hdrs=headers,
            fp=None,
        ),
        _response("recovered"),
    ]

    def transport(_request: Request, _timeout: float) -> HttpResponse:
        result = results.pop(0)
        if isinstance(result, HTTPError):
            raise result
        return result

    client = HiraHttpClient(
        policy=HiraRequestPolicy(delay_after_response_seconds=0.0),
        transport=transport,
        sleep=sleeps.append,
        monotonic=lambda: 0.0,
        uniform=lambda _start, _end: 0.0,
        observe=lambda _event: None,
    )

    assert client.get_text("https://www.hira.or.kr/retry-after") == "recovered"
    assert sleeps == [17.0]


def test_request_events_record_status_elapsed_and_retry_count() -> None:
    events: list[RequestEvent] = []
    results: list[HttpResponse | HTTPError] = [
        _http_error(503),
        _response("recovered"),
    ]
    ticks = iter((10.0, 10.4, 20.0, 20.6, 21.0))

    def transport(_request: Request, _timeout: float) -> HttpResponse:
        result = results.pop(0)
        if isinstance(result, HTTPError):
            raise result
        return result

    client = HiraHttpClient(
        policy=HiraRequestPolicy(delay_after_response_seconds=0.0),
        transport=transport,
        sleep=lambda _seconds: None,
        monotonic=lambda: next(ticks),
        uniform=lambda _start, _end: 0.0,
        observe=events.append,
    )

    assert client.get_text("https://www.hira.or.kr/observed") == "recovered"
    assert [(event.status, event.retry_count) for event in events] == [
        (503, 0),
        (200, 1),
    ]
    assert [round(event.elapsed_seconds, 3) for event in events] == [0.4, 0.6]


def test_circuit_opens_for_thirty_minutes_after_three_failed_requests() -> None:
    sleeps: list[float] = []

    def transport(_request: Request, _timeout: float) -> HttpResponse:
        raise _http_error(500)

    client = HiraHttpClient(
        policy=HiraRequestPolicy(
            delay_after_response_seconds=0.0,
            maximum_attempts=0,
        ),
        transport=transport,
        sleep=sleeps.append,
        monotonic=lambda: 0.0,
        uniform=lambda _start, _end: 0.0,
        observe=lambda _event: None,
    )

    with pytest.raises(HTTPError):
        client.get_text("https://www.hira.or.kr/fail-1")
    with pytest.raises(HTTPError):
        client.get_text("https://www.hira.or.kr/fail-2")
    with pytest.raises(CircuitOpenError) as error:
        client.get_text("https://www.hira.or.kr/fail-3")

    assert error.value.retry_after_seconds == 1800
    assert sleeps == []


def test_five_consecutive_four_x_slow_responses_open_circuit() -> None:
    ticks = iter(
        (
            0.0,
            0.7,
            0.7,
            0.7,
            1.0,
            1.7,
            1.7,
            1.7,
            2.0,
            2.7,
            2.7,
            2.7,
            3.0,
            3.7,
            3.7,
            3.7,
            4.0,
            4.7,
        )
    )
    client = HiraHttpClient(
        policy=HiraRequestPolicy(
            delay_after_response_seconds=0.0,
            slow_response_seconds=0.64,
        ),
        transport=lambda _request, _timeout: _response(),
        sleep=lambda _seconds: None,
        monotonic=lambda: next(ticks),
        uniform=lambda _start, _end: 0.0,
        observe=lambda _event: None,
    )

    for index in range(4):
        assert client.get_text(f"https://www.hira.or.kr/slow-{index}") == "ok"
    with pytest.raises(CircuitOpenError, match="slow_response"):
        client.get_text("https://www.hira.or.kr/slow-4")
