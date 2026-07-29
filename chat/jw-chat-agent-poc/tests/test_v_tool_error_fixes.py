from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from jw_chat_agent_poc.agent_loop.external_tools import search_news_call
from jw_chat_agent_poc.agent_loop.news_query import normalize_news_query
from jw_chat_agent_poc.orchestrator.agent import (
    _is_external_tool_agent_candidate,
    _query_failed_metric_call,
)
from jw_chat_agent_poc.router import BQRouter


class _RecordingNews:
    def related_news(
        self,
        brand: str,
        *,
        filter_entries: tuple[tuple[str, str], ...],
    ) -> dict[str, Any]:
        return {
            "render_data": {
                "brand": brand,
                "items": tuple(range(250)),
                "received_filters": filter_entries,
            }
        }


@pytest.mark.parametrize(
    "question",
    (
        "리바로 관련 최근 이슈 뭐 있어?",
        "리바로 이슈 뭐야?",
        "리바로 최근 소식 있나?",
        "리바로 뉴스 알려줘",
        "리바로 관련 기사",
    ),
)
def test_generic_news_question_does_not_become_corpus_text_filter(
    question: str,
) -> None:
    query = normalize_news_query(question, brand="리바로")
    call = search_news_call(_RecordingNews(), "리바로", query)  # type: ignore[arg-type]

    assert query == ""
    assert call["render_data"]["filter_entries"] == ()
    assert len(call["render_data"]["items"]) == 250


@pytest.mark.parametrize(
    ("question", "expected"),
    (
        ("리바로 특허 관련 뉴스", "특허"),
        ("리바로 약가 인하 기사", "약가 인하"),
    ),
)
def test_meaningful_news_terms_remain_corpus_text_filters(
    question: str,
    expected: str,
) -> None:
    query = normalize_news_query(question, brand="리바로")
    call = search_news_call(_RecordingNews(), "리바로", query)  # type: ignore[arg-type]

    assert query == expected
    assert call["render_data"]["filter_entries"] == (("text_contains", expected),)


@pytest.mark.parametrize(
    "question",
    (
        "리바로 IQVIA랑 UBIST 수치가 다른데 왜?",
        "리바로 UBIST와 IQVIA를 비교해줘",
        "리바로 IQVIA versus UBIST 차이",
    ),
)
def test_iqvia_ubist_comparison_routes_to_internal_metrics(
    question: str,
) -> None:
    routes = BQRouter().route(question)

    assert [(route.bq, route.sources) for route in routes] == [
        ("Q1", ("metrics",)),
    ]
    assert not _is_external_tool_agent_candidate(routes, [], question=question)


def test_single_source_mention_is_not_promoted_to_source_comparison() -> None:
    routes = BQRouter().route("리바로 IQVIA가 왜 달라?")

    assert [(route.bq, route.sources) for route in routes] == [
        ("UNKNOWN", ("none",)),
    ]


@pytest.mark.parametrize(
    ("error_message", "expected"),
    (
        ("market is unresolved for 아일리아", "market_unresolved"),
        ("brand belongs to multiple markets: ml_001,ml_002", "market_ambiguous"),
        ("mart brand not found: brand=아일리아", "record_absent"),
        ("sales is unavailable for source=iqvia_nsa", "source_absent"),
        ("mart periods missing: market=ml_001", "period_absent"),
        ("mart market period value missing: period=2026-05", "value_absent"),
        ("unexpected query-layer failure", "unknown"),
    ),
)
def test_query_failed_call_exposes_allowlisted_reason_code(
    error_message: str,
    expected: str,
) -> None:
    call = _query_failed_metric_call(
        "아일리아",
        "sales",
        (),
        LookupError(error_message),
    )

    assert call["render_data"]["reason_code"] == expected
    assert error_message not in json.dumps(call, ensure_ascii=False)


def test_query_failed_logs_masked_message_without_public_leak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_message = (
        "mart brand not found password=super-secret "
        "dsn=mysql://db-user:db-pass@db.internal:3306/cache"
    )

    with caplog.at_level(logging.WARNING):
        call = _query_failed_metric_call(
            "아일리아",
            "sales",
            (),
            LookupError(raw_message),
        )

    public_payload = json.dumps(call, ensure_ascii=False)
    assert raw_message not in public_payload
    assert "super-secret" not in caplog.text
    assert "db-pass" not in caplog.text
    assert "password=[REDACTED]" in caplog.text
    assert "[REDACTED_CONNECTION_STRING]" in caplog.text
    assert "mart brand not found" in caplog.text


@pytest.mark.parametrize(
    "question",
    (
        "리바로 최근 3개년 실적",
        "리바로 얼마나 팔렸어",
        "리바로 최근 실적 알려줘",
        "카나브패밀리 실적",
    ),
)
def test_sales_wording_variants_route_to_internal_metrics(question: str) -> None:
    # The BQ map recognised 매출/판매 but not 실적/팔렸, so these utterances fell to the
    # UNKNOWN/none fallback and were picked up as external-tool candidates, which answered
    # a false no-data from an ingredient lookup. They belong on the internal metric route.
    routes = BQRouter().route(question)

    assert [(route.bq, route.sources) for route in routes] == [
        ("Q1", ("metrics",)),
    ]
    assert not _is_external_tool_agent_candidate(routes, [], question=question)


@pytest.mark.parametrize(
    "question",
    (
        "리바로 성분 알려줘",
        "리바로 급여기준 알려줘",
        "리바로 허가정보 알려줘",
    ),
)
def test_external_wording_still_reaches_the_tool_pack(question: str) -> None:
    # Guards the other direction: widening the sales vocabulary must not pull
    # external-evidence questions onto the metric route.
    routes = BQRouter().route(question)

    assert [(route.bq, route.sources) for route in routes] == [
        ("UNKNOWN", ("none",)),
    ]
    assert _is_external_tool_agent_candidate(routes, [], question=question)
