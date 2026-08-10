from __future__ import annotations

from jw_chat_agent_poc.tools.general_view_backend import AtcCandidate
from jw_chat_agent_poc.tools.general_view_mart import (
    GeneralMartRows,
    GeneralViewMartBackend,
    MariaDbGeneralMartReader,
)


class FakeGeneralMartReader:
    def read(self, atc4: str, brand: str | None, source: str, measure: str) -> GeneralMartRows:
        return GeneralMartRows(
            atc4_code=atc4,
            atc4_description="GLP-1",
            source=source,
            measure=measure,
            unit="KRW",
            market_size_series={"2025-Q4": 200.0, "2026-Q1": 300.0},
            brand_ranking={
                "2025-Q4": [{"brand": "마운자로", "rank": 1, "raw_value": 190.0, "ms": 95.0}],
                "2026-Q1": [
                    {"brand": "마운자로", "rank": 1, "raw_value": 270.0, "ms": 90.0},
                    {"brand": "오젠픽", "rank": 2, "raw_value": 30.0, "ms": 10.0},
                ],
            },
            brand_name=brand,
            brand_metric_history={"2025-Q4": {"raw_value": 190.0, "ms": 95.0, "rank": 1}, "2026-Q1": {"raw_value": 270.0, "ms": 90.0, "rank": 1}},
            brand_metric_histories={
                "마운자로": {"2026-Q1": {"raw_value": 270.0, "ms": 90.0, "rank": 1, "yoy": 42.1}},
                "오젠픽": {"2026-Q1": {"raw_value": 30.0, "ms": 10.0, "rank": 2, "yoy": 7.5}},
            },
            hhi_series={"2025-Q4": 9050.0, "2026-Q1": 8200.0},
            member_population=("마운자로", "오젠픽", "제로브랜드"),
        )


class CandidateOnlyBackend:
    def candidates(self, brand: str, source: str) -> tuple[AtcCandidate, ...]:
        return (AtcCandidate("A10S0", "GLP-1"),)


def test_reader_uses_general_mart_schema_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_CACHE_DB_NAME", "jw_mart")
    monkeypatch.setenv("CHAT_GENERAL_MART_SCHEMA", "jw_mart_d2_stage_20260630_r2")

    reader = MariaDbGeneralMartReader()

    assert reader.database == "jw_mart_d2_stage_20260630_r2"


def test_mart_backend_uses_latest_period_for_market_brand_and_top_five() -> None:
    backend = GeneralViewMartBackend(FakeGeneralMartReader(), CandidateOnlyBackend())

    market = backend.market("A10S0", "마운자로", "iqvia", "sales")

    assert market.period == "2026-Q1"
    assert market.market_size == 300.0
    assert market.brand_value == 270.0
    assert market.brand_share_pct == 90.0
    assert market.brand_rank == 1
    assert market.hhi_recent == 8200.0
    assert [row.brand for row in market.top_brands] == ["마운자로", "오젠픽"]
    assert [row.growth_pct for row in market.top_brands] == [42.1, 7.5]
    assert market.top_brands[0].growth_start_period == "2025-Q1"
    assert market.top_brands[0].growth_end_period == "2026-Q1"


def test_mart_backend_keeps_candidate_fallback_contract() -> None:
    backend = GeneralViewMartBackend(FakeGeneralMartReader(), CandidateOnlyBackend())

    assert backend.candidates("마운자로", "iqvia") == (AtcCandidate("A10S0", "GLP-1"),)


def test_mart_backend_sorts_full_member_population_by_rank() -> None:
    class UnsortedReader(FakeGeneralMartReader):
        def read(self, atc4: str, brand: str | None, source: str, measure: str) -> GeneralMartRows:
            rows = super().read(atc4, brand, source, measure)
            rows.brand_ranking["2026-Q1"].reverse()
            return rows

    backend = GeneralViewMartBackend(UnsortedReader(), CandidateOnlyBackend())

    market = backend.market("A10S0", "마운자로", "iqvia", "sales")

    assert [row.brand for row in market.member_brands] == ["마운자로", "오젠픽"]


def test_mart_backend_separates_full_active_and_display_member_populations() -> None:
    backend = GeneralViewMartBackend(FakeGeneralMartReader(), CandidateOnlyBackend())

    market = backend.market("A10S0", "마운자로", "iqvia", "sales")

    assert market.member_population == ("마운자로", "오젠픽", "제로브랜드")
    assert [row.brand for row in market.active_members] == ["마운자로", "오젠픽"]
    assert [row.brand for row in market.display_members] == ["마운자로", "오젠픽"]


def test_mart_backend_uses_latest_period_shared_by_market_size_hhi_and_ranking() -> None:
    class MisalignedLatestReader(FakeGeneralMartReader):
        def read(self, atc4: str, brand: str | None, source: str, measure: str) -> GeneralMartRows:
            rows = super().read(atc4, brand, source, measure)
            rows.hhi_series["2026-Q2"] = 8100.0
            return rows

    backend = GeneralViewMartBackend(MisalignedLatestReader(), CandidateOnlyBackend())

    market = backend.market("A10S0", "마운자로", "iqvia", "sales")

    assert market.period == "2026-Q1"
    assert market.market_size == 300.0
    assert market.hhi_recent == 8200.0
