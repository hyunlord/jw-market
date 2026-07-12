from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from jw_chat_agent_poc.tools.general_view_backend import (
    AtcCandidate,
    GeneralMarket,
    GeneralViewBackendError,
    TopBrand,
    focus_brand_key,
)


class GeneralViewMartLoadError(GeneralViewBackendError):
    """Raised when exact general-view mart rows cannot be loaded safely."""


@dataclass(frozen=True, slots=True)
class GeneralMartRows:
    atc4_code: str
    atc4_description: str
    source: str
    measure: str
    unit: str
    market_size_series: dict[str, float]
    brand_ranking: dict[str, list[dict[str, Any]]]
    brand_name: str | None
    brand_metric_history: dict[str, dict[str, Any]]


class GeneralMartReader(Protocol):
    def read(self, atc4: str, brand: str | None, source: str, measure: str) -> GeneralMartRows: ...


class GeneralBackendFallback(Protocol):
    def candidates(self, brand: str, source: str) -> tuple[AtcCandidate, ...]: ...
    def market(self, atc4: str, brand: str | None, source: str, measure: str) -> GeneralMarket: ...


@dataclass(frozen=True, slots=True)
class MariaDbGeneralMartReader:
    host: str = os.environ.get("CHAT_CACHE_DB_HOST", "llmops-mariadb-service.llmops.svc.cluster.local")
    port: int = int(os.environ.get("CHAT_CACHE_DB_PORT", "3306"))
    database: str = field(
        default_factory=lambda: os.environ.get(
            "CHAT_GENERAL_MART_SCHEMA",
            os.environ.get("CHAT_CACHE_DB_NAME", "jw_mart"),
        )
    )
    user: str = os.environ.get("CHAT_CACHE_DB_USER", "llmops")
    password: str = os.environ.get("CHAT_CACHE_DB_PASSWORD", "")
    connect_timeout_s: int = int(os.environ.get("CHAT_CACHE_DB_CONNECT_TIMEOUT_S", "3"))
    read_timeout_s: int = int(os.environ.get("CHAT_CACHE_DB_READ_TIMEOUT_S", "5"))

    def read(self, atc4: str, brand: str | None, source: str, measure: str) -> GeneralMartRows:
        import pymysql

        mart_source = "iqvia_nsa" if source.lower() == "iqvia" else source.lower()
        try:
            with pymysql.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database,
                connect_timeout=self.connect_timeout_s,
                read_timeout=self.read_timeout_s,
                write_timeout=self.read_timeout_s,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=True,
            ) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT atc4_code, atc4_desc, source, measure, unit_label,
                               market_size_series, brand_ranking
                        FROM mart_general_market_metric
                        WHERE atc4_code=%s AND source=%s AND measure=%s
                        LIMIT 1
                        """,
                        (atc4.upper(), mart_source, measure.lower()),
                    )
                    market_row = cursor.fetchone()
                    brand_row = None
                    if brand:
                        cursor.execute(
                            """
                            SELECT brand_name, metric_history
                            FROM mart_general_brand_metric
                            WHERE atc4_code=%s AND source=%s AND measure=%s AND brand_key=%s
                            LIMIT 1
                            """,
                            (atc4.upper(), mart_source, measure.lower(), focus_brand_key(brand)),
                        )
                        brand_row = cursor.fetchone()
        except pymysql.MySQLError as exc:
            raise GeneralViewMartLoadError("general-view mart query failed") from exc

        if not market_row or (brand and not brand_row):
            raise GeneralViewMartLoadError("general-view exact mart row not found")
        return GeneralMartRows(
            atc4_code=str(market_row["atc4_code"]),
            atc4_description=str(market_row["atc4_desc"] or f"ATC4 {atc4.upper()}"),
            source=source.lower(),
            measure=measure.lower(),
            unit=str(market_row["unit_label"] or ""),
            market_size_series=_number_map(market_row["market_size_series"]),
            brand_ranking=_ranking_map(market_row["brand_ranking"]),
            brand_name=str(brand_row["brand_name"]) if brand_row else None,
            brand_metric_history=_metric_map(brand_row["metric_history"]) if brand_row else {},
        )


@dataclass(frozen=True, slots=True)
class GeneralViewMartBackend:
    reader: GeneralMartReader
    fallback: GeneralBackendFallback

    def candidates(self, brand: str, source: str) -> tuple[AtcCandidate, ...]:
        return self.fallback.candidates(brand, source)

    def market(self, atc4: str, brand: str | None, source: str, measure: str) -> GeneralMarket:
        try:
            rows = self.reader.read(atc4, brand, source, measure)
            return _market_from_rows(rows)
        except GeneralViewMartLoadError:
            return self.fallback.market(atc4, brand, source, measure)


def _market_from_rows(rows: GeneralMartRows) -> GeneralMarket:
    periods = set(rows.market_size_series) | set(rows.brand_ranking) | set(rows.brand_metric_history)
    if not periods:
        raise GeneralViewMartLoadError("general-view mart rows contain no periods")
    period = max(periods)
    ranking_rows = rows.brand_ranking.get(period, [])
    top_brands = tuple(
        TopBrand(
            brand=str(item.get("brand") or item.get("brand_key") or ""),
            rank=_as_int(item.get("rank")),
            value=_as_float(item.get("raw_value")),
            share_pct=_as_float(item.get("ms")),
        )
        for item in ranking_rows[:5]
    )
    metric = rows.brand_metric_history.get(period, {})
    return GeneralMarket(
        view_type="general_view",
        market_basis="ATC4",
        atc4_code=rows.atc4_code.upper(),
        atc4_description=rows.atc4_description,
        source="IQVIA" if rows.source == "iqvia" else rows.source.upper(),
        measure=rows.measure,
        unit=rows.unit,
        period=period,
        market_size=rows.market_size_series.get(period),
        brand=rows.brand_name,
        brand_value=_as_float(metric.get("raw_value")),
        brand_share_pct=_as_float(metric.get("ms")),
        brand_rank=_as_int(metric.get("rank")),
        top_brands=top_brands,
    )


def _json_dict(raw: str | dict[str, Any] | None) -> dict[str, Any]:
    value = json.loads(raw) if isinstance(raw, str) else raw
    return value if isinstance(value, dict) else {}


def _number_map(raw: str | dict[str, Any] | None) -> dict[str, float]:
    return {str(key): float(value) for key, value in _json_dict(raw).items() if isinstance(value, int | float)}


def _ranking_map(raw: str | dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    return {
        str(key): [item for item in value if isinstance(item, dict)]
        for key, value in _json_dict(raw).items()
        if isinstance(value, list)
    }


def _metric_map(raw: str | dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return {
        str(key): value
        for key, value in _json_dict(raw).items()
        if isinstance(value, dict)
    }


def _as_float(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _as_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int | float) else None
