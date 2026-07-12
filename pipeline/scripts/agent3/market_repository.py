from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .db import DbConfig, connect
from .json_util import parse_history, parse_json_object
from .source_loader import Agent3Source


MarketViewKind = Literal["market_landscape", "competitive_dynamics"]


@dataclass(frozen=True, slots=True)
class MarketUnit:
    view_kind: MarketViewKind
    market_id: str
    brand_key: str
    brand_name: str
    source: Agent3Source
    mart_source: str


@dataclass(frozen=True, slots=True)
class StrategicMetricRow:
    view_kind: MarketViewKind
    market_id: str
    brand_key: str
    brand_name: str
    source: str
    measure: str
    unit_label: str
    raw_value_history: dict[str, float]
    channel_data: dict[str, Any]
    specialty_data: dict[str, Any]
    dimension_data: dict[str, Any]
    dimension_channel_data: dict[str, Any]
    dimension_specialty_data: dict[str, Any]


class StrategicMarketRepository:
    def __init__(self, config: DbConfig | None = None) -> None:
        self.config = config or DbConfig.from_env()

    def load_native_scope(self, unit: MarketUnit) -> list[StrategicMetricRow]:
        table, market_column = _table_spec(unit.view_kind)
        with connect(self.config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT brand_key, brand_name, source, measure, unit_label,
                           raw_value_history, channel_data, specialty_data,
                           dimension_data, dimension_channel_data,
                           dimension_specialty_data
                    FROM {table}
                    WHERE {market_column}=%s AND source=%s AND measure='sales'
                    ORDER BY brand_key
                    """,
                    (unit.market_id, unit.mart_source),
                )
                rows = cursor.fetchall()
        scope = [_metric_row(unit, row) for row in rows]
        _validate_scope(unit, scope)
        return scope


def read_market_units(path: Path) -> list[MarketUnit]:
    with path.open(encoding="utf-8", newline="") as stream:
        return [_parse_unit(row) for row in csv.DictReader(stream, delimiter="\t")]


def _parse_unit(row: dict[str, str]) -> MarketUnit:
    view_kind = row["view_kind"]
    source = row["source"]
    if view_kind not in {"market_landscape", "competitive_dynamics"}:
        raise ValueError(f"unsupported strategic view_kind: {view_kind}")
    if source not in {"iqvia", "ubist"}:
        raise ValueError(f"unsupported strategic source: {source}")
    expected_mart_source = "iqvia_nsa" if source == "iqvia" else "ubist"
    if row["mart_source"] != expected_mart_source:
        raise ValueError(
            f"source mapping mismatch: {source}/{row['mart_source']} expected {expected_mart_source}"
        )
    market_id = row["market_id"]
    expected_prefix = "ml_" if view_kind == "market_landscape" else "cd_"
    if not market_id.startswith(expected_prefix):
        raise ValueError(f"market/view mismatch: {view_kind}/{market_id}")
    return MarketUnit(
        view_kind=view_kind,
        market_id=market_id,
        brand_key=row["brand_key"],
        brand_name=row["brand_name"],
        source=source,
        mart_source=row["mart_source"],
    )


def _table_spec(view_kind: MarketViewKind) -> tuple[str, str]:
    if view_kind == "market_landscape":
        return "mart_strategic_ml_brand_metric", "ml_id"
    return "mart_strategic_cd_brand_metric", "cd_market_id"


def _metric_row(unit: MarketUnit, row: dict[str, Any]) -> StrategicMetricRow:
    return StrategicMetricRow(
        view_kind=unit.view_kind,
        market_id=unit.market_id,
        brand_key=str(row["brand_key"]),
        brand_name=str(row["brand_name"]),
        source=str(row["source"]),
        measure=str(row["measure"]),
        unit_label=str(row["unit_label"]),
        raw_value_history=parse_history(row.get("raw_value_history")),
        channel_data=parse_json_object(row.get("channel_data")),
        specialty_data=parse_json_object(row.get("specialty_data")),
        dimension_data=parse_json_object(row.get("dimension_data")),
        dimension_channel_data=parse_json_object(row.get("dimension_channel_data")),
        dimension_specialty_data=parse_json_object(row.get("dimension_specialty_data")),
    )


def _validate_scope(unit: MarketUnit, rows: list[StrategicMetricRow]) -> None:
    if not rows:
        raise RuntimeError(f"empty strategic native scope: {unit.view_kind}/{unit.market_id}/{unit.mart_source}")
    if not any(row.brand_key == unit.brand_key for row in rows):
        raise RuntimeError(
            f"target absent from strategic scope: {unit.brand_key}/{unit.market_id}/{unit.mart_source}"
        )
    for row in rows:
        if row.view_kind != unit.view_kind or row.market_id != unit.market_id or row.source != unit.mart_source:
            raise RuntimeError(f"strategic scope contamination: {unit.market_id}/{unit.mart_source}")
