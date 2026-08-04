from __future__ import annotations

import pytest

from jw_chat_agent_poc.tools.general_view_backend import AtcCandidate, GeneralMarket
from jw_chat_agent_poc.tools.general_view_mart import GeneralViewMartBackend, GeneralViewMartLoadError
from v3_market_scope_fakes import FakeGeneralBackend


class FailingGeneralMartReader:
    def read(self, atc4: str, brand: str | None, source: str, measure: str) -> object:
        raise GeneralViewMartLoadError("fixture direct mart miss", reason="zero_rows")


class TrackingGeneralFallback:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def candidates(self, brand: str, source: str) -> tuple[AtcCandidate, ...]:
        self.calls.append(("candidates", brand, source))
        return (AtcCandidate("S01P0", "fallback"),)

    def market(
        self,
        atc4: str,
        brand: str | None,
        source: str,
        measure: str,
    ) -> GeneralMarket:
        self.calls.append(("market", atc4, brand, source, measure))
        return FakeGeneralBackend([]).market(atc4, brand, source, measure)


def test_mart_only_mode_never_calls_backend_fallback() -> None:
    fallback = TrackingGeneralFallback()
    backend = GeneralViewMartBackend(
        FailingGeneralMartReader(),
        fallback,
        allow_fallback=False,
    )

    with pytest.raises(GeneralViewMartLoadError, match="fixture direct mart miss"):
        backend.market("S01P0", "아일리아", "iqvia", "sales")
    with pytest.raises(GeneralViewMartLoadError, match="disabled"):
        backend.candidates("아일리아", "iqvia")

    assert fallback.calls == []
