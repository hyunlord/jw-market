"""Native market-scope loading and forecast payload construction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from pipeline.scripts.etl import build_cache_deep_analysis_general as general_builder
from pipeline.scripts.forecast import forecast_runner

ViewKind = Literal["general", "market_landscape", "competitive_dynamics"]
VALID_SOURCES: Final[frozenset[str]] = frozenset({"iqvia_nsa", "ubist"})
HORIZON_YEARS: Final[int] = 5


@dataclass(frozen=True, slots=True)
class Scope:
    view_kind: ViewKind
    market_id: str
    source: str

    @classmethod
    def general(cls, market_id: str, source: str) -> Scope:
        return cls("general", market_id, source)

    @classmethod
    def market_landscape(cls, market_id: str, source: str) -> Scope:
        return cls("market_landscape", market_id, source)

    @classmethod
    def competitive_dynamics(cls, market_id: str, source: str) -> Scope:
        return cls("competitive_dynamics", market_id, source)

    @property
    def table_spec(self) -> tuple[str, str, str]:
        match self.view_kind:
            case "general":
                return ("mart_general_brand_metric", "atc4_code", "gen")
            case "market_landscape":
                return ("mart_strategic_ml_brand_metric", "ml_id", "ml")
            case "competitive_dynamics":
                return ("mart_strategic_cd_brand_metric", "cd_market_id", "cd")


@dataclass(frozen=True, slots=True)
class Unit:
    brand_key: str
    scope: Scope

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.brand_key, self.scope.source, self.scope.market_id)


@dataclass(frozen=True, slots=True)
class BlockRow:
    brand_key: str
    source: str
    market_id: str
    view_kind: str
    forecast_json: str
    simulation_json: str | None
    generation_status: str
    no_history_fallback: str | None
    simulation_available: int
    source_epoch: str
    source_computed_at: datetime | None
    generated_at: datetime


@dataclass(frozen=True, slots=True)
class HorizonRow:
    market_id: str
    source: str
    measure: str
    view_kind: str
    forecast_horizon_json: str
    source_row_count: int
    source_epoch: str
    source_computed_at: datetime | None
    generated_at: datetime


def row_cache_id(scope: Scope, raw_id: int | str) -> str:
    return f"{scope.table_spec[2]}:{raw_id}"


def load_units(connection: Any) -> list[Unit]:
    sql = """
        SELECT 'general' AS view_kind, atc4_code AS market_id, brand_key, source
        FROM mart_general_brand_metric WHERE measure = 'sales'
        UNION ALL
        SELECT 'market_landscape', ml_id, brand_key, source
        FROM mart_strategic_ml_brand_metric WHERE measure = 'sales'
        UNION ALL
        SELECT 'competitive_dynamics', cd_market_id, brand_key, source
        FROM mart_strategic_cd_brand_metric WHERE measure = 'sales'
    """
    with connection.cursor() as cursor:
        cursor.execute(sql)
        rows = cursor.fetchall()
    units = [Unit(str(row["brand_key"]), Scope(str(row["view_kind"]), str(row["market_id"]), str(row["source"]))) for row in rows]
    units.sort(key=lambda item: (item.scope.view_kind, item.scope.market_id, item.scope.source, item.brand_key))
    if len({unit.key for unit in units}) != len(units):
        raise RuntimeError("forecast universe contains duplicate block keys")
    return units


def load_scope(connection: Any, scope: Scope) -> list[dict[str, Any]]:
    table, market_column, prefix = scope.table_spec
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT CONCAT('{prefix}:', id) AS id, {market_column} AS atc4_code,
                   brand_key, brand_name, source, measure, unit_label,
                   COALESCE(is_jw, 0) AS is_jw, metric_history, computed_at
            FROM {general_builder.quote_ident(table)}
            WHERE {market_column} = %s AND source = %s
            ORDER BY measure, brand_name, brand_key, id
            """ if scope.view_kind != "general" else f"""
            SELECT CONCAT('{prefix}:', id) AS id, {market_column} AS atc4_code,
                   brand_key, brand_name, source, measure, unit_label,
                   0 AS is_jw, metric_history, computed_at
            FROM {general_builder.quote_ident(table)}
            WHERE {market_column} = %s AND source = %s
            ORDER BY measure, brand_name, brand_key, id
            """,
            (scope.market_id, scope.source),
        )
        rows = list(cursor.fetchall())
    forecast_runner._FORECAST_ENTRY_CACHE.clear()
    forecast_runner._MARKET_FORECAST_CACHE.clear()
    if not rows:
        raise RuntimeError(f"empty native scope: {scope!r}")
    if any(str(row["atc4_code"]) != scope.market_id or str(row["source"]) != scope.source for row in rows):
        raise RuntimeError(f"native scope contamination: {scope!r}")
    return rows
