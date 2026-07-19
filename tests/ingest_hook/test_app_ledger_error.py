"""G-1 (service edge): a terminal ledger connection loss becomes a *clear* 5xx.

When ``Ledger`` cannot revive the mysql connection (ping + reconnect + one retry
all failed) it raises ``LedgerConnectionError``. The trigger service must turn
that into an explicit HTTP 500 whose body names the cause — not the generic
``{"detail": "Internal Server Error"}`` and never a silent 200. The handler is
global, so it covers every route (webhook/status/reconcile) uniformly; /status
exercises it with the least setup.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from pipeline.scripts.ingest_hook.app import IngestService, create_app
from pipeline.scripts.ingest_hook.ledger import LedgerConnectionError

IDENTITY_QS = {"epoch": "2026-03", "category": "ubist", "manifest_sha": "a" * 64}


class _ConnLostLedger:
    """Stands in for a ledger whose connection could not be revived."""

    def status(self, *args, **kwargs):
        raise LedgerConnectionError(
            "ingest ledger DB connection unavailable after reconnect and one retry: "
            "(2006, 'MySQL server has gone away')"
        )


def _client(ledger) -> TestClient:
    app = create_app(IngestService(ledger, input_root=None))
    # raise_server_exceptions=False: an *unhandled* error yields FastAPI's generic
    # 500 body, so this test fails until the LedgerConnectionError handler exists.
    return TestClient(app, raise_server_exceptions=False)


def test_ledger_connection_error_maps_to_clear_500():
    resp = _client(_ConnLostLedger()).get("/ingest/status", params=IDENTITY_QS)

    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert detail != "Internal Server Error"  # not the generic body
    assert "ledger" in detail.lower()  # names the cause: no silent / opaque failure


def test_healthz_unaffected():
    resp = _client(_ConnLostLedger()).get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
