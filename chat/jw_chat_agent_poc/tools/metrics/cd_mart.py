from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import time
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class CdBrandLink:
    """One competitive-dynamics brand membership row (measure=sales)."""

    brand_name: str
    cd_market_id: str
    source: str
    ml_id: str


@dataclass(frozen=True, slots=True)
class CdMartSnapshot:
    """In-memory mart_strategic_cd_* snapshot for competitive-dynamics market size lookups."""

    brand_links: tuple[CdBrandLink, ...]
    market_series: dict[tuple[str, str], dict[str, Any]]
    loaded_at: float

    def market_size_series(self, *, brand: str, source: str, market_id: str) -> dict[str, dict[str, float | None]]:
        source_key = mart_source_key(source)
        links = [
            link
            for link in self.brand_links
            if link.brand_name == brand and link.source == source_key
        ]
        if market_id and len(links) > 1:
            matched = [link for link in links if strategy_id_from_ml(link.ml_id) == market_id]
            if matched:
                links = matched
        if not links:
            raise LookupError(f"cd mart brand row is missing: brand={brand} source={source_key}")
        link = links[0]
        if market_id and link.ml_id and strategy_id_from_ml(link.ml_id) != market_id:
            raise LookupError(
                f"cd mart market mismatch: brand={brand} requested={market_id} mart_ml={link.ml_id}"
            )
        series = self.market_series.get((link.cd_market_id, source_key))
        if not isinstance(series, dict) or not series:
            raise LookupError(f"cd mart market series is missing: cd_market={link.cd_market_id} source={source_key}")
        return series_with_yoy(series)


class CdMartReader(Protocol):
    def load(self) -> CdMartSnapshot: ...


@dataclass(frozen=True, slots=True)
class StaticCdMartReader:
    brand_links: tuple[CdBrandLink, ...]
    market_series: dict[tuple[str, str], dict[str, Any]]

    def load(self) -> CdMartSnapshot:
        return CdMartSnapshot(
            brand_links=self.brand_links,
            market_series=self.market_series,
            loaded_at=time.monotonic(),
        )


@dataclass(frozen=True, slots=True)
class MariaDbCdMartReader:
    host: str = os.environ.get("CHAT_CACHE_DB_HOST", "llmops-mariadb-service.llmops.svc.cluster.local")
    port: int = int(os.environ.get("CHAT_CACHE_DB_PORT", "3306"))
    database: str = os.environ.get("CHAT_CACHE_DB_NAME", "jw_mart")
    schema: str = os.environ.get("CHAT_CD_MART_SCHEMA", "")
    user: str = os.environ.get("CHAT_CACHE_DB_USER", "llmops")
    password: str = os.environ.get("CHAT_CACHE_DB_PASSWORD", "")
    connect_timeout_s: int = int(os.environ.get("CHAT_CACHE_DB_CONNECT_TIMEOUT_S", "3"))
    read_timeout_s: int = int(os.environ.get("CHAT_CD_MART_DB_READ_TIMEOUT_S", "15"))

    def load(self) -> CdMartSnapshot:
        import pymysql

        prefix = f"{_quote_identifier(self.schema)}." if self.schema else ""
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
                    f"""
                    SELECT cd_market_id, source, market_size_series
                    FROM {prefix}`mart_strategic_cd_market_metric`
                    WHERE measure='sales'
                      AND source IN ('ubist', 'iqvia_nsa')
                    """
                )
                market_rows = cursor.fetchall()
                cursor.execute(
                    f"""
                    SELECT brand_name, cd_market_id, source, overlay_data
                    FROM {prefix}`mart_strategic_cd_brand_metric`
                    WHERE measure='sales'
                      AND source IN ('ubist', 'iqvia_nsa')
                    """
                )
                brand_rows = cursor.fetchall()

        market_series: dict[tuple[str, str], dict[str, Any]] = {}
        for row in market_rows:
            series = _loads(row.get("market_size_series"))
            if series:
                market_series[(str(row["cd_market_id"]), str(row["source"]))] = series

        brand_links = tuple(
            CdBrandLink(
                brand_name=str(row["brand_name"]),
                cd_market_id=str(row["cd_market_id"]),
                source=str(row["source"]),
                ml_id=str(_loads(row.get("overlay_data")).get("ml_id") or ""),
            )
            for row in brand_rows
        )
        return CdMartSnapshot(brand_links=brand_links, market_series=market_series, loaded_at=time.monotonic())


class TtlCdMartCache:
    def __init__(self, reader: CdMartReader, ttl_seconds: int) -> None:
        self._reader = reader
        self._ttl_seconds = ttl_seconds
        self._snapshot: CdMartSnapshot | None = None

    def snapshot(self) -> CdMartSnapshot:
        current = self._snapshot
        now = time.monotonic()
        if current is None or now - current.loaded_at > self._ttl_seconds:
            current = self._reader.load()
            self._snapshot = current
        return current


def series_with_yoy(series: dict[str, Any]) -> dict[str, dict[str, float | None]]:
    """Attach YoY to a raw {period: value} series.

    Mirrors the retired cause-cache builder semantics: 12-point lag for monthly
    periods, 4-point lag when every period is quarterly, pct rounded to 4 places.
    """

    periods = sorted(str(period) for period in series)
    step = 12 if any("-Q" not in period for period in periods) else 4
    output: dict[str, dict[str, float | None]] = {}
    for index, period in enumerate(periods):
        value = _number(series.get(period))
        yoy: float | None = None
        if index >= step:
            previous = _number(series.get(periods[index - step]))
            if value is not None and previous not in (None, 0):
                yoy = round((value - previous) / previous * 100, 4)
        output[period] = {"value": value, "yoy_growth_pct": yoy}
    return output


def mart_source_key(value: str) -> str:
    lowered = str(value or "").lower()
    if "iqvia" in lowered:
        return "iqvia_nsa"
    return "ubist"


def strategy_id_from_ml(ml_id: str) -> str:
    match = re.search(r"(\d+)$", str(ml_id or ""))
    return f"strategy_{int(match.group(1)):03d}" if match else str(ml_id or "")


def _quote_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return f"`{value}`"


def _loads(value: Any) -> dict[str, Any]:
    parsed = json.loads(value) if isinstance(value, str) and value else value
    return parsed if isinstance(parsed, dict) else {}


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None
