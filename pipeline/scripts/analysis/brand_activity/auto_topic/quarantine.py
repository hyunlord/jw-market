from __future__ import annotations

from .models import JsonValue


def axis_failed(axis_payload: dict[str, JsonValue]) -> bool:
    """Return whether a market axis is unavailable for downstream share calls."""
    return str(axis_payload.get("status") or "") != "ok"


def brand_axis_quarantine(*, atc4: str, brand: str, scope_id: str, axis_version: str, row_count: int, axis_payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Create a sanitized no-call brand result when the market axis failed."""
    return {
        "status": "quarantined_axis_failed",
        "brand": brand,
        "atc4": atc4,
        "scope_id": scope_id,
        "axis_version": axis_version,
        "denominator": "brand_row_count_primary_topic",
        "row_count": row_count,
        "topic_shares": [],
        "etc_pct": None,
        "reason": "market_axis_unavailable",
        "axis_status": str(axis_payload.get("status") or ""),
        "axis_reason": str(axis_payload.get("reason") or axis_payload.get("error_type") or ""),
        "qc": {
            "guard": {"layer": "mechanical_guard", "status": "fail", "reasons": ["market_axis_unavailable"]},
            "drift": {"layer": "drift", "status": "skip_quarantined"},
            "dict_xcheck": {"layer": "dict_xcheck", "status": "skip_quarantined"},
        },
    }
