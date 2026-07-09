from __future__ import annotations

import json

from bundle_builder.zero_kpi_provider import (
    BatchGeneralZeroKpiSnapshotProvider,
    brand_cagr_pct,
    snapshot_from_metric_rows,
)


class _Cursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []

    def execute(self, sql, params=()):
        self.executed.append((sql, tuple(params or ())))

    def fetchall(self):
        return list(self._rows)


class _Conn:
    def __init__(self, rows):
        self.cursor_obj = _Cursor(rows)

    def cursor(self):
        return self.cursor_obj


def _row(brand_key: str, brand_name: str, value: float, history: dict, atc4: str = "C10A", source: str = "ubist"):
    return {
        "brand_key": brand_key,
        "brand_name": brand_name,
        "atc4_code": atc4,
        "atc4_desc": "지질조절제",
        "source": source,
        "measure": "sales",
        "metric_history": json.dumps(history, ensure_ascii=False) if isinstance(history, dict) else history,
        "_value": value,
    }


def test_brand_cagr_pct_handles_monthly_and_quarterly_periods() -> None:
    monthly = {
        "2025-01": {"raw_value": 100.0},
        "2026-01": {"raw_value": 121.0},
    }
    quarterly = {
        "2025-Q1": {"raw_value": 100.0},
        "2026-Q1": {"raw_value": 110.0},
    }

    assert round(brand_cagr_pct(monthly) or 0, 1) == 21.0
    assert round(brand_cagr_pct(quarterly) or 0, 1) == 10.0


def test_snapshot_from_metric_rows_builds_rank_share_hhi_and_brand_cagr() -> None:
    rows = [
        _row("leader", "리더", 70.0, {"2025-01": {"raw_value": 50.0}, "2026-01": {"raw_value": 70.0}}),
        _row("target", "타겟", 20.0, {"2025-01": {"raw_value": 10.0}, "2026-01": {"raw_value": 20.0}}),
        _row("small", "스몰", 10.0, {"2026-01": {"raw_value": 10.0}}),
    ]

    snapshot = snapshot_from_metric_rows(rows, "target", "표시명")

    assert snapshot.brand == "표시명"
    assert snapshot.market_name == "지질조절제"
    assert snapshot.rank == 2
    assert snapshot.share_pct == 20.0
    assert round(snapshot.cagr_pct or 0, 1) == 100.0
    assert round(snapshot.hhi or 0, 1) == 5400.0
    assert snapshot.market_size_recent == 100.0
    assert snapshot.ei is None
    assert snapshot.momentum is None


def test_batch_general_zero_kpi_provider_caches_sales_rows_and_keeps_requested_brand_name() -> None:
    rows = [
        _row("leader", "리더", 70.0, {"2026-01": {"raw_value": 70.0}}),
        _row("target", "타겟원본", 30.0, {"2026-01": {"raw_value": 30.0}}),
    ]
    conn = _Conn(rows)
    provider = BatchGeneralZeroKpiSnapshotProvider(conn)

    first = provider.get_snapshot("target", "타겟표시")
    second = provider.get_snapshot("target", "타겟표시")

    assert first.brand == "타겟표시"
    assert first.rank == 2
    assert second.rank == first.rank
    assert len(conn.cursor_obj.executed) == 1


def test_batch_general_zero_kpi_provider_returns_brand_only_snapshot_when_missing() -> None:
    provider = BatchGeneralZeroKpiSnapshotProvider(_Conn([]))

    snapshot = provider.get_snapshot("missing", "미싱")

    assert snapshot.brand == "미싱"
    assert snapshot.rank is None
    assert snapshot.share_pct is None
