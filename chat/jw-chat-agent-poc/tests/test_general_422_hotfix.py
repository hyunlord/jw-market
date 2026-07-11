from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from jw_chat_agent_poc.tools.general_view_backend import (
    GeneralViewBackend,
    GeneralViewBackendError,
    focus_brand_key,
    parse_general_market_response,
)


ALLOWED_TOP_LEVEL = {"view", "source", "measure", "filters", "options"}
ALLOWED_FILTERS = {"focus_brand_key", "atc4", "analysis_level"}
ALLOWED_OPTIONS = {"period_range"}


def _slim_backend_validate(payload: dict[str, Any]) -> list[tuple[str, ...]]:
    """Replicates the slimmed backend's extra_forbidden validation (v0.9.148 schema)."""

    rejected: list[tuple[str, ...]] = []
    rejected.extend((key,) for key in payload if key not in ALLOWED_TOP_LEVEL)
    for key in payload.get("filters") or {}:
        if key not in ALLOWED_FILTERS:
            rejected.append(("filters", key))
    for key in payload.get("options") or {}:
        if key not in ALLOWED_OPTIONS:
            rejected.append(("options", key))
    return rejected


def _market_payload(*, atc4: str = "C10A1", rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "status": "SUCCESS",
        "result": {
            "unit_label": "KRW",
            "market_meta": {
                "market_definition_label": f"동적 시장: ATC4 {atc4}",
                "filters": {"view": "general", "atc4": [atc4], "source": "ubist", "measure": "sales"},
            },
            "data": {
                "kpi": {"market_size_recent": 87_020_000_000.0},
                "sources_data": {"market_size_series": [{"period": "2026-05", "value": 87_020_000_000.0}]},
                "ei_ms_matrix": {"data": rows or []},
            },
        },
    }


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeSlimSession:
    """Accepts only the slimmed request schema; records every outgoing payload."""

    def __init__(self, response_payload: dict[str, Any]) -> None:
        self.requests: list[dict[str, Any]] = []
        self._response_payload = response_payload

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        payload = kwargs.get("json") or {}
        self.requests.append(json.loads(json.dumps(payload)))
        rejected = _slim_backend_validate(payload)
        if rejected:
            return _FakeResponse(422, {"detail": [{"type": "extra_forbidden", "loc": list(loc)} for loc in rejected]})
        return _FakeResponse(200, self._response_payload)


FOCUS_PREPENDED_ROWS = [
    {"brand": "휴텍스 아토르바스타틴", "rank": 56, "value_recent": 335_909_399.73, "share_pct": 0.386},
    {"brand": "리피토", "rank": 1, "value_recent": 13_108_840_203.03, "share_pct": 15.0643},
    {"brand": "리바로", "rank": 2, "value_recent": 8_038_598_793.61, "share_pct": 9.2377},
    {"brand": "크레스토", "rank": 3, "value_recent": 6_497_346_945.75, "share_pct": 7.4665},
    {"brand": "리피로우", "rank": 4, "value_recent": 3_104_657_265.9, "share_pct": 3.5677},
    {"brand": "아토르바", "rank": 5, "value_recent": 2_294_712_912.18, "share_pct": 2.637},
]


def test_legacy_payload_with_top_n_is_rejected_by_slim_schema() -> None:
    legacy = {
        "filters": {"atc4": ["C10A1"], "focus_brand_key": "리바로"},
        "source": "ubist",
        "measure": "sales",
        "options": {"top_n": 100},
    }
    assert _slim_backend_validate(legacy) == [("options", "top_n")]


def test_adapter_payload_contains_only_allowed_fields_and_succeeds() -> None:
    session = _FakeSlimSession(_market_payload(rows=FOCUS_PREPENDED_ROWS))
    backend = GeneralViewBackend(base_url="http://backend", session=session)  # type: ignore[arg-type]

    market = backend.market("C10A1", "휴텍스 아토르바스타틴", "ubist", "sales")

    sent = session.requests[-1]
    assert _slim_backend_validate(sent) == []
    assert set(sent) == {"view", "filters", "source", "measure"}
    assert sent["view"] == "general"
    assert sent["filters"] == {"atc4": ["C10A1"], "focus_brand_key": "휴텍스아토르바스타틴"}
    assert "options" not in sent
    assert market.brand == "휴텍스 아토르바스타틴"
    assert market.brand_rank == 56


def test_adapter_payload_without_brand_omits_focus_key() -> None:
    session = _FakeSlimSession(_market_payload(rows=FOCUS_PREPENDED_ROWS[1:]))
    backend = GeneralViewBackend(base_url="http://backend", session=session)  # type: ignore[arg-type]

    backend.market("C10A1", None, "ubist", "sales")

    sent = session.requests[-1]
    assert sent["filters"] == {"atc4": ["C10A1"]}
    assert "options" not in sent


def test_top_n_slice_equivalence_with_focus_prepended_matrix() -> None:
    market = parse_general_market_response(
        _market_payload(rows=FOCUS_PREPENDED_ROWS),
        requested_atc4="C10A1",
        requested_source="ubist",
        requested_measure="sales",
        requested_brand="휴텍스 아토르바스타틴",
    )

    assert [brand.brand for brand in market.top_brands] == ["리피토", "리바로", "크레스토", "리피로우", "아토르바"]
    assert len(market.top_brands) == 5
    assert market.brand == "휴텍스 아토르바스타틴"
    assert market.brand_value == 335_909_399.73
    assert market.brand_share_pct == 0.386
    assert market.brand_rank == 56


def test_focus_brand_key_matches_backend_convention() -> None:
    assert focus_brand_key("리바로") == "리바로"
    assert focus_brand_key("휴텍스 아토르바스타틴") == "휴텍스아토르바스타틴"
    assert focus_brand_key("휴마로그 100I.U/mL") == "휴마로그100iuml"
    assert focus_brand_key("애피드라 솔로스타 /") == "애피드라솔로스타"
    assert focus_brand_key("노보래피드 100단위/mL") == "노보래피드100단위ml"


def test_fail_closed_echo_check_is_retained() -> None:
    payload = _market_payload(rows=FOCUS_PREPENDED_ROWS)
    payload["result"]["market_meta"]["filters"]["atc4"] = ["A10B1"]
    with pytest.raises(GeneralViewBackendError):
        parse_general_market_response(
            payload,
            requested_atc4="C10A1",
            requested_source="ubist",
            requested_measure="sales",
        )
