from __future__ import annotations

import json

import pytest

from pipeline.scripts.api.routes import market_status


def test_market_status_overlays_exclusive_brand_cagr_from_mart(monkeypatch) -> None:
    cached = {
        "brand_cards": [
            {
                "brand": "리바로",
                "back": {"cagr_5y_pct": 3.9056},
                "back_extended": {"brand_cagr_5y_pct": 3.9056},
            },
            {
                "brand": "악템라",
                "back": {"cagr_5y_pct": -3.9362},
                "back_extended": {"brand_cagr_5y_pct": -3.9362},
            },
            {
                "brand": "신규브랜드",
                "back": {"cagr_5y_pct": None},
                "back_extended": {"brand_cagr_5y_pct": None},
            },
        ]
    }
    mart_rows = [
        {
            "brand_name": "리바로",
            "source": "ubist",
            "metric_history": json.dumps({"2021-05": 100.0, "2026-05": 121.0}),
        },
        {
            "brand_name": "악템라",
            "source": "iqvia_nsa",
            "metric_history": json.dumps({"2023-Q1": 100.0, "2026-Q1": 90.0}),
        },
        {
            "brand_name": "신규브랜드",
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


def test_market_status_brand_cagr_prefers_ubist_row(monkeypatch) -> None:
    rows = [
        {
            "brand_name": "리바로",
            "source": "iqvia_nsa",
            "metric_history": json.dumps({"2023-Q1": 100.0, "2026-Q1": 80.0}),
        },
        {
            "brand_name": "리바로",
            "source": "ubist",
            "metric_history": json.dumps({"2021-05": 100.0, "2026-05": 121.0}),
        },
    ]
    monkeypatch.setattr(market_status.db, "fetch_all", lambda *_args, **_kwargs: rows)

    values = market_status._brand_cagr_by_brand()

    assert values["리바로"] == (pytest.approx(3.886), None)
