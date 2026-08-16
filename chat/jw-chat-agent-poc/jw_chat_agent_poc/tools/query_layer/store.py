from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from functools import wraps
import json
import logging
import math
import os
import threading
import time
from typing import Any, Final, Protocol

from jw_chat_agent_poc.agentic.sales_filter_aliases import normalise_channel_data
from jw_chat_agent_poc.tools.query_layer.mart_json import compact_mart_json


logger = logging.getLogger(__name__)
startup_timing_logger = logging.getLogger("uvicorn.error")

FAILED_VALUE_STATUSES: Final[frozenset[str]] = frozenset(
    {"query_failed", "mapping_failed", "incomplete_split", "missing", "error"}
)


@dataclass(slots=True)
class SnapshotMemo:
    """Answers already derived from one snapshot, kept beside that snapshot.

    Counters are reported so a hit is never taken silently. ``entries`` is
    exact; ``hits`` and ``misses`` are incremented without a lock because the
    lookups they count run on every mart row of every worker thread, and taking
    a lock there would cost more than the recomputation being avoided. Under
    concurrent readers they are therefore a lower bound, which is stated here
    rather than presented as an exact count.
    """

    # Off until the snapshot finishes assembling itself. Building the derived
    # index walks every row and period exactly once, so recording that pass
    # cannot save a later one -- it would only hold a million entries that never
    # get a hit. Staying off also means a snapshot never keeps an answer it
    # worked out about itself before it was finished.
    armed: bool = False
    table: dict[tuple[Any, ...], Any] = field(default_factory=dict)
    # Objects a key was derived from by identity. Holding them here keeps those
    # identities from being recycled under a key that is still in the table.
    # Keyed by identity so one row costs one reference no matter how many of its
    # periods are in the table -- a list would hold a million duplicates.
    retained: dict[int, Any] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0

    def lookups_recorded(self) -> int:
        return self.hits + self.misses

    def observability(self) -> dict[str, int | float]:
        total = self.hits + self.misses
        return {
            "lookups": total,
            "hits": self.hits,
            "misses": self.misses,
            "entries": len(self.table),
            "hit_ratio": round(self.hits / total, 4) if total else 0.0,
        }


def _memoised_on_snapshot(
    key: Callable[..., tuple[Any, ...]],
    *,
    detach: Callable[[Any], Any] | None = None,
    retain: Callable[..., Any] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Derive a pure snapshot answer once per distinct argument tuple.

    The decorated methods read nothing but ``self.records``, which is frozen for
    the life of the snapshot, so the same arguments cannot produce a different
    answer. A TTL refresh does not mutate a snapshot; it builds a new one, whose
    memo starts empty. A stale answer therefore has no path to a caller: the
    cache cannot outlive the data it was derived from.

    ``key`` must name every argument that selects rows -- dropping ``source`` or
    ``measure`` would let a UBIST question read an IQVIA answer. ``detach`` is
    for methods that hand back a structure the caller goes on to annotate; it
    returns a private copy so one caller's edit cannot reach the next.
    ``retain`` names an argument the key identifies by object identity, so the
    memo can hold it and keep that identity from being reused.
    """

    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        name = function.__name__

        @wraps(function)
        def wrapper(self: "MartSnapshot", *args: Any, **kwargs: Any) -> Any:
            memo = self.memo
            if not memo.armed:
                return function(self, *args, **kwargs)
            entry_key = (name, *key(*args, **kwargs))
            try:
                cached = memo.table[entry_key]
            except KeyError:
                memo.misses += 1
                value = function(self, *args, **kwargs)
                if retain is not None:
                    retained = retain(*args, **kwargs)
                    memo.retained[id(retained)] = retained
                memo.table[entry_key] = value
                return detach(value) if detach is not None else value
            memo.hits += 1
            return detach(cached) if detach is not None else cached

        wrapper.__wrapped_uncached__ = function  # type: ignore[attr-defined]
        return wrapper

    return decorate


def _record_key(record: "MartRecord", period: str) -> tuple[Any, ...]:
    """Identify the row itself, not a description of it.

    ``MartRecord`` carries dicts and so is unhashable, and two rows can share
    ml_id/brand/source/measure while holding different histories, which would
    make a descriptive key conflate them. Identity avoids both problems; the
    memo retains the record (see ``retain``) so the identity behind a live key
    cannot be handed to a different object.
    """
    return (id(record), period)


def _first_argument(record: "MartRecord", _period: str) -> "MartRecord":
    return record


@dataclass(frozen=True, slots=True)
class MartRecord:
    """One read-only strategic mart brand row."""

    ml_id: str
    brand_name: str
    source: str
    measure: str
    metric_history: Mapping[str, Any]
    channel_data: Mapping[str, Any]
    specialty_data: Mapping[str, Any]
    dimension_data: Mapping[str, Any]
    by_dimension: Mapping[str, Any]
    unit_label: str = ""

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "MartRecord":
        return cls(
            ml_id=str(row["ml_id"]),
            brand_name=str(row["brand_name"]),
            source=str(row["source"]),
            measure=str(row["measure"]),
            metric_history=_loads(row.get("metric_history"), column="metric_history"),
            channel_data=normalise_channel_data(
                _loads(row.get("channel_data"), column="channel_data")
            ),
            specialty_data=_loads(row.get("specialty_data"), column="specialty_data"),
            dimension_data=_loads(row.get("dimension_data"), column="dimension_data"),
            by_dimension=_loads(row.get("by_dimension"), column="by_dimension"),
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
    memo: SnapshotMemo = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        from jw_chat_agent_poc.tools.query_layer.derived import DerivedSnapshotIndex

        object.__setattr__(self, "memo", SnapshotMemo())
        object.__setattr__(self, "derived", DerivedSnapshotIndex.build(self))
        # Only now, with every row in place and the derived index built, does
        # an answer about this snapshot become one worth keeping.
        self.memo.armed = True

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

    @_memoised_on_snapshot(
        lambda market_id, source="ubist", measure="sales": (market_id, source, measure)
    )
    def market_records(self, market_id: str, source: str = "ubist", measure: str = "sales") -> tuple[MartRecord, ...]:
        source_key = _source_key(source)
        return tuple(
            record
            for record in self.records
            if record.ml_id == market_id and record.source == source_key and record.measure == measure
        )

    @_memoised_on_snapshot(
        lambda market_id, brand, source="ubist", measure="sales": (market_id, brand, source, measure)
    )
    def record(self, market_id: str, brand: str, source: str = "ubist", measure: str = "sales") -> MartRecord:
        for record in self.market_records(market_id, source, measure):
            if record.brand_name == brand:
                return record
        raise LookupError(f"mart brand not found: market={market_id} source={source} brand={brand}")

    @_memoised_on_snapshot(
        lambda market_id, source="ubist", measure="sales": (market_id, source, measure)
    )
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

    @_memoised_on_snapshot(_record_key, retain=_first_argument)
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
        if not isinstance(row, Mapping):
            return "missing"
        raw_value = row.get("raw_value")
        if isinstance(raw_value, int | float) and not math.isfinite(float(raw_value)):
            return "missing"
        return _row_status(row)

    @_memoised_on_snapshot(_record_key, retain=_first_argument)
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
        if not isinstance(row, Mapping) or _row_status(row) in FAILED_VALUE_STATUSES:
            return None
        value = row.get("raw_value")
        if not isinstance(value, int | float):
            return None
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None

    def value(self, record: MartRecord, period: str) -> float | None:
        return self.value_or_none(record, period)

    @_memoised_on_snapshot(
        lambda market_id, period, source="ubist", measure="sales": (market_id, period, source, measure)
    )
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
            if isinstance(row, Mapping):
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

    @_memoised_on_snapshot(
        lambda market_id, period, source="ubist", measure="sales": (market_id, period, source, measure),
        # Callers annotate the rows they get back, so every caller is handed its
        # own rows rather than the ones held in the memo.
        detach=lambda rows: [dict(row) for row in rows],
    )
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
                    if isinstance(period_row, Mapping) and isinstance(period_row.get("ms"), int | float):
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


MART_TTL_ENV: Final = "CHAT_QUERY_MART_TTL_SECONDS"
DEFAULT_MART_TTL_SECONDS: Final = 300


def mart_ttl_seconds() -> int:
    """Resolve the process-wide mart snapshot TTL.

    Every caller has to agree on this number. The shared store used to be keyed by
    whatever TTL the first caller passed, and the three call sites read three
    different things (``CHAT_QUERY_MART_TTL_SECONDS``,
    ``CHAT_MARKET_SCOPE_TTL_SECONDS``, and a hardcoded default). They all resolve to
    300 today, so one store exists -- but raising only the mart TTL would have minted
    a second store, and a second store means a second multi-GiB snapshot resident for
    the life of the process. Reading the TTL in one place makes that unreachable.
    """
    raw = os.environ.get(MART_TTL_ENV)
    if raw is None:
        return DEFAULT_MART_TTL_SECONDS
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_MART_TTL_SECONDS
    return parsed if parsed > 0 else DEFAULT_MART_TTL_SECONDS


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

    def fingerprint(self) -> str | None:
        """Cheap probe describing the serving rows, or None when it cannot be taken.

        Rebuilding the snapshot costs 32-38s and holds a second full copy while it
        runs. This answers "did the rows change at all?" in ~40ms, so the expensive
        rebuild only has to happen when the answer is yes. None means "unknown", and
        the caller must then fall back to a full rebuild.
        """
        import pymysql

        sql = f"""
            SELECT COUNT(*) AS row_count, MAX(computed_at) AS max_computed_at
            FROM {self.table}
            WHERE (measure='sales' AND source IN ('ubist', 'iqvia_nsa'))
               OR (measure='volume' AND source='ubist')
        """
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
                    cursor.execute(sql)
                    row = cursor.fetchone()
        except Exception:  # noqa: BLE001 - an unavailable probe must not block refresh
            logger.exception("strategic mart fingerprint probe failed")
            return None
        if not row:
            return None
        return f"{row.get('row_count')}|{row.get('max_computed_at')}"

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
        self._refresh_successes = 0
        self._refresh_failures = 0
        self._refresh_skips = 0
        # Freshness lives here rather than on MartSnapshot: the snapshot is frozen and
        # its __post_init__ rebuilds DerivedSnapshotIndex, so replacing it just to carry
        # a new timestamp would re-spend the build this change exists to avoid. The TTL
        # comparison below was the only reader of snapshot.loaded_at.
        self._loaded_at = 0.0
        self._fingerprint: str | None = None
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
                if now - self._loaded_at > self._ttl_seconds:
                    self._start_refresh_locked()
                return current
        return self._load_cold_snapshot()

    def _load_cold_snapshot(self) -> MartSnapshot:
        with self._load_lock:
            with self._snapshot_lock:
                current = self._snapshot
                if current is not None:
                    return current
            probe = self._probe_fingerprint()
            try:
                current = self._reader.load()
            except Exception:
                with self._snapshot_lock:
                    self._refresh_failures += 1
                raise
            with self._snapshot_lock:
                self._snapshot = current
                self._loaded_at = time.monotonic()
                self._fingerprint = probe
                self._refresh_successes += 1
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

    def _probe_fingerprint(self) -> str | None:
        """Ask the reader for a cheap change signal, if it offers one.

        Test doubles only implement load(), so this stays optional: no fingerprint
        means every refresh rebuilds, which is exactly the previous behaviour.
        """
        probe = getattr(self._reader, "fingerprint", None)
        if not callable(probe):
            return None
        try:
            return probe()
        except Exception:  # noqa: BLE001 - a failed probe must not block the refresh
            logger.exception("strategic mart fingerprint probe raised")
            return None

    def _refresh_snapshot(self) -> None:
        started_at = time.monotonic()
        startup_timing_logger.info("strategic mart TTL refresh started")

        # Rebuilding holds the new snapshot alongside the one still being served, so for
        # the 32-38s the load takes the process carries two full copies -- ~9.2GiB against
        # a 10Gi limit. Ask first whether the rows actually changed; when they have not,
        # extending freshness is equivalent to rebuilding and costs no memory. A None
        # probe means "unknown" and falls through to the rebuild, preserving the original
        # behaviour. Skips are counted separately so refresh_successes keeps meaning
        # "a rebuild completed" instead of quietly flattening to zero.
        probe = self._probe_fingerprint()
        if probe is not None:
            with self._snapshot_lock:
                unchanged = self._snapshot is not None and probe == self._fingerprint
                if unchanged:
                    self._loaded_at = time.monotonic()
                    self._refresh_skips += 1
                    self._refreshing = False
            if unchanged:
                startup_timing_logger.info(
                    "strategic mart TTL refresh skipped unchanged elapsed_s=%.3f",
                    time.monotonic() - started_at,
                )
                return

        try:
            snapshot = self._reader.load()
        except Exception:  # noqa: BLE001 - refresh failure must preserve the serving snapshot
            with self._snapshot_lock:
                self._refreshing = False
                self._refresh_failures += 1
            logger.exception("strategic mart TTL refresh failed")
            return
        with self._snapshot_lock:
            self._snapshot = snapshot
            self._loaded_at = time.monotonic()
            self._fingerprint = probe
            self._refreshing = False
            self._refresh_successes += 1
        startup_timing_logger.info(
            "strategic mart TTL refresh completed elapsed_s=%.3f records=%d",
            time.monotonic() - started_at,
            len(snapshot.records),
        )

    def observability(self) -> dict[str, int | float | bool | None]:
        with self._snapshot_lock:
            snapshot = self._snapshot
            if snapshot is None:
                row_count = 0
                market_point_count = 0
                brand_point_count = 0
                snapshot_age_seconds = None
                # Reported even when there is no snapshot, so an absent memo is
                # told apart from a memo that was never consulted.
                memo = SnapshotMemo().observability()
            else:
                row_count = len(snapshot.records)
                market_point_count = len(snapshot.derived.market_points)
                brand_point_count = len(snapshot.derived.brand_points)
                snapshot_age_seconds = round(max(0.0, time.monotonic() - snapshot.loaded_at), 3)
                memo = snapshot.memo.observability()
            return {
                "row_count": row_count,
                "derived_point_count": market_point_count + brand_point_count,
                "market_point_count": market_point_count,
                "brand_point_count": brand_point_count,
                "snapshot_age_seconds": snapshot_age_seconds,
                "refresh_successes": self._refresh_successes,
                "refresh_failures": self._refresh_failures,
                "refresh_skips": self._refresh_skips,
                "refreshing": self._refreshing,
                "snapshot_memo_lookups": memo["lookups"],
                "snapshot_memo_hits": memo["hits"],
                "snapshot_memo_misses": memo["misses"],
                "snapshot_memo_entries": memo["entries"],
                "snapshot_memo_hit_ratio": memo["hit_ratio"],
            }


_SHARED_STORE_LOCK = threading.Lock()
_SHARED_MART_STORE: TtlStrategicMartStore | None = None


def shared_strategic_mart_store() -> TtlStrategicMartStore:
    """Return the one process-wide mart snapshot store.

    A single slot, not a per-TTL mapping. The snapshot is several GiB, so holding two
    of them is not a caching trade-off -- it is the difference between running and
    being OOM-killed. The TTL comes from mart_ttl_seconds() so no caller can mint a
    second store by passing a different number.
    """
    global _SHARED_MART_STORE
    with _SHARED_STORE_LOCK:
        if _SHARED_MART_STORE is None:
            _SHARED_MART_STORE = TtlStrategicMartStore(
                MariaDbStrategicMartReader(),
                ttl_seconds=mart_ttl_seconds(),
            )
        return _SHARED_MART_STORE


def _loads(value: Any, *, column: str) -> dict[str, Any]:
    parsed = json.loads(value) if isinstance(value, str) and value else value
    return compact_mart_json(parsed, column=column) if isinstance(parsed, dict) else {}


def _row_status(row: Mapping[str, Any]) -> str:
    raw = row.get("source_status", row.get("status"))
    status = str(raw or "OK")
    return status if status in FAILED_VALUE_STATUSES else "OK"


def _source_key(value: str) -> str:
    """Map a requested source label onto its stored key, rejecting unknown labels.

    An unrecognised label used to fall through to "ubist", so a request for a source that
    does not exist came back as a full, plausible set of UBIST rows. A trailing-padded
    IQVIA label was worse still: this function had no strip() and so folded it to ubist,
    while render.source_label matches on startswith("iqvia"), which trailing padding does
    not disturb - "iqvia_nsa " therefore returned UBIST rows under an IQVIA heading.
    Unknown labels now raise, and stripping removes the disagreement between the two rules.

    The message keeps the "source not found" wording that _query_failure_reason already
    maps to QueryFailureReason.SOURCE_ABSENT, so the tool facade reports a typed
    reason_code without any change to the caller.
    """
    normalized = str(value or "").strip().lower()
    if normalized in {"iqvia", "iqvia_nsa"}:
        return "iqvia_nsa"
    if normalized == "ubist":
        return "ubist"
    raise ValueError(f"mart source not found: source={value!r}")
