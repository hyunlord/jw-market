from __future__ import annotations

from dataclasses import dataclass

from bundle_builder.market_view_builder import _view_exists
from bundle_builder.mart_metric_reader import use_cache_free_ml_kpi


@dataclass(frozen=True)
class _MarketConfig:
    ms_computation: dict


@dataclass(frozen=True)
class _Config:
    market: _MarketConfig


class _Cursor:
    def __init__(self) -> None:
        self.sql = ""

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, sql: str, _params: tuple) -> None:
        self.sql = sql

    def fetchone(self) -> dict:
        return {"ok": 1}


class _Connection:
    def __init__(self) -> None:
        self.cursor_obj = _Cursor()

    def cursor(self) -> _Cursor:
        return self.cursor_obj


def test_cache_free_flag_reads_market_config() -> None:
    assert use_cache_free_ml_kpi(_Config(_MarketConfig({"kpi_source": "mart"})))
    assert use_cache_free_ml_kpi(_Config(_MarketConfig({"cache_free_ml_kpi": True})))
    assert not use_cache_free_ml_kpi(_Config(_MarketConfig({"kpi_source": "cache_cause"})))


def test_ml_view_exists_uses_mart_gate_when_cache_free() -> None:
    conn = _Connection()

    assert _view_exists("리바로젯", "ml_006", "market_landscape", "UBIST", "sales", _Config(_MarketConfig({"kpi_source": "mart"})), conn)

    assert "mart_strategic_ml_brand_metric" in conn.cursor_obj.sql
    assert "cache_cause" not in conn.cursor_obj.sql
