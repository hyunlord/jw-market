from __future__ import annotations

from dataclasses import dataclass

from pipeline.scripts.api.competitor_ranking import CompetitorRankItem, select_top_competitors


@dataclass(frozen=True, slots=True)
class Payload:
    brand_key: str


def test_select_top_competitors_pins_selected_brand_then_total_sales_order() -> None:
    items = (
        _item("focus", 1.0),
        _item("a", 100.0),
        _item("b", 80.0),
        _item("c", 120.0),
        _item("d", 60.0),
        _item("e", 40.0),
        _item("f", 20.0),
    )

    selected = select_top_competitors(items, selected_brand_key="focus", top_n=5)

    assert [item.brand_key for item in selected] == ["focus", "c", "a", "b", "d", "e"]


def test_select_top_competitors_breaks_total_sales_ties_by_brand_key() -> None:
    items = (_item("focus", 1.0), _item("b", 50.0), _item("a", 50.0), _item("c", 10.0))

    selected = select_top_competitors(items, selected_brand_key="focus", top_n=5)

    assert [item.brand_key for item in selected] == ["focus", "a", "b", "c"]


def test_select_top_competitors_returns_available_market_when_less_than_top_n() -> None:
    items = (_item("focus", 1.0), _item("b", 50.0))

    selected = select_top_competitors(items, selected_brand_key="focus", top_n=5)

    assert [item.brand_key for item in selected] == ["focus", "b"]


def _item(brand_key: str, total_value: float) -> CompetitorRankItem[Payload]:
    return CompetitorRankItem(brand_key=brand_key, total_value=total_value, payload=Payload(brand_key))
