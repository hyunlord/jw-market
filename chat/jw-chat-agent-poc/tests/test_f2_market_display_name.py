"""F-2: the market-id serving path must carry the catalog's public market label.

ANCH-3 established that `answer_market_id` / `market_scope_by_id` are the only
market path with no display-name argument, so both hardcode "해당 전략 시장".
`catalog_ml_market.name` is populated for every strategic market, and the
resolver already loads it, so the label only has to be plumbed through.

The literal stays as the last-resort fallback: catalog_membership COALESCEs the
name to the ml_id upstream, so a row that carries no public name must not put an
internal identifier on screen.
"""

from __future__ import annotations

from jw_chat_agent_poc.resolver.catalog_membership import StaticCatalogMembershipReader
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


def _resolver(market_name: str | None) -> MarketScopeResolver:
    rows = ()
    if market_name is not None:
        rows = (
            {
                "brand": "브랜드1",
                "brand_alias": "",
                "market_id": "ml_006",
                "market_name": market_name,
                "support_source": "strategic_mart",
            },
        )
    return MarketScopeResolver(
        cache_reader=StaticMetricsCacheReader(cache_brands=[], market_status={}),
        query_layer=_layer(),
        membership_reader=StaticCatalogMembershipReader(rows=rows),
    )


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


def test_answer_market_id_explicit_display_name_wins_over_catalog() -> None:
    result = _resolver(PUBLIC_LABEL).answer_market_id(
        "ml_006 시장 규모는?",
        market_id="ml_006",
        period="2026-05",
        market_display_name="고지혈증 치료제 시장",
    )

    assert result["tool_calls"][0]["render_data"]["market_name"] == "고지혈증 치료제 시장"
    assert "고지혈증 치료제 시장" in result["answer"]


def test_member_listing_by_market_id_is_unchanged_by_the_label_argument() -> None:
    # The member branch renders through a template that shows no market label, so
    # F-2 leaves it as it was. It is pinned here so a later round cannot change the
    # member answer without a test saying so. Its summary line still carries the raw
    # identifier, which is a separate defect and out of scope for F-2.
    result = _resolver(PUBLIC_LABEL).answer_market_id(
        "ml_006 시장에 어떤 브랜드들이 있어?", market_id="ml_006", period="2026-05"
    )

    assert result["tool_calls"][0]["tool"] == "get_market_members"
    assert FALLBACK_LABEL not in result["answer"]
