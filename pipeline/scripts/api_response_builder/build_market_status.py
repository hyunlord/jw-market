from __future__ import annotations

from typing import Any

from pipeline.scripts.api.db import connect

from .utils import BRAND_MARTS, normalise_market_row


def build_market_status_response_from_row(view_type: str, market_row: dict[str, Any]) -> dict[str, Any]:
    return normalise_market_row(view_type, market_row)


def build_market_status_response(market_id: str, view_type: str, source: str, measure: str) -> dict[str, Any]:
    cfg = BRAND_MARTS[view_type]
    mart = cfg["market_mart"]
    market_id_col = cfg["market_id_col"]
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT *
                FROM {mart}
                WHERE {market_id_col} = %s AND source = %s AND measure = %s
                """,
                (market_id, source, measure),
            )
            row = cur.fetchone()
    if not row:
        return {
            "error": "not_found",
            "market_id": market_id,
            "view": view_type,
            "source": source,
            "measure": measure,
        }
    return build_market_status_response_from_row(view_type, row)
