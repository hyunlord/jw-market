from __future__ import annotations

import json

from pipeline.scripts.api.routes import market_status


def test_market_status_adds_source_scoped_agent_freshness(monkeypatch) -> None:
    rows = iter(
        (
            {"response_json": json.dumps({"brand_cards": []})},
            {"epoch": "2026-05", "manifest_sha": "a" * 64},
            {
                "epoch": "2026-05",
                "manifest_sha": "a" * 64,
                "status": "complete",
                "finished_at": "2026-07-30T01:00:00+00:00",
            },
            {"finished_at": "2026-07-30T01:00:00+00:00"},
            {"epoch": "2026-Q1", "manifest_sha": "b" * 64},
            {
                "epoch": "2025-Q4",
                "manifest_sha": "c" * 64,
                "status": "complete",
                "finished_at": "2026-07-29T01:00:00+00:00",
            },
            {"finished_at": "2026-07-29T01:00:00+00:00"},
        )
    )
    monkeypatch.setattr(market_status.db, "fetch_one", lambda *_args, **_kwargs: next(rows))
    monkeypatch.setattr(market_status.db, "fetch_all", lambda *_args, **_kwargs: [])

    payload = market_status.market_status()

    assert payload["agent_refresh"] == {
        "ubist": {
            "agent_epoch": "2026-05",
            "agent_status": "fresh",
            "last_success_at": "2026-07-30T01:00:00+00:00",
        },
        "iqvia_nsa": {
            "agent_epoch": "2025-Q4",
            "agent_status": "stale",
            "last_success_at": "2026-07-29T01:00:00+00:00",
        },
    }


def test_market_status_preserves_unknown_when_agent_status_query_fails(
    monkeypatch,
) -> None:
    calls = 0

    def fetch_one(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"response_json": json.dumps({"brand_cards": []})}
        raise RuntimeError("injected unavailable ledger")

    monkeypatch.setattr(market_status.db, "fetch_one", fetch_one)
    monkeypatch.setattr(market_status.db, "fetch_all", lambda *_args, **_kwargs: [])

    payload = market_status.market_status()

    assert payload["agent_refresh"] == {
        source: {
            "agent_epoch": None,
            "agent_status": "unknown",
            "last_success_at": None,
        }
        for source in ("ubist", "iqvia_nsa")
    }
