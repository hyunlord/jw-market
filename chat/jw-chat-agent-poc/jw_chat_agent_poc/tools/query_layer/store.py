from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
import json
import logging
import math
import os
import threading
import time
from typing import Any, Final, Protocol

from jw_chat_agent_poc.agentic.sales_filter_aliases import normalise_channel_data


logger = logging.getLogger(__name__)
startup_timing_logger = logging.getLogger("uvicorn.error")

FAILED_VALUE_STATUSES: Final[frozenset[str]] = frozenset(
    {"query_failed", "mapping_failed", "incomplete_split", "missing", "error"}
)


@dataclass(frozen=True, slots=True)
class MartRecord:
    """One read-only strategic mart brand row."""

    ml_id: str
    brand_name: str
    source: str
    measure: str
    metric_history: dict[str, dict[str, Any]]
    channel_data: dict[str, Any]
    specialty_data: dict[str, Any]
    dimension_data: dict[str, Any]
    by_dimension: dict[str, Any]
    unit_label: str = ""

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "MartRecord":
        return cls(
            ml_id=str(row["ml_id"]),
            brand_name=str(row["brand_name"]),
            source=str(row["source"]),
            measure=str(row["measure"]),
            metric_history=_loads(row.get("metric_history")),
            channel_data=normalise_channel_data(_loads(row.get("channel_data"))),
            specialty_data=_loads(row.get("specialty_data")),
            dimension_data=_loads(row.get("dimension_data")),
            by_dimension=_loads(row.get("by_dimension")),
            unit_label=str(row.get("unit_label") or ""),
        )

    def company(self) -> str:
        return str(self.by_dimension.get("company") or self.by_dimension.get("raw_company") or "")

    def molecule(self) -> str:
        return str(self.by_dimension.get("molecule") or "")

    def class_label(self) -> str:
        return str(self.by_dimension.get("class") or "")

    def class_1(self) -> str:
        return str(self.by_dimension.get("class_1") or "")

    def class_2(self) -> str:
        return str(self.by_dimension.get("class_2") or "")

    def dosage_form(self) -> str:
        return str(self.by_dimension.get("dosage_form") or "")

    def nhi_type(self) -> str:
        return str(self.by_dimension.get("nhi_type") or "")

    def ox_gx(self) -> str:
        return str(self.by_dimension.get("ox_gx") or "")


@dataclass(frozen=True, slots=True)
class MartSnapshot:
    """In-memory strategic mart snapshot used for deterministic query calculations."""

    records: tuple[MartRecord, ...]
    loaded_at: float
    derived: Any = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        from jw_chat_agent_poc.tools.query_layer.derived import DerivedSnapshotIndex

        object.__setattr__(self, "derived", DerivedSnapshotIndex.build(self))

    def market_id_for_brand(self, brand: str) -> str | None:
        markets = self.market_ids_for_brand(brand)
        return markets[0] if len(markets) == 1 else None

    def market_ids_for_brand(self, brand: str) -> tuple[str, ...]:
        return tuple(sorted({record.ml_id for record in self.records if record.brand_name == brand}))

    def source_for_market(self, market_id: str) -> str:
        sources = self.sources_for_market(market_id)
        if "ubist" in sources:
            return "ubist"
        return sources[0] if sources else "ubist"

    def sources_for_market(self, market_id: str) -> tuple[str, ...]:
        return tuple(sorted({record.source for record in self.records if record.ml_id == market_id}))

    def sources_for_brand(self, market_id: str, brand: str, measure: str = "sales") -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    record.source
                    for record in self.records
                    if record.ml_id == market_id and record.brand_name == brand and record.measure == measure
                }
            )
        )

    def market_records(self, market_id: str, source: str = "ubist", measure: str = "sales") -> tuple[MartRecord, ...]:
        source_key = _source_key(source)
        return tuple(
            record
            for record in self.records
            if record.ml_id == market_id and record.source == source_key and record.measure == measure
        )

    def record(self, market_id: str, brand: str, source: str = "ubist", measure: str = "sales") -> MartRecord:
        for record in self.market_records(market_id, source, measure):
            if record.brand_name == brand:
                return record
        raise LookupError(f"mart brand not found: market={market_id} source={source} brand={brand}")

    def periods(self, market_id: str, source: str = "ubist", measure: str = "sales") -> tuple[str, ...]:
        values: set[str] = set()
        for record in self.market_records(market_id, source, measure):
            values.update(record.metric_history)
        return tuple(sorted(values))

    def latest_period(self, market_id: str, source: str = "ubist", measure: str = "sales") -> str:
        periods = self.periods(market_id, source, measure)
        if not periods:
            raise LookupError(f"mart periods missing: market={market_id} source={source}")
        return periods[-1]

    def latest_valid_period(self, record: MartRecord) -> str | None:
        periods = tuple(sorted(period for period in record.metric_history if self.value_or_none(record, period) is not None))
        return periods[-1] if periods else None

    def value_status(self, record: MartRecord, period: str) -> str:
        if len(period) == 4 and period.isdigit():
            statuses = tuple(
                self.value_status(record, key)
                for key in sorted(record.metric_history)
                if key.startswith(f"{period}-")
            )
            if not statuses:
                return "missing"
            if any(status in FAILED_VALUE_STATUSES for status in statuses):
                return next(status for status in statuses if status in FAILED_VALUE_STATUSES)
            return "OK"
        quarter_months = _quarter_months(period)
        if quarter_months and period not in record.metric_history:
            statuses = tuple(self.value_status(record, month) for month in quarter_months)
            if any(status in FAILED_VALUE_STATUSES for status in statuses):
                return next(status for status in statuses if status in FAILED_VALUE_STATUSES)
            return "OK"
        row = record.metric_history.get(period)
        if not isinstance(row, dict):
            return "missing"
        raw_value = row.get("raw_value")
        if isinstance(raw_value, int | float) and not math.isfinite(float(raw_value)):
            return "missing"
        return _row_status(row)

    def value_or_none(self, record: MartRecord, period: str) -> float | None:
        if len(period) == 4 and period.isdigit():
            matching_periods = [key for key in sorted(record.metric_history) if key.startswith(f"{period}-")]
            values = [self.value_or_none(record, key) for key in matching_periods]
            return sum(value for value in values if value is not None) if values and all(value is not None for value in values) else None
        quarter_months = _quarter_months(period)
        if quarter_months and period not in record.metric_history:
            values = tuple(self.value_or_none(record, month) for month in quarter_months)
            return sum(value for value in values if value is not None) if all(value is not None for value in values) else None
        row = record.metric_history.get(period)
        if not isinstance(row, dict) or _row_status(row) in FAILED_VALUE_STATUSES:
            return None
        value = row.get("raw_value")
        if not isinstance(value, int | float):
            return None
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None

    def value(self, record: MartRecord, period: str) -> float | None:
        return self.value_or_none(record, period)

    def market_value_or_none(self, market_id: str, period: str, source: str = "ubist", measure: str = "sales") -> float | None:
        records = self.market_records(market_id, source, measure)
        values = [self.value_or_none(record, period) for record in records]
        return sum(value for value in values if value is not None) if values and all(value is not None for value in values) else None

    def market_value(self, market_id: str, period: str, source: str = "ubist", measure: str = "sales") -> float | None:
        return self.market_value_or_none(market_id, period, source, measure)

    def share_or_none(self, market_id: str, record: MartRecord, period: str, source: str = "ubist", measure: str = "sales") -> float | None:
        value = self.value_or_none(record, period)
        if value is None:
            return None
        stored_share = None
        if not (len(period) == 4 and period.isdigit()):
            row = record.metric_history.get(period)
            if isinstance(row, dict):
                share = row.get("ms")
                if isinstance(share, int | float):
                    numeric_share = float(share)
                    stored_share = numeric_share if math.isfinite(numeric_share) else None
        total = self.market_value_or_none(market_id, period, source, measure)
        if total is None or total == 0:
            return None
        if stored_share is not None:
            return stored_share
        return value / total * 100

    def share(self, market_id: str, record: MartRecord, period: str, source: str = "ubist", measure: str = "sales") -> float | None:
        return self.share_or_none(market_id, record, period, source, measure)

    def rank(self, market_id: str, brand: str, period: str, source: str = "ubist", measure: str = "sales") -> int | None:
        rows = self.ranked_brands(market_id, period, source, measure)
        for row in rows:
            if row["brand"] == brand:
                return int(row["rank"])
        return None

    def ranked_brands(self, market_id: str, period: str, source: str = "ubist", measure: str = "sales") -> list[dict[str, Any]]:
        records = self.market_records(market_id, source, measure)
        total = self.market_value_or_none(market_id, period, source, measure)
        rows = []
        for record in records:
            value = self.value_or_none(record, period)
            if value is None:
                continue
            share = None
            if total is not None and total != 0:
                if not (len(period) == 4 and period.isdigit()):
                    period_row = record.metric_history.get(period)
                    if isinstance(period_row, dict) and isinstance(period_row.get("ms"), int | float):
                        numeric_share = float(period_row["ms"])
                        share = numeric_share if math.isfinite(numeric_share) else None
                if share is None:
                    share = value / total * 100
            rows.append(
                {
                    "brand": record.brand_name,
                    "value": value,
                    "source_status": self.value_status(record, period),
                    "ms_recent_pct": share,
                    "company": record.company(),
                    "molecule": record.molecule(),
                    "measure": measure,
                    "unit_label": record.unit_label or ("Rx" if measure == "volume" else "KRW"),
                }
            )
        rows.sort(key=lambda item: item["value"], reverse=True)
        for index, row in enumerate(rows, start=1):
            row["rank"] = index
        return rows

    def brand_series(self, market_id: str, brand: str, periods: Iterable[str], source: str = "ubist", measure: str = "sales") -> list[dict[str, Any]]:
        record = self.record(market_id, brand, source, measure)
        rows: list[dict[str, Any]] = []
        for period in periods:
            value = self.value_or_none(record, period)
            ranked_row = None
            if value is not None:
                ranked_row = next(
                    (
                        row
                        for row in self.ranked_brands(market_id, period, source, measure)
                        if row["brand"] == brand
                    ),
                    None,
                )
            row: dict[str, Any] = {
                "period": period,
                "value": value,
                "ms_pct": ranked_row["ms_recent_pct"] if ranked_row is not None else None,
                "rank": int(ranked_row["rank"]) if ranked_row is not None else None,
                "source_status": self.value_status(record, period),
                "measure": measure,
                "unit_label": record.unit_label or ("Rx" if measure == "volume" else "KRW"),
            }
            if measure == "sales":
                row.update(
                    {
                        "value_krw": value,
                        "value_억원": round(value / 100_000_000, 2) if value is not None else None,
                    }
                )
            else:
                row["prescription_volume"] = value
            rows.append(row)
        return rows

    def market_series(self, market_id: str, periods: Iterable[str], source: str = "ubist", measure: str = "sales") -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for period in periods:
            value = self.market_value_or_none(market_id, period, source, measure)
            row: dict[str, Any] = {"period": period, "value": value, "measure": measure}
            if measure == "sales":
                row.update(
                    {
                        "value_krw": value,
                        "value_억원": round(value / 100_000_000, 2) if value is not None else None,
                    }
                )
            else:
                row.update({"prescription_volume": value, "unit_label": "Rx"})
            rows.append(row)
        return rows

    def hhi(self, market_id: str, period: str, source: str = "ubist", measure: str = "sales") -> float | None:
        shares = [row["ms_recent_pct"] for row in self.ranked_brands(market_id, period, source, measure) if row["ms_recent_pct"] is not None]
        return sum(float(share) ** 2 for share in shares) if shares else None


def _quarter_months(period: str) -> tuple[str, ...]:
    if len(period) != 7 or period[4:6] != "-Q" or period[-1] not in "1234" or not period[:4].isdigit():
        return ()
    first_month = (int(period[-1]) - 1) * 3 + 1
    return tuple(f"{period[:4]}-{month:02d}" for month in range(first_month, first_month + 3))


class StrategicMartReader(Protocol):
    def load(self) -> MartSnapshot: ...


@dataclass(frozen=True, slots=True)
class StaticStrategicMartReader:
    records: tuple[MartRecord, ...]

    def load(self) -> MartSnapshot:
        return MartSnapshot(self.records, time.monotonic())


@dataclass(frozen=True, slots=True)
class MariaDbStrategicMartReader:
    host: str = os.environ.get("CHAT_QUERY_DB_HOST") or os.environ.get("CHAT_CACHE_DB_HOST", "llmops-mariadb-service.llmops.svc.cluster.local")
    port: int = int(os.environ.get("CHAT_QUERY_DB_PORT") or os.environ.get("CHAT_CACHE_DB_PORT", "3306"))
    database: str = os.environ.get("CHAT_QUERY_DB_NAME") or os.environ.get("CHAT_CACHE_DB_NAME", "jw_mart")
    user: str = os.environ.get("CHAT_QUERY_DB_USER") or os.environ.get("CHAT_CACHE_DB_USER", "llmops")
    password: str = os.environ.get("CHAT_QUERY_DB_PASSWORD") or os.environ.get("CHAT_CACHE_DB_PASSWORD", "")
    table: str = os.environ.get("CHAT_QUERY_MART_TABLE", "mart_strategic_ml_brand_metric")
    connect_timeout_s: int = int(os.environ.get("CHAT_QUERY_DB_CONNECT_TIMEOUT_S", "3"))
    read_timeout_s: int = int(os.environ.get("CHAT_QUERY_DB_READ_TIMEOUT_S", "15"))

    def load(self) -> MartSnapshot:
        import pymysql

        started_at = time.monotonic()
        sql = f"""
            SELECT ml_id, brand_name, source, measure, unit_label,
                   metric_history, channel_data, specialty_data, dimension_data, by_dimension
            FROM {self.table}
            WHERE (measure='sales' AND source IN ('ubist', 'iqvia_nsa'))
               OR (measure='volume' AND source='ubist')
        """
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
                cursor.execute(sql)
                rows = cursor.fetchall()
        query_completed_at = time.monotonic()
        records = tuple(MartRecord.from_row(dict(row)) for row in rows)
        deserialization_completed_at = time.monotonic()
        snapshot = MartSnapshot(records, time.monotonic())
        completed_at = time.monotonic()
        startup_timing_logger.info(
            "strategic mart snapshot load stages snapshot_query_s=%.3f "
            "deserialization_s=%.3f build_s=%.3f total_s=%.3f records=%d",
            query_completed_at - started_at,
            deserialization_completed_at - query_completed_at,
            completed_at - deserialization_completed_at,
            completed_at - started_at,
            len(records),
        )
        return snapshot


class TtlStrategicMartStore:
    def __init__(
        self,
        reader: StrategicMartReader,
        ttl_seconds: int = 300,
        prewarm: bool = True,
    ) -> None:
        self._reader = reader
        self._ttl_seconds = ttl_seconds
        self._snapshot: MartSnapshot | None = None
        self._snapshot_lock = threading.Lock()
        self._load_lock = threading.Lock()
        self._refreshing = False
        if prewarm:
            self.prewarm()

    def prewarm(self) -> None:
        thread = threading.Thread(
            target=self._prewarm_snapshot,
            name="strategic-mart-prewarm",
            daemon=True,
        )
        thread.start()

    def _prewarm_snapshot(self) -> None:
        started_at = time.monotonic()
        try:
            snapshot = self._load_cold_snapshot()
        except Exception:  # noqa: BLE001 - background prewarm must not break requests
            logger.exception("strategic mart snapshot prewarm failed")
            return
        logger.info(
            "strategic mart snapshot prewarmed",
            extra={
                "elapsed_s": round(time.monotonic() - started_at, 3),
                "records": len(snapshot.records),
            },
        )

    def snapshot(self) -> MartSnapshot:
        with self._snapshot_lock:
            current = self._snapshot
            now = time.monotonic()
            if current is not None:
                if now - current.loaded_at > self._ttl_seconds:
                    self._start_refresh_locked()
                return current
        return self._load_cold_snapshot()

    def _load_cold_snapshot(self) -> MartSnapshot:
        with self._load_lock:
            with self._snapshot_lock:
                current = self._snapshot
                if current is not None:
                    return current
            current = self._reader.load()
            with self._snapshot_lock:
                self._snapshot = current
            return current

    def _start_refresh_locked(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        thread = threading.Thread(
            target=self._refresh_snapshot,
            name="strategic-mart-ttl-refresh",
            daemon=True,
        )
        thread.start()

    def _refresh_snapshot(self) -> None:
        started_at = time.monotonic()
        startup_timing_logger.info("strategic mart TTL refresh started")
        try:
            snapshot = self._reader.load()
        except Exception:  # noqa: BLE001 - refresh failure must preserve the serving snapshot
            with self._snapshot_lock:
                self._refreshing = False
            logger.exception("strategic mart TTL refresh failed")
            return
        with self._snapshot_lock:
            self._snapshot = snapshot
            self._refreshing = False
        startup_timing_logger.info(
            "strategic mart TTL refresh completed elapsed_s=%.3f records=%d",
            time.monotonic() - started_at,
            len(snapshot.records),
        )


_SHARED_STORE_LOCK = threading.Lock()
_SHARED_MART_STORES: dict[int, TtlStrategicMartStore] = {}


def shared_strategic_mart_store(ttl_seconds: int = 300) -> TtlStrategicMartStore:
    """Return the process-wide mart snapshot store for the requested TTL."""
    with _SHARED_STORE_LOCK:
        store = _SHARED_MART_STORES.get(ttl_seconds)
        if store is None:
            store = TtlStrategicMartStore(
                MariaDbStrategicMartReader(),
                ttl_seconds=ttl_seconds,
            )
            _SHARED_MART_STORES[ttl_seconds] = store
        return store


def _loads(value: Any) -> dict[str, Any]:
    parsed = json.loads(value) if isinstance(value, str) and value else value
    return parsed if isinstance(parsed, dict) else {}


def _row_status(row: dict[str, Any]) -> str:
    raw = row.get("source_status", row.get("status"))
    status = str(raw or "OK")
    return status if status in FAILED_VALUE_STATUSES else "OK"


def _source_key(value: str) -> str:
    lowered = str(value or "").lower()
    if lowered in {"iqvia", "iqvia_nsa"}:
        return "iqvia_nsa"
    return "ubist"
