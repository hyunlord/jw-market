from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from pipeline.scripts.api.config import get_settings
from pipeline.scripts.api.db import connect, fetch_one
from pipeline.scripts.api.utils import json_dumps, loads_json_maybe


def get_cache(cache_key: str) -> Any | None:
    row = fetch_one(
        """
        SELECT response_json
        FROM response_store
        WHERE cache_key = %s
          AND (expires_at IS NULL OR expires_at > NOW())
        """,
        (cache_key,),
    )
    if not row:
        return None
    return loads_json_maybe(row["response_json"])


def set_cache(
    cache_key: str,
    endpoint: str,
    response: Any,
    *,
    brand_name: str | None = None,
    period_yyyymm: str | None = None,
    view: str | None = None,
    source: str | None = None,
    measure: str | None = None,
    ttl_seconds: int | None = None,
    computation_ms: int | None = None,
) -> None:
    ttl = ttl_seconds or get_settings().cache_ttl_seconds
    payload = json_dumps(response)
    expires_at = datetime.now() + timedelta(seconds=ttl)
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO response_store (
                  cache_key, endpoint, brand_name, period_yyyymm, view, source, measure,
                  response_json, ttl_seconds, computed_at, expires_at,
                  computation_ms, size_bytes
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                  endpoint = VALUES(endpoint),
                  brand_name = VALUES(brand_name),
                  period_yyyymm = VALUES(period_yyyymm),
                  view = VALUES(view),
                  source = VALUES(source),
                  measure = VALUES(measure),
                  response_json = VALUES(response_json),
                  ttl_seconds = VALUES(ttl_seconds),
                  computed_at = NOW(),
                  expires_at = VALUES(expires_at),
                  computation_ms = VALUES(computation_ms),
                  size_bytes = VALUES(size_bytes)
                """,
                (
                    cache_key,
                    endpoint,
                    brand_name,
                    period_yyyymm,
                    view,
                    source,
                    measure,
                    payload,
                    ttl,
                    expires_at,
                    computation_ms,
                    len(payload.encode("utf-8")),
                ),
            )
        conn.commit()


def truncate_cache() -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE response_store")
        conn.commit()


def count_cache_keys() -> int:
    row = fetch_one("SELECT COUNT(*) AS row_count FROM response_store")
    return int(row["row_count"]) if row else 0


def cache_state() -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT endpoint, COUNT(*) AS keys_count
                FROM response_store
                GROUP BY endpoint
                ORDER BY endpoint
                """
            )
            return list(cur.fetchall())
