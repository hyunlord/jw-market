from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
import json
import logging

import pytest
import requests

from jw_chat_agent_poc.tools.external.client import ExternalApiClient, ExternalCall
from jw_chat_agent_poc.tools.external.hira_reimbursement import (
    CacheStatus,
    ReimbursementCacheResult,
    ReimbursementCriterion,
    ReimbursementLookupService,
)
from jw_chat_agent_poc.tools.external.mcp_client import McpClientError, McpToolResult
from jw_chat_agent_poc.tools.external.telemetry import (
    emit_external_call_telemetry,
    emit_external_source_telemetry,
    evaluator_exception_counts,
    reset_evaluator_exception_counts,
)


NOW = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)


def _telemetry_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    prefix = "external_source_telemetry "
    return [
        json.loads(record.getMessage()[len(prefix) :])
        for record in caplog.records
        if record.getMessage().startswith(prefix)
    ]


class _WebResponse:
    def __init__(self, *, status_code: int = 200, results: list[dict[str, str]] | None = None) -> None:
        self.status_code = status_code
        self._results = results or []

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            raise requests.HTTPError(
                f"{self.status_code} Server Error",
                response=response,
            )

    def json(self) -> dict[str, object]:
        return {"results": self._results}


@pytest.mark.parametrize(
    ("outcome", "failure_class", "factory"),
    [
        (
            "timeout",
            "timeout",
            lambda: requests.Timeout("deadline exceeded"),
        ),
        (
            "5xx",
            "5xx",
            lambda: _WebResponse(status_code=503),
        ),
        (
            "0_results",
            "0_results",
            lambda: _WebResponse(results=[]),
        ),
    ],
)
def test_web_failure_telemetry_classifies_without_changing_call(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    outcome: str,
    failure_class: str,
    factory,
) -> None:
    expected_calls: list[object] = []

    def fake_post(*_args, **_kwargs):
        value = factory()
        expected_calls.append(value)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "telemetry-test-value")
    monkeypatch.setattr(
        "jw_chat_agent_poc.tools.external.client.requests.post",
        fake_post,
    )
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    call = ExternalApiClient(mode="live").web_search("아일리아 최신 근거")
    before = asdict(call)
    events = _telemetry_events(caplog)

    assert len(expected_calls) == 1, outcome
    assert asdict(call) == before
    assert len(events) == 1
    assert events[0] == {
        "cache_status": "not_applicable",
        "domain_source": "web",
        "failure_class": failure_class,
        "fallback_eligible": True,
        "primary_provider": "tavily",
        "question_fingerprint": events[0]["question_fingerprint"],
    }
    assert len(str(events[0]["question_fingerprint"])) == 64
    assert "아일리아 최신 근거" not in caplog.text
    assert "telemetry-test-value" not in caplog.text


def test_unsupported_web_provider_is_not_fallback_eligible(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "unsupported-provider")
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    call = ExternalApiClient(mode="live").web_search("웹검색해줘")
    events = _telemetry_events(caplog)

    assert call.status == "unsupported"
    assert len(events) == 1
    assert events[0]["failure_class"] == "none"
    assert events[0]["fallback_eligible"] is False


def test_security_block_forces_fallback_ineligible(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    emit_external_source_telemetry(
        primary_provider="tavily",
        question="blocked request",
        failure_class="0_results",
        domain_source="web",
        cache_status="not_applicable",
        fallback_blocked=True,
    )

    events = _telemetry_events(caplog)
    assert len(events) == 1
    assert events[0]["failure_class"] == "0_results"
    assert events[0]["fallback_eligible"] is False


class _McpClient:
    result = McpToolResult(
        content_text="",
        raw_result={
            "structuredContent": {
                "result": [
                    {
                        "ITEM_SEQ": "200500287",
                        "ITEM_NAME": "리바로정1밀리그램",
                    }
                ]
            }
        },
    )

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def call_tool(self, *_args, **_kwargs) -> McpToolResult:
        return self.result

    def call_tool_checked(self, *_args, **_kwargs) -> McpToolResult:
        return self.result


class _FailingMcpClient:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def call_tool(self, *_args, **_kwargs) -> McpToolResult:
        raise McpClientError("MCP execution deadline exceeded")

    def call_tool_checked(self, *_args, **_kwargs) -> McpToolResult:
        raise McpClientError("MCP execution deadline exceeded")


@pytest.mark.parametrize(
    ("invoke", "provider"),
    [
        (lambda client: client.clinicaltrials_v2_search("뇌경색"), "clinicaltrials_mcp"),
        (lambda client: client.openfda_label_search("AFLIBERCEPT"), "openfda_mcp"),
        (lambda client: client.mfds_permission_search("아일리아"), "nedrug_mcp"),
    ],
)
def test_domain_mcp_success_emits_distinct_source(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    invoke,
    provider: str,
) -> None:
    monkeypatch.setattr(
        "jw_chat_agent_poc.tools.external.client.McpJsonClient",
        _McpClient,
    )
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    call = invoke(ExternalApiClient(mode="live"))
    events = _telemetry_events(caplog)

    assert call.status in {"live", "no_data"}
    assert len(events) == 1
    assert events[0]["primary_provider"] == provider
    assert events[0]["domain_source"] == "MCP"
    assert events[0]["cache_status"] == "not_applicable"


@pytest.mark.parametrize(
    ("invoke", "provider"),
    [
        (lambda client: client.clinicaltrials_v2_search("뇌경색"), "clinicaltrials_mcp"),
        (lambda client: client.openfda_label_search("AFLIBERCEPT"), "openfda_mcp"),
        (lambda client: client.mfds_permission_search("아일리아"), "nedrug_mcp"),
    ],
)
def test_domain_mcp_failure_emits_timeout_without_merging_into_web(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    invoke,
    provider: str,
) -> None:
    monkeypatch.setattr(
        "jw_chat_agent_poc.tools.external.client.McpJsonClient",
        _FailingMcpClient,
    )
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    call = invoke(ExternalApiClient(mode="live"))
    events = _telemetry_events(caplog)

    assert call.status == "error"
    assert len(events) == 1
    assert events[0]["primary_provider"] == provider
    assert events[0]["domain_source"] == "MCP"
    assert events[0]["failure_class"] == "timeout"
    assert events[0]["fallback_eligible"] is True


def _criterion(*, collected_at: datetime) -> ReimbursementCriterion:
    return ReimbursementCriterion(
        brand_name="아일리아",
        title="급여기준",
        raw_text="확인된 급여기준",
        source_date="2026-07-30",
        collected_at=collected_at,
        notice_number=None,
        source_url="https://www.hira.or.kr/example",
    )


class _HiraStore:
    def __init__(self, result: ReimbursementCacheResult) -> None:
        self.result = result

    def get_reimbursement_criteria(self, _brand_name: str) -> ReimbursementCacheResult:
        return self.result

    def put_reimbursement_criteria(self, _criterion: ReimbursementCriterion) -> bool:
        return True


class _HiraRealtime:
    def __init__(self, result: ReimbursementCriterion | None) -> None:
        self.result = result

    def fetch(self, _brand_name: str) -> ReimbursementCriterion | None:
        return self.result


@pytest.mark.parametrize(
    ("cached", "realtime", "expected_status"),
    [
        (
            ReimbursementCacheResult(
                CacheStatus.FRESH,
                _criterion(collected_at=NOW - timedelta(hours=1)),
                "2026-07-30",
            ),
            None,
            "hit",
        ),
        (
            ReimbursementCacheResult(
                CacheStatus.STALE,
                _criterion(collected_at=NOW - timedelta(days=3)),
                "2026-07-30",
            ),
            None,
            "stale",
        ),
        (
            ReimbursementCacheResult(CacheStatus.NOT_FOUND, None, None),
            _criterion(collected_at=NOW),
            "miss",
        ),
    ],
)
def test_hira_cache_telemetry_distinguishes_hit_stale_and_miss(
    caplog: pytest.LogCaptureFixture,
    cached: ReimbursementCacheResult,
    realtime: ReimbursementCriterion | None,
    expected_status: str,
) -> None:
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    result = ReimbursementLookupService(
        store=_HiraStore(cached),
        realtime=_HiraRealtime(realtime),
        refresh_trigger=lambda _brand: None,
        now=lambda: NOW,
    ).lookup("아일리아")
    events = _telemetry_events(caplog)

    assert result.ok is True
    assert events[0]["primary_provider"] == "hira_reimbursement"
    assert events[0]["domain_source"] == "cache"
    assert events[0]["cache_status"] == expected_status
    assert "아일리아" not in caplog.text


def test_fifty_case_golden_set_has_complete_classification_and_byte_parity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    categories = (
        ("hira_reimbursement", "cache", "hit", "live"),
        ("nedrug_mcp", "MCP", "not_applicable", "live"),
        ("clinicaltrials_mcp", "MCP", "not_applicable", "no_data"),
        ("tavily", "web", "not_applicable", "live"),
        ("tavily", "web", "not_applicable", "no_data"),
    )
    calls: list[ExternalCall] = []
    before: list[dict[str, object]] = []
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    for category_index, (provider, domain_source, cache_status, status) in enumerate(categories):
        for case_index in range(10):
            items = [{"title": f"item-{case_index}"}] if status == "live" else []
            call = ExternalCall(
                tool=f"golden-{category_index}",
                source=provider,
                status=status,
                summary_text=f"golden-result-{case_index}",
                render_data={"items": items},
            )
            calls.append(call)
            before.append(asdict(call))
            emit_external_call_telemetry(
                primary_provider=provider,
                question=f"golden-{category_index}-{case_index}",
                domain_source=domain_source,
                cache_status=cache_status,
                call=call,
            )

    events = _telemetry_events(caplog)
    assert len(events) == 50
    assert all(
        {
            "primary_provider",
            "failure_class",
            "domain_source",
            "cache_status",
            "fallback_eligible",
            "question_fingerprint",
        }
        == set(event)
        for event in events
    )
    assert [asdict(call) for call in calls] == before


def test_telemetry_evaluator_exception_is_counted_and_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    call = ExternalCall(
        tool="web_search",
        source="web_search",
        status="live",
        summary_text="unchanged",
        render_data={"items": [{"title": "evidence"}]},
    )
    before = asdict(call)
    reset_evaluator_exception_counts()
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    def fail_evaluator(_call) -> str:
        raise ValueError("injected evaluator failure")

    monkeypatch.setattr(
        "jw_chat_agent_poc.tools.external.telemetry.failure_class_from_call",
        fail_evaluator,
    )
    emit_external_call_telemetry(
        primary_provider="tavily",
        question="secret-free question",
        domain_source="web",
        cache_status="not_applicable",
        call=call,
    )

    assert evaluator_exception_counts() == {"ValueError": 1}
    assert "external_source_telemetry_evaluator_failed" in caplog.text
    assert "error_type=ValueError count=1" in caplog.text
    assert asdict(call) == before
