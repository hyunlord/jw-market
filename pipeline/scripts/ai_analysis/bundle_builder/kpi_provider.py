from __future__ import annotations

from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from threading import RLock
from typing import Any, Protocol

from pipeline.scripts.api import db as api_db
from pipeline.scripts.api.dynamic_market.aggregator import MetricAggregator
from pipeline.scripts.api.dynamic_market.composer import ResponseComposer
from pipeline.scripts.api.dynamic_market.resolvers import GeneralViewResolver
from pipeline.scripts.api.dynamic_market.types import BrandMetric, PeriodRange

from .catalog_db_loader import source_public_to_db
from .market_kpi_calculator import calculate_ml_kpi_extras
from .mart_metric_reader import fetch_ml_metric_rows


class KpiProvider(Protocol):
    """Realtime KPI provider used by Agent2 bundle builders."""

    view_kind: str

    def get_kpi(self, brand_key: str) -> dict[str, Any]:
        """Return Agent2 KPI extras for one canonical brand key."""


def build_kpi_provider(view_kind: str, **kwargs: Any) -> KpiProvider:
    """Build the configured Agent2 KPI provider.

    ``strategic_ml`` keeps the cache-free MI Master market path. ``general``
    uses the ATC4 runtime market path.  The factory is intentionally small so
    future production runners can switch providers from config without mixing
    the two market definitions.
    """

    normalized = view_kind.strip().lower()
    if normalized in {"strategic", "strategic_ml", "market_landscape"}:
        return StrategicMlKpiProvider(**kwargs)
    if normalized in {"general", "general_view", "atc4"}:
        return GeneralViewKpiProvider(**kwargs)
    raise ValueError(f"unsupported KPI provider view kind: {view_kind}")


_DB_PATCH_LOCK = RLock()


@contextmanager
def connection_bound_dynamic_market_db(db_conn: Any):
    """Route dynamic-market module reads through an existing Agent2 connection.

    The FastAPI dynamic-market runtime opens a connection from API environment
    variables.  Agent2 already owns a DB connection, and bulk bundle generation
    must not perform one HTTP/API-environment connection per brand.  This
    context keeps the audited resolver/aggregator/composer code unchanged while
    binding its module-level read helpers to the caller's connection for the
    duration of a single provider call.
    """

    original_fetch_all = api_db.fetch_all
    original_fetch_one = api_db.fetch_one

    def fetch_all(sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        with db_conn.cursor() as cur:
            cur.execute(sql, params or ())
            return [dict(row) for row in cur.fetchall()]

    def fetch_one(sql: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
        rows = fetch_all(sql, params)
        return rows[0] if rows else None

    with _DB_PATCH_LOCK:
        api_db.fetch_all = fetch_all
        api_db.fetch_one = fetch_one
        try:
            yield
        finally:
            api_db.fetch_all = original_fetch_all
            api_db.fetch_one = original_fetch_one


@dataclass(frozen=True, slots=True)
class StrategicMlKpiProvider:
    """Adapter around the existing cache-free strategic ML KPI calculator."""

    db_conn: Any
    ml_id: str
    source: str = "UBIST"
    measure: str = "sales"
    view_kind: str = "market_landscape"

    def get_kpi(self, brand_key: str) -> dict[str, Any]:
        brand_name = self._brand_name_for_key(brand_key)
        if not brand_name:
            return self._empty(brand_key)
        rows = fetch_ml_metric_rows(brand_name, self.ml_id, self.source, self.measure, self.db_conn)
        if not rows:
            return self._empty(brand_key)
        return {
            "view_kind": self.view_kind,
            "market_scope": "strategic_ml",
            "market_id": self.ml_id,
            "source": self.source,
            "measure": self.measure,
            **calculate_ml_kpi_extras(rows),
        }

    def _brand_name_for_key(self, brand_key: str) -> str | None:
        with self.db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT brand_name
                FROM mart_strategic_ml_brand_metric
                WHERE ml_id = %s
                  AND brand_key = %s
                  AND source = %s
                  AND measure = %s
                ORDER BY brand_name
                LIMIT 1
                """,
                (self.ml_id, brand_key, source_public_to_db(self.source), self.measure),
            )
            row = cur.fetchone()
        return str(row["brand_name"]) if row and row.get("brand_name") else None

    def _empty(self, brand_key: str) -> dict[str, Any]:
        return {
            "view_kind": self.view_kind,
            "market_scope": "strategic_ml",
            "market_id": self.ml_id,
            "source": self.source,
            "measure": self.measure,
            "brand_key": brand_key,
            "available": False,
        }


@dataclass(frozen=True, slots=True)
class GeneralViewKpiProvider:
    """Realtime ATC4-market KPI provider backed by dynamic-market formulas."""

    db_conn: Any
    mart_db: str
    bridge_db: str
    source: str = "ubist"
    measure: str = "sales"
    top_n: int = 100
    period_start: str | None = None
    period_end: str | None = None
    view_kind: str = "general"

    def get_kpi(self, brand_key: str) -> dict[str, Any]:
        target_rows = self._brand_rows_for_key(brand_key)
        if not target_rows:
            return self._empty(brand_key)
        atc4_codes = self._atc4_codes(target_rows)
        with connection_bound_dynamic_market_db(self.db_conn):
            definition = GeneralViewResolver(mart_db=self.mart_db, bridge_db=self.bridge_db).resolve(
                atc4=list(atc4_codes),
                molecule=[],
                source=self.source,
                measure=self.measure,
            )
            metrics = MetricAggregator(mart_db=self.mart_db).aggregate(
                brands=definition.brands,
                source=definition.source,
                measure=definition.measure,
                period_range=PeriodRange(start=self.period_start, end=self.period_end),
                top_n=self.top_n,
            )
            payload = ResponseComposer().compose(definition=definition, metrics=metrics)

        target_metric = self._target_metric(metrics.all_brands, brand_key)
        kpi = self._extract_payload_kpi(payload)
        return {
            "view_kind": self.view_kind,
            "market_scope": "atc4",
            "source": self.source,
            "measure": self.measure,
            "brand_key": brand_key,
            "brand_name": target_metric.brand_name if target_metric else target_rows[0]["brand_name"],
            "atc4_codes": list(atc4_codes),
            "market_size_recent": kpi.get("market_size_recent"),
            "market_cagr_5y_pct": kpi.get("market_cagr_5y_pct"),
            "hhi_recent": kpi.get("hhi_recent"),
            "hhi_series_5y": self._data_section(payload, "hhi_series_5y", []),
            "direct_competition_count": kpi.get("direct_competition_count"),
            "target_brand": target_metric.brand_name if target_metric else None,
            "target_rank": target_metric.rank if target_metric else None,
            "brand_value_recent": target_metric.latest_value if target_metric else None,
            "brand_share_pct": self._latest_share(metrics, target_metric),
            "target_share_pct": self._latest_share(metrics, target_metric),
            "ms_pct": self._latest_share(metrics, target_metric),
            "brand_ranking": self._data_section(payload, "brand_ranking", {}),
            "company_ranking": self._data_section(payload, "company_ranking", {}),
            "ei_ms_matrix": self._data_section(payload, "ei_ms_matrix", {}),
            "market_size_series": self._data_section(payload, "market_size_series", []),
            "dynamic_payload": payload,
        }

    def _brand_rows_for_key(self, brand_key: str) -> list[dict[str, Any]]:
        with self.db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT brand_key, brand_name, atc4_code
                FROM mart_general_brand_metric
                WHERE brand_key = %s
                  AND source = %s
                  AND measure = %s
                ORDER BY atc4_code, brand_name, brand_key
                """,
                (brand_key, self.source, self.measure),
            )
            return [dict(row) for row in cur.fetchall()]

    @staticmethod
    def _atc4_codes(rows: list[dict[str, Any]]) -> tuple[str, ...]:
        seen: set[str] = set()
        codes: list[str] = []
        for row in rows:
            code = str(row.get("atc4_code") or "").strip().upper()
            if code and code not in seen:
                seen.add(code)
                codes.append(code)
        return tuple(codes)

    @staticmethod
    def _target_metric(brands: tuple[BrandMetric, ...], brand_key: str) -> BrandMetric | None:
        candidates = [brand for brand in brands if brand.brand_key == brand_key]
        if not candidates:
            return None
        return sorted(candidates, key=lambda item: (-item.total_value, item.atc4_code, item.brand_name))[0]

    @staticmethod
    def _latest_share(metrics: Any, target: BrandMetric | None) -> float | None:
        if target is None:
            return None
        latest_market = 0.0
        if target.latest_period:
            latest_market = sum(
                float(point.get("value") or 0.0)
                for brand in metrics.all_brands
                for point in brand.monthly_series
                if str(point.get("period")) == target.latest_period
            )
        if latest_market <= 0:
            return target.market_share_pct
        return target.latest_value / latest_market * 100 if target.latest_value is not None else None

    @staticmethod
    def _extract_payload_kpi(payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("data")
        if not isinstance(data, dict):
            return {}
        kpi = data.get("kpi")
        return kpi if isinstance(kpi, dict) else {}

    @staticmethod
    def _data_section(payload: dict[str, Any], key: str, default: Any) -> Any:
        data = payload.get("data")
        if not isinstance(data, dict):
            return default
        return data.get(key, default)

    def _empty(self, brand_key: str) -> dict[str, Any]:
        return {
            "view_kind": self.view_kind,
            "market_scope": "atc4",
            "source": self.source,
            "measure": self.measure,
            "brand_key": brand_key,
            "available": False,
        }
