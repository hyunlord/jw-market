from __future__ import annotations

from fastapi import APIRouter, HTTPException

from pipeline.scripts.api import db
from pipeline.scripts.api.composers.cache_to_response import compose_cached_json
from pipeline.scripts.api.openapi_docs import MARKET_STATUS_RESPONSES, PORTAL_CORE_TAG
from pipeline.scripts.api.utils import loads_json_maybe
from pipeline.scripts.etl.cache_build_common import brand_cagr_exclusive, iqvia_period_to_display, period_key


router = APIRouter()


def _brand_metric_rows() -> list[dict]:
    return db.fetch_all(
        """
        SELECT ml_id, brand_name, source, metric_history
        FROM mart_strategic_ml_brand_metric
        WHERE measure = 'sales' AND is_jw = 1
        """
    )


def _market_recent_periods(rows: list[dict] | None = None) -> dict[str, str | None]:
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
    rows = _brand_metric_rows() if rows is None else rows
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


def _brand_cagr_by_brand(
    rows: list[dict] | None = None,
) -> dict[tuple[str, str, str], tuple[float | None, float | None]]:
    selected: dict[tuple[str, str, str], dict] = {}
    for row in _brand_metric_rows() if rows is None else rows:
        brand = str(row.get("brand_name") or "")
        ml_id = str(row.get("ml_id") or "")
        source = str(row.get("source") or "").lower()
        if not brand or not ml_id or not source:
            continue
        selected[(brand, ml_id, source)] = row

    result: dict[tuple[str, str, str], tuple[float | None, float | None]] = {}
    for key, row in selected.items():
        history = loads_json_maybe(row.get("metric_history")) or {}
        result[key] = brand_cagr_exclusive(history if isinstance(history, dict) else {})
    return result


def _overlay_brand_cagr(payload: dict, rows: list[dict]) -> None:
    values = _brand_cagr_by_brand(rows)
    for card in payload.get("brand_cards") or []:
        if not isinstance(card, dict):
            continue
        extended = card.setdefault("back_extended", {})
        market_id = str(card.get("market_id") or "")
        ml_id = f"ml_{market_id.removeprefix('strategy_')}" if market_id else ""
        brand = str(card.get("brand") or "")
        default_source = str((card.get("front") or {}).get("default_source") or "").upper()
        source = {"UBIST": "ubist", "IQVIA": "iqvia_nsa"}.get(default_source)
        candidates = [source] if source else []
        candidates.extend(candidate for candidate in ("ubist", "iqvia_nsa") if candidate not in candidates)
        brand_cagr_5y, brand_cagr_3y = next(
            (values[(brand, ml_id, candidate)] for candidate in candidates if (brand, ml_id, candidate) in values),
            (None, None),
        )
        extended["brand_cagr_5y_pct"] = brand_cagr_5y
        extended["brand_cagr_3y_pct"] = brand_cagr_3y


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
    rows = _brand_metric_rows()
    _overlay_brand_cagr(payload, rows)
    payload.update(_market_recent_periods(rows))
    return payload
