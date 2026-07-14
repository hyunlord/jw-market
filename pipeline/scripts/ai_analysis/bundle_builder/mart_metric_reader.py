from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .catalog_db_loader import source_public_to_db

PROJECT_ROOT = Path(__file__).resolve().parents[4]
CATALOG_DIR = PROJECT_ROOT / "output" / "catalog"


@dataclass(frozen=True, slots=True)
class MlMetricRows:
    """ML mart rows required to calculate cache-compatible KPI extras."""

    brand_row: dict[str, Any]
    market_row: dict[str, Any]
    sibling_rows: tuple[dict[str, Any], ...]
    catalog_member_count: int | None = None


def json_load(value: Any) -> Any:
    if value in (None, ""):
        return {}
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}


def _table_exists(db_conn: Any, table_name: str) -> bool:
    with db_conn.cursor() as cur:
        cur.execute("SHOW TABLES LIKE %s", (table_name,))
        return cur.fetchone() is not None


def _catalog_member_count_from_db(ml_id: str, db_conn: Any) -> int | None:
    if not _table_exists(db_conn, "catalog_strategic_brand"):
        return None
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(DISTINCT COALESCE(NULLIF(canonical_name, ''), NULLIF(name, ''))) AS member_count
            FROM catalog_strategic_brand
            WHERE ml_id = %s
              AND COALESCE(NULLIF(canonical_name, ''), NULLIF(name, '')) IS NOT NULL
            """,
            (ml_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    value = row.get("member_count")
    return int(value) if value is not None else None


def _catalog_member_count_from_parquet(ml_id: str) -> int | None:
    path = CATALOG_DIR / "strategic_brand" / "strategic_brand.parquet"
    if not path.exists():
        return None
    import pandas as pd

    frame = pd.read_parquet(path, columns=["ml_id", "canonical_name", "name"])
    sub = frame[frame["ml_id"].astype(str) == str(ml_id)]
    names = {
        str(row.get("canonical_name") or row.get("name") or "").strip()
        for _, row in sub.iterrows()
        if str(row.get("canonical_name") or row.get("name") or "").strip()
    }
    return len(names)


def catalog_member_count_for_ml_market(ml_id: str, db_conn: Any) -> int | None:
    """Return original build_cache_cause strategic_brand member count for an ML market."""

    db_count = _catalog_member_count_from_db(ml_id, db_conn)
    if db_count is not None:
        return db_count
    return _catalog_member_count_from_parquet(ml_id)


def fetch_ml_metric_rows(
    brand_name: str,
    ml_id: str,
    source: str,
    measure: str,
    db_conn: Any,
) -> MlMetricRows | None:
    db_source = source_public_to_db(source)
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM mart_strategic_ml_brand_metric
            WHERE ml_id = %s
              AND brand_name = %s
              AND source = %s
              AND measure = %s
            LIMIT 1
            """,
            (ml_id, brand_name, db_source, measure),
        )
        brand_row = cur.fetchone()
        cur.execute(
            """
            SELECT *
            FROM mart_strategic_ml_market_metric
            WHERE ml_id = %s
              AND source = %s
              AND measure = %s
            LIMIT 1
            """,
            (ml_id, db_source, measure),
        )
        market_row = cur.fetchone()
        cur.execute(
            """
            SELECT *
            FROM mart_strategic_ml_brand_metric
            WHERE ml_id = %s
              AND source = %s
              AND measure = %s
            """,
            (ml_id, db_source, measure),
        )
        sibling_rows = tuple(cur.fetchall())
    if not brand_row or not market_row:
        return None
    return MlMetricRows(
        brand_row=dict(brand_row),
        market_row=dict(market_row),
        sibling_rows=tuple(dict(row) for row in sibling_rows),
        catalog_member_count=catalog_member_count_for_ml_market(ml_id, db_conn),
    )


def use_cache_free_ml_kpi(config: Any) -> bool:
    ms_config = getattr(getattr(config, "market", config), "ms_computation", None)
    if not isinstance(ms_config, dict):
        return False
    return bool(ms_config.get("cache_free_ml_kpi") or ms_config.get("kpi_source") == "mart")


def ml_view_exists(
    brand_name: str,
    ml_id: str,
    source: str,
    measure: str,
    db_conn: Any,
) -> bool:
    db_source = source_public_to_db(source)
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM mart_strategic_ml_brand_metric b
            JOIN mart_strategic_ml_market_metric m
              ON m.ml_id = b.ml_id
             AND m.source = b.source
             AND m.measure = b.measure
            WHERE b.ml_id = %s
              AND b.brand_name = %s
              AND b.source = %s
              AND b.measure = %s
            LIMIT 1
            """,
            (ml_id, brand_name, db_source, measure),
        )
        return cur.fetchone() is not None
