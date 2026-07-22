from __future__ import annotations

import json

import pytest

from pipeline.scripts.api.routes import market_status


def test_market_status_overlays_exclusive_brand_cagr_from_mart(monkeypatch) -> None:
    cached = {
        "brand_cards": [
            {
                "brand": "리바로",
                "market_id": "strategy_006",
                "front": {"default_source": "UBIST"},
                "back": {"cagr_5y_pct": 3.9056},
                "back_extended": {"brand_cagr_5y_pct": 3.9056},
            },
            {
                "brand": "악템라",
                "market_id": "strategy_011",
                "front": {"default_source": "IQVIA"},
                "back": {"cagr_5y_pct": -3.9362},
                "back_extended": {"brand_cagr_5y_pct": -3.9362},
            },
            {
                "brand": "신규브랜드",
                "market_id": "strategy_999",
                "back": {"cagr_5y_pct": None},
                "back_extended": {"brand_cagr_5y_pct": None},
            },
        ]
    }
    mart_rows = [
        {
            "brand_name": "리바로",
            "ml_id": "ml_006",
            "source": "ubist",
            "metric_history": json.dumps({"2021-05": 100.0, "2026-05": 121.0}),
        },
        {
            "brand_name": "악템라",
            "ml_id": "ml_011",
            "source": "iqvia_nsa",
            "metric_history": json.dumps({"2023-Q1": 100.0, "2026-Q1": 90.0}),
        },
        {
            "brand_name": "악템라",
            "ml_id": "ml_011",
            "source": "ubist",
            "metric_history": json.dumps({"2021-05": 100.0, "2026-05": 121.0}),
        },
        {
            "brand_name": "신규브랜드",
            "ml_id": "ml_999",
            "source": "iqvia_nsa",
            "metric_history": json.dumps({"2025-Q1": 100.0, "2026-Q1": 105.0}),
        },
    ]

    monkeypatch.setattr(market_status.db, "fetch_one", lambda *_args, **_kwargs: {"response_json": json.dumps(cached)})

    def fetch_all(sql: str, *_args, **_kwargs):
        if "brand_name" in sql:
            return mart_rows
        return []

    monkeypatch.setattr(market_status.db, "fetch_all", fetch_all)

    payload = market_status.market_status()
    cards = {card["brand"]: card for card in payload["brand_cards"]}

    assert cards["리바로"]["back"] == {"cagr_5y_pct": 3.9056}
    assert cards["리바로"]["back_extended"]["brand_cagr_5y_pct"] == pytest.approx(3.886)
    assert cards["리바로"]["back_extended"]["brand_cagr_3y_pct"] is None
    assert cards["악템라"]["back"] == {"cagr_5y_pct": -3.9362}
    assert cards["악템라"]["back_extended"]["brand_cagr_5y_pct"] is None
    assert cards["악템라"]["back_extended"]["brand_cagr_3y_pct"] == pytest.approx(-3.4511)
    assert cards["신규브랜드"]["back_extended"]["brand_cagr_5y_pct"] is None
    assert cards["신규브랜드"]["back_extended"]["brand_cagr_3y_pct"] is None


def test_market_status_brand_cagr_keeps_each_source_separate(monkeypatch) -> None:
    rows = [
        {
            "brand_name": "리바로",
            "ml_id": "ml_006",
            "source": "iqvia_nsa",
            "metric_history": json.dumps({"2023-Q1": 100.0, "2026-Q1": 80.0}),
        },
        {
            "brand_name": "리바로",
            "ml_id": "ml_006",
            "source": "ubist",
            "metric_history": json.dumps({"2021-05": 100.0, "2026-05": 121.0}),
        },
    ]
    monkeypatch.setattr(market_status.db, "fetch_all", lambda *_args, **_kwargs: rows)

    values = market_status._brand_cagr_by_brand()

    assert values[("리바로", "ml_006", "ubist")] == (pytest.approx(3.886), None)
    assert values[("리바로", "ml_006", "iqvia_nsa")] == (None, pytest.approx(-7.1682))


def test_market_status_brand_cagr_never_crosses_market_scope(monkeypatch) -> None:
    rows = [
        {
            "brand_name": "악템라",
            "ml_id": "ml_001",
            "source": "iqvia_nsa",
            "metric_history": json.dumps({"2023-Q1": 100.0, "2026-Q1": 90.0}),
        },
        {
            "brand_name": "악템라",
            "ml_id": "ml_777",
            "source": "ubist",
            "metric_history": json.dumps({"2021-05": 100.0, "2026-05": 121.0}),
        },
    ]
    payload = {
        "brand_cards": [
            {
                "brand": "악템라",
                "market_id": "strategy_001",
                "front": {"default_source": "IQVIA"},
                "back_extended": {},
            },
        ]
    }

    market_status._overlay_brand_cagr(payload, rows)

    extended = payload["brand_cards"][0]["back_extended"]
    assert extended["brand_cagr_5y_pct"] is None
    assert extended["brand_cagr_3y_pct"] == pytest.approx(-3.4511)
