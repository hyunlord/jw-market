from __future__ import annotations

from pipeline.scripts.api.handlers.multi_market import choose_primary_market


def _row(market_id: str, values: list[tuple[str, float]]) -> dict[str, object]:
    return {
        "market_id": market_id,
        "response_json": {
            "data": {
                "market_size_series": [
                    {"period": period, "market_size": value}
                    for period, value in values
                ],
            },
        },
    }


def test_primary_market_uses_latest_market_total() -> None:
    rows = [
        _row("strategy_003", [("2026-06", 500.0), ("2026-07", 100.0)]),
        _row("strategy_009", [("2026-06", 200.0), ("2026-07", 900.0)]),
    ]

    primary, markets = choose_primary_market(rows)

    assert primary["market_id"] == "strategy_009"
    assert markets == [
        {"market_id": "strategy_003", "is_primary": False},
        {"market_id": "strategy_009", "is_primary": True},
    ]


def test_explicit_preferred_market_preserves_existing_assignment() -> None:
    rows = [
        _row("strategy_003", [("2026-07", 100.0)]),
        _row("strategy_009", [("2026-07", 900.0)]),
    ]

    primary, _markets = choose_primary_market(rows, preferred_market_id="strategy_003")

    assert primary["market_id"] == "strategy_003"


def test_primary_market_tie_breaks_by_market_id() -> None:
    rows = [
        _row("strategy_009", [("2026-07", 900.0)]),
        _row("strategy_003", [("2026-07", 900.0)]),
    ]

    primary, _markets = choose_primary_market(rows)

    assert primary["market_id"] == "strategy_003"
