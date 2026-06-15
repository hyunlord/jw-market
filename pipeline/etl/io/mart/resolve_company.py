"""Company resolution for Layer 3 JSON mart rows."""

from __future__ import annotations

from typing import Any

try:
    import pandas as pd
except Exception:  # pragma: no cover
    pd = None  # type: ignore[assignment]


def _present(value: Any) -> bool:
    if value is None:
        return False
    if pd is not None:
        try:
            if pd.isna(value):
                return False
        except Exception:
            pass
    text = str(value).strip()
    return bool(text and text.lower() not in {"nan", "none", "null"})


def resolve_company(catalog_row: dict[str, Any] | None, raw_row: dict[str, Any], source: str) -> str | None:
    """Resolve manufacturer/company with catalog-first precedence."""

    if catalog_row:
        for key in ("제조사", "판매사", "manufacturer", "company"):
            value = catalog_row.get(key)
            if _present(value):
                return str(value).strip()

    if source == "ubist":
        for key in ("제조사", "판매사", "manufacturer", "company"):
            value = raw_row.get(key)
            if _present(value):
                return str(value).strip()

    if source == "iqvia_nsa":
        payload_static = raw_row.get("payload_static") or {}
        for value in (
            payload_static.get("MFR NAME KOR"),
            raw_row.get("mfr_name_kor"),
            raw_row.get("mfr_name"),
            payload_static.get("MFR NAME"),
        ):
            if _present(value):
                return str(value).strip()

    return None
