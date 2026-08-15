from __future__ import annotations

from collections import Counter
from datetime import date
from types import SimpleNamespace

import pytest

from jw_chat_agent_poc.service.v4 import adapters as v4_adapters
from jw_chat_agent_poc.tools.external.client import ExternalCall


def _build_hira_adapter(
    monkeypatch: pytest.MonkeyPatch,
    rows_by_year: dict[str, list[dict[str, str]]],
) -> tuple[object, list[str]]:
    requested_years: list[str] = []

    class Resolver:
        def resolve(self, _query: str, *, allow_default: bool) -> object:
            assert allow_default is False
            raise LookupError

    class External:
        timeout_s = 12

        def hira_disease_name_code(self, code: str) -> ExternalCall:
            return ExternalCall(
                tool="hira_disease_name_code",
                source="HIRA",
                status="live",
                summary_text=code,
                render_data={"items": [{"sickCd": code}]},
            )

        def hira_disease_hospitalization_outpatient_stats(
            self,
            code: str,
            year: str,
        ) -> ExternalCall:
            requested_years.append(year)
            items = rows_by_year.get(year, [])
            return ExternalCall(
                tool="hira_disease_hospitalization_outpatient_stats",
                source="HIRA",
                status="live" if items else "no_data",
                summary_text=f"{year}: {len(items)} rows",
                render_data={
                    "request": {"sickCd": code, "year": year},
                    "items": items,
                },
            )

    from jw_chat_agent_poc.agent_loop import factory
    from jw_chat_agent_poc.service import general_view_routing

    monkeypatch.setattr(v4_adapters, "current_kst_date", lambda: date(2026, 8, 14))
    monkeypatch.setattr(
        factory,
        "build_chat_agent_dependencies",
        lambda **_kwargs: SimpleNamespace(
            external=External(),
            resolver=Resolver(),
            query_layer=None,
        ),
    )
    monkeypatch.setattr(
        general_view_routing.GeneralViewService,
        "from_env",
        lambda _resolver: SimpleNamespace(),
    )
    return v4_adapters.build_source_adapters()["hira"], requested_years


def _stat_calls(result: object) -> list[dict[str, object]]:
    return [
        call
        for call in result.payload["calls"]
        if call["tool"] == "hira_disease_hospitalization_outpatient_stats"
    ]


def test_short_korean_year_and_range_are_resolved_without_fallback() -> None:
    assert v4_adapters._requested_hira_years(
        "22년도 상병코드 D69의 입원 환자수는?",
        current_year=2026,
    ) == ("2022",)
    assert v4_adapters._requested_hira_years(
        "22년부터 24년 상병코드 D693 연도별 환자수 추이를 비교해줘",
        current_year=2026,
    ) == ("2022", "2023", "2024")
    assert v4_adapters._requested_hira_years("올해 D693 환자수", current_year=2026) == (
        "2026",
    )
    assert v4_adapters._requested_hira_years("작년 D693 환자수", current_year=2026) == (
        "2025",
    )
    assert v4_adapters._requested_hira_years("2027년 D693 환자수", current_year=2026) == ()


def test_no_year_discovers_latest_available_year_and_preserves_empty_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, requested = _build_hira_adapter(
        monkeypatch,
        {
            "2025": [{"sickCd": "D693", "year": "2025", "ptntCnt": "10"}],
            "2024": [{"sickCd": "D693", "year": "2024", "ptntCnt": "9"}],
        },
    )

    result = adapter("D693의 환자수")

    assert Counter(requested) == Counter({"2026": 1, "2025": 1, "2024": 1})
    assert [call["render_data"]["requested_year"] for call in _stat_calls(result)] == ["2025"]
    periods = result.payload["period_coverage"]["periods"]
    assert [(item["period"], item["status"]) for item in periods] == [
        ("2026", "no_data"),
        ("2025", "ok"),
        ("2024", "ok"),
    ]
    assert periods[0]["availability_status"] == "EMPTY"
    assert periods[0]["received_count"] == 0
    assert result.notice == "최신 제공 연도 2025년 데이터를 사용합니다."


def test_latest_year_expression_uses_discovery_not_a_literal_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, requested = _build_hira_adapter(
        monkeypatch,
        {"2025": [{"sickCd": "D693", "year": "2025", "vstDdcnt": "12"}]},
    )

    result = adapter("최근년도 상병코드 d693의 내원일수를 알려줘")

    assert Counter(requested) == Counter({"2026": 1, "2025": 1, "2024": 1})
    assert [call["render_data"]["requested_year"] for call in _stat_calls(result)] == ["2025"]


def test_recent_range_excludes_empty_year_from_narrative_but_reports_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, requested = _build_hira_adapter(
        monkeypatch,
        {
            str(year): [{"sickCd": "D693", "year": str(year), "ptntCnt": str(year)}]
            for year in range(2022, 2026)
        },
    )

    result = adapter("질병코드 4단계 d693의 최근 5개년 환자 수")

    assert Counter(requested) == Counter({str(year): 1 for year in range(2022, 2027)})
    assert [
        call["render_data"]["requested_year"] for call in _stat_calls(result)
    ] == ["2022", "2023", "2024", "2025"]
    last_period = result.payload["period_coverage"]["periods"][-1]
    assert (last_period["period"], last_period["status"]) == ("2026", "no_data")
    assert last_period["availability_status"] == "EMPTY"
    assert last_period["received_count"] == 0
    assert result.notice == "요청 2022~2026년 중 2022~2025년 제공 · 2026년은 아직 미제공입니다."


def test_parser_none_injection_still_uses_dynamic_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, requested = _build_hira_adapter(
        monkeypatch,
        {"2025": [{"sickCd": "D693", "year": "2025", "ptntCnt": "10"}]},
    )
    monkeypatch.setattr(v4_adapters, "_requested_hira_years", lambda *_args, **_kwargs: None)

    result = adapter("D693의 환자수")

    assert Counter(requested) == Counter({"2026": 1, "2025": 1, "2024": 1})
    assert [call["render_data"]["requested_year"] for call in _stat_calls(result)] == ["2025"]
    assert "2024" not in v4_adapters.build_source_adapters.__code__.co_consts


def test_latest_discovery_reports_when_every_probed_year_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, requested = _build_hira_adapter(monkeypatch, {})

    result = adapter("D693의 최신 환자수")

    assert Counter(requested) == Counter({"2026": 1, "2025": 1, "2024": 1})
    assert _stat_calls(result) == []
    assert result.notice == "탐색한 2024~2026년은 아직 미제공입니다."


def test_future_explicit_year_is_not_replaced_with_latest_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, requested = _build_hira_adapter(
        monkeypatch,
        {"2025": [{"sickCd": "D693", "year": "2025", "ptntCnt": "10"}]},
    )

    result = adapter("2027년 D693 환자수")

    assert requested == []
    assert _stat_calls(result) == []
    assert result.notice == "요청 연도가 현재 연도보다 미래여서 조회하지 않았습니다."
