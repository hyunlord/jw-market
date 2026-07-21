from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pipeline.scripts.api import db
from pipeline.scripts.api.composers.cache_to_response import compose_cached_json
from pipeline.scripts.api.openapi_docs import MARKET_STATUS_RESPONSES, PORTAL_CORE_TAG
from pipeline.scripts.api.utils import loads_json_maybe
from pipeline.scripts.etl.cache_build_common import iqvia_period_to_display, period_key


router = APIRouter()


def _market_recent_periods() -> dict[str, str | None]:
    """Return the latest period that actually has mart values, per source.

    Serving-time computation (the market-status route serves cache_market_status
    verbatim, and mart/ETL must not be rebuilt). ``ubist_recent`` is a ``YYYY-MM``
    month; ``iqvia_recent`` is a ``YYYY-nQ`` quarter converted from the mart's
    ``YYYY-Qn`` label. Source of truth is ``metric_history`` (a period-keyed map
    of real values), scanned over the actively-tracked JW brand rows — the latest
    period is globally uniform across markets, so this bounded scan equals the
    global mart maximum. ``None`` when no data exists.

    Kept route-local (imports only db + cache_build_common) so the market-status
    startup path does not pull heavy optional deps.
    """
    rows = db.fetch_all(
        """
        SELECT source, metric_history
        FROM mart_strategic_ml_brand_metric
        WHERE measure = 'sales' AND is_jw = 1
        """
    )
    keys_by_source: dict[str, set[str]] = {}
    for row in rows:
        history = loads_json_maybe(row.get("metric_history")) or {}
        if not isinstance(history, dict):
            continue
        keys_by_source.setdefault(str(row["source"]), set()).update(str(k) for k in history)

    def _latest(source: str) -> str | None:
        keys = keys_by_source.get(source)
        return sorted(keys, key=period_key)[-1] if keys else None

    return {
        "ubist_recent": _latest("ubist"),
        "iqvia_recent": iqvia_period_to_display(_latest("iqvia_nsa")),
    }


@router.get(
    "/api/market-status",
    tags=[PORTAL_CORE_TAG],
    summary="포탈 시장 현황 카드",
    description="운영 포탈 첫 화면의 시장 카드/상태 목록을 cache_market_status에서 그대로 반환합니다.",
    response_model=None,
    responses=MARKET_STATUS_RESPONSES,
)
def market_status() -> dict:
    row = db.fetch_one(
        """
        SELECT response_json
        FROM cache_market_status
        WHERE query_key = 'default'
        LIMIT 1
        """
    )
    if not row:
        raise HTTPException(status_code=404, detail={"error": "cache_not_found", "cache": "cache_market_status"})
    payload = compose_cached_json(row["response_json"])
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail={"error": "invalid_cache_payload", "cache": "cache_market_status"})
    # Serving-time baseline labels (mart latest period per source); the cached
    # payload is otherwise returned verbatim.
    payload.update(_market_recent_periods())
    return payload
