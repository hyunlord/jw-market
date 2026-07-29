"""F-2: the market-id serving path must carry the catalog's public market label.

ANCH-3 established that `answer_market_id` / `market_scope_by_id` are the only
market path with no display-name argument, so both hardcode "해당 전략 시장".
`catalog_ml_market.name` is populated for every strategic market and the resolver
already loads it, so the label only has to be plumbed through.

Two contracts in this codebase look alike, and confusing them broke deploy 28:

    CatalogMembershipSource   (catalog_membership.py:26)  -> load()
    BrandMembershipReader     (brand_resolver.py:60)      -> brand_memberships()

`MarketScopeResolver(membership_reader=...)` takes the *reader*, and production
supplies `TtlCatalogMembershipReader`, a wrapper around a source that has
`brand_memberships()` and **no** `load()`. Deploy 28 called `load()` and its unit
tests passed only because they injected a bare source. Every fixture here builds
the reader the way production does, and the layer-3 tests at the bottom pin the
contract in both directions so the mistake cannot come back green.

The literal stays as the last-resort fallback: catalog_membership COALESCEs the
name to the ml_id upstream, so a market with no public name must not put an
internal identifier on screen.
"""

from __future__ import annotations

import pytest

from jw_chat_agent_poc.resolver.catalog_membership import (
    StaticCatalogMembershipReader,
    TtlCatalogMembershipReader,
)
from jw_chat_agent_poc.tools.metrics.cache_live import StaticMetricsCacheReader
from jw_chat_agent_poc.tools.metrics.market_scope import MarketScopeResolver
from jw_chat_agent_poc.tools.query_layer import (
    MartRecord,
    StaticStrategicMartReader,
    StrategicQueryLayer,
)

FALLBACK_LABEL = "해당 전략 시장"
PUBLIC_LABEL = "리바로 리바로젯"


def _records() -> tuple[MartRecord, ...]:
    return tuple(
        MartRecord(
            ml_id="ml_006",
            brand_name=f"브랜드{rank}",
            source="ubist",
            measure="sales",
            metric_history={"2026-05": {"raw_value": float(100 - rank)}},
            channel_data={},
            specialty_data={},
            dimension_data={},
            by_dimension={},
        )
        for rank in range(1, 6)
    )


def _layer() -> StrategicQueryLayer:
    return StrategicQueryLayer(reader=StaticStrategicMartReader(_records()))


def _rows(market_name: str) -> tuple[dict[str, str], ...]:
    return (
        {
            "brand": "브랜드1",
            "brand_alias": "",
            "market_id": "ml_006",
            "market_name": market_name,
            "support_source": "strategic_mart",
        },
    )


def _operational_reader(market_name: str | None) -> TtlCatalogMembershipReader:
    """A reader shaped exactly like production: the TTL wrapper around a source."""
    rows = () if market_name is None else _rows(market_name)
    return TtlCatalogMembershipReader(
        StaticCatalogMembershipReader(rows=rows), ttl_seconds=300
    )


def _resolver(market_name: str | None) -> MarketScopeResolver:
    return MarketScopeResolver(
        cache_reader=StaticMetricsCacheReader(cache_brands=[], market_status={}),
        query_layer=_layer(),
        membership_reader=_operational_reader(market_name),
    )


# --- layer 1/2: the label reaches the answer -------------------------------


def test_market_scope_by_id_uses_supplied_display_name() -> None:
    call = _layer().market_scope_by_id("ml_006", "2026-05", market_display_name=PUBLIC_LABEL)

    assert call["render_data"]["market_name"] == PUBLIC_LABEL


def test_market_scope_by_id_keeps_fallback_literal_without_display_name() -> None:
    call = _layer().market_scope_by_id("ml_006", "2026-05")

    assert call["render_data"]["market_name"] == FALLBACK_LABEL


def test_market_scope_by_id_keeps_fallback_literal_for_blank_display_name() -> None:
    call = _layer().market_scope_by_id("ml_006", "2026-05", market_display_name="")

    assert call["render_data"]["market_name"] == FALLBACK_LABEL


def test_answer_market_id_resolves_public_label_from_catalog() -> None:
    result = _resolver(PUBLIC_LABEL).answer_market_id(
        "ml_006 시장 규모는?", market_id="ml_006", period="2026-05"
    )

    assert result["tool_calls"][0]["render_data"]["market_name"] == PUBLIC_LABEL
    assert PUBLIC_LABEL in result["answer"]
    assert FALLBACK_LABEL not in result["answer"]


def test_answer_market_id_explicit_display_name_wins_over_catalog() -> None:
    result = _resolver(PUBLIC_LABEL).answer_market_id(
        "ml_006 시장 규모는?",
        market_id="ml_006",
        period="2026-05",
        market_display_name="고지혈증 치료제 시장",
    )

    assert result["tool_calls"][0]["render_data"]["market_name"] == "고지혈증 치료제 시장"
    assert "고지혈증 치료제 시장" in result["answer"]


# --- negative: no public label means the literal, never the identifier -----


def test_answer_market_id_falls_back_when_catalog_name_is_the_identifier() -> None:
    # catalog_membership COALESCEs a NULL catalog name to the ml_id, so this row
    # carries no public label and the identifier must not reach the answer.
    result = _resolver("ml_006").answer_market_id(
        "ml_006 시장 규모는?", market_id="ml_006", period="2026-05"
    )

    assert result["tool_calls"][0]["render_data"]["market_name"] == FALLBACK_LABEL
    assert FALLBACK_LABEL in result["answer"]
    assert "ml_006" not in result["answer"]


def test_answer_market_id_falls_back_without_catalog_rows() -> None:
    result = _resolver(None).answer_market_id(
        "ml_006 시장 규모는?", market_id="ml_006", period="2026-05"
    )

    assert result["tool_calls"][0]["render_data"]["market_name"] == FALLBACK_LABEL


def test_answer_market_id_falls_back_for_unknown_market_id() -> None:
    result = _resolver(PUBLIC_LABEL).answer_market_id(
        "ml_006 시장 규모는?", market_id="ml_006", period="2026-05", market_display_name=""
    )

    assert result["tool_calls"][0]["render_data"]["market_name"] == PUBLIC_LABEL


def test_member_listing_by_market_id_is_unchanged_by_the_label_argument() -> None:
    # The member branch renders through a template that shows no market label, so
    # F-2 leaves it as it was. Pinned so a later round cannot change the member
    # answer silently. Its summary line still carries the raw identifier, which is
    # a separate defect and out of scope for F-2.
    result = _resolver(PUBLIC_LABEL).answer_market_id(
        "ml_006 시장에 어떤 브랜드들이 있어?", market_id="ml_006", period="2026-05"
    )

    assert result["tool_calls"][0]["tool"] == "get_market_members"
    assert FALLBACK_LABEL not in result["answer"]


# --- layer 3: the reader contract, pinned in both directions ---------------


class _ReaderOnly:
    """Exposes only BrandMembershipReader.brand_memberships() — like production."""

    def __init__(self, rows: tuple[dict[str, str], ...]) -> None:
        self.rows = rows
        self.calls = 0

    def brand_memberships(self) -> tuple[dict[str, str], ...]:
        self.calls += 1
        return self.rows


class _SourceOnly:
    """Exposes only CatalogMembershipSource.load() — a source, not a reader."""

    def __init__(self, rows: tuple[dict[str, str], ...]) -> None:
        self.rows = rows
        self.calls = 0

    def load(self) -> tuple[dict[str, str], ...]:
        self.calls += 1
        return self.rows


def _resolver_with(reader: object) -> MarketScopeResolver:
    return MarketScopeResolver(
        cache_reader=StaticMetricsCacheReader(cache_brands=[], market_status={}),
        query_layer=_layer(),
        membership_reader=reader,  # type: ignore[arg-type]
    )


def test_operational_wrapper_type_has_reader_contract_and_not_source_contract() -> None:
    reader = _operational_reader(PUBLIC_LABEL)

    assert hasattr(reader, "brand_memberships")
    assert not hasattr(reader, "load")


def test_label_lookup_uses_the_reader_contract() -> None:
    reader = _ReaderOnly(_rows(PUBLIC_LABEL))

    result = _resolver_with(reader).answer_market_id(
        "ml_006 시장 규모는?", market_id="ml_006", period="2026-05"
    )

    assert reader.calls >= 1, "brand_memberships() was never called"
    assert result["tool_calls"][0]["render_data"]["market_name"] == PUBLIC_LABEL


def test_label_lookup_never_calls_the_source_contract() -> None:
    # Deploy 28 called load(). If that regresses, this test fails twice over: the
    # source gets used, and the failure is silent instead of loud.
    source = _SourceOnly(_rows(PUBLIC_LABEL))

    with pytest.raises(AttributeError):
        _resolver_with(source).answer_market_id(
            "ml_006 시장 규모는?", market_id="ml_006", period="2026-05"
        )

    assert source.calls == 0, "load() must never be called on the membership reader"
