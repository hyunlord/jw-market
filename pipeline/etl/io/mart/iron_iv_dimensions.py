"""페린젝트/베노훼럼 IV iron dimension ground-truth helpers."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

IRON_ML_ID = "ml_012"
IRON_CD_ID = "cd_015"
FE_CONTENT_FIELD = "fe_content_per_ml"
IRON_IV_DIMENSION_FIELDS = ("molecule", "strength_pack", FE_CONTENT_FIELD)
IRON_IV_SRC_STRENGTH_FIELD = "_iron_iv_src_strength_pack"
IRON_IV_DIMENSION_PAYLOAD_KEYS = ("dimension_data", "dimension_channel_data", "dimension_specialty_data")

IRON_IV_PACK_OVERLAY: dict[str, dict[str, str]] = {
    "A.IV 540MG/ML 5ML": {"molecule": "Iron scurose", FE_CONTENT_FIELD: "20", "strength_pack": "100"},
    "A.IV 5400MG 10ML 5": {"molecule": "Iron scurose", FE_CONTENT_FIELD: "20", "strength_pack": "200"},
    "A.IV 540MG/ML 5ML 5": {"molecule": "Iron scurose", FE_CONTENT_FIELD: "20", "strength_pack": "100"},
    "V.IV 50MG/ML 10ML": {"molecule": "Ferric carboxymaltose", FE_CONTENT_FIELD: "50", "strength_pack": "500"},
    "V.IV 50MG/ML 20ML": {"molecule": "Ferric carboxymaltose", FE_CONTENT_FIELD: "50", "strength_pack": "1000"},
    "V.IV 50MG/ML 2ML": {"molecule": "Ferric carboxymaltose", FE_CONTENT_FIELD: "50", "strength_pack": "100"},
    "50MG": {"molecule": "Ferric carboxymaltose", FE_CONTENT_FIELD: "50", "strength_pack": "500"},
    "A.IV 100MG 5ML 5": {"molecule": "Iron scurose", FE_CONTENT_FIELD: "20", "strength_pack": "100"},
    "100MG": {"molecule": "Iron scurose", FE_CONTENT_FIELD: "20", "strength_pack": "100"},
    "A.IV 2700MG 5ML 5": {"molecule": "Iron scurose", FE_CONTENT_FIELD: "20", "strength_pack": "100"},
    "2700MG": {"molecule": "Iron scurose", FE_CONTENT_FIELD: "20", "strength_pack": "100"},
    "A.IV 6.75MG 4.5ML": {"molecule": "Ferric Pyrophosphate", FE_CONTENT_FIELD: "1.5", "strength_pack": "7"},
    "6.75MG": {"molecule": "Ferric Pyrophosphate", FE_CONTENT_FIELD: "1.5", "strength_pack": "7"},
    "A.IV 417MG/ML 2ML": {"molecule": "Iron isomaltoside", FE_CONTENT_FIELD: "100", "strength_pack": "200"},
    "A.IV 417MG/ML 5ML": {"molecule": "Iron isomaltoside", FE_CONTENT_FIELD: "100", "strength_pack": "500"},
    "417MG": {"molecule": "Iron isomaltoside", FE_CONTENT_FIELD: "100", "strength_pack": "200"},
    "A.IV 200MG 10ML 5": {"molecule": "Iron scurose", FE_CONTENT_FIELD: "20", "strength_pack": "200"},
    "200MG": {"molecule": "Iron scurose", FE_CONTENT_FIELD: "20", "strength_pack": "200"},
    "A.IV 540MG/ML 10ML": {"molecule": "Iron scurose", FE_CONTENT_FIELD: "20", "strength_pack": "200"},
    "540MG": {"molecule": "Iron scurose", FE_CONTENT_FIELD: "20", "strength_pack": "100"},
    "5400MG": {"molecule": "Iron scurose", FE_CONTENT_FIELD: "20", "strength_pack": "200"},
}


def _norm(value: Any) -> str:
    return str(value or "").strip().upper().replace(",", "")


_OVERLAY_BY_NORM = {_norm(pack): overlay for pack, overlay in IRON_IV_PACK_OVERLAY.items()}


def is_iron_iv_dimension_market(market_id: Any) -> bool:
    return str(market_id or "").strip() in {IRON_ML_ID, IRON_CD_ID}


def capture_iron_iv_source_strength(row: dict[str, Any], *, market_id: Any) -> None:
    if not is_iron_iv_dimension_market(market_id) or str(row.get("source") or "").strip().lower() != "iqvia_nsa":
        return
    for payload_key in IRON_IV_DIMENSION_PAYLOAD_KEYS:
        payload = row.get(payload_key)
        if not isinstance(payload, dict):
            continue
        strength_payload = payload.get("strength_pack")
        if isinstance(strength_payload, dict) and strength_payload:
            payload[IRON_IV_SRC_STRENGTH_FIELD] = deepcopy(strength_payload)


def _series_value(item: Any) -> float:
    if isinstance(item, dict):
        item = item.get("raw_value")
    try:
        return float(item or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _add_series(target: dict[str, Any], series: dict[str, Any]) -> None:
    for period, item in (series or {}).items():
        bucket = target.setdefault(str(period), {"raw_value": 0.0})
        bucket["raw_value"] = _series_value(bucket) + _series_value(item)


def _add_nested_series(target: dict[str, Any], nested: dict[str, Any]) -> None:
    for key, series in (nested or {}).items():
        if isinstance(series, dict):
            _add_series(target.setdefault(str(key), {}), series)


def _strength_source_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sidecar = payload.get(IRON_IV_SRC_STRENGTH_FIELD)
    if isinstance(sidecar, dict) and sidecar:
        return sidecar
    strength_payload = payload.get("strength_pack")
    return strength_payload if isinstance(strength_payload, dict) else {}


def _rebuild_from_strength_series(strength_payload: dict[str, Any], *, nested: bool = False) -> dict[str, Any]:
    rebuilt: dict[str, dict[str, Any]] = {field: {} for field in IRON_IV_DIMENSION_FIELDS}
    for raw_label, series in (strength_payload or {}).items():
        overlay = _OVERLAY_BY_NORM.get(_norm(raw_label))
        if not overlay:
            continue
        for field in IRON_IV_DIMENSION_FIELDS:
            bucket = rebuilt[field].setdefault(overlay[field], {})
            if nested:
                _add_nested_series(bucket, series)
            else:
                _add_series(bucket, series)
    return rebuilt


def apply_iron_iv_dimension_rule(row: dict[str, Any], *, market_id: Any) -> dict[str, Any]:
    if not is_iron_iv_dimension_market(market_id):
        return row
    result = dict(row)
    dimension_data = deepcopy(result.get("dimension_data") or {})
    dimension_channel_data = deepcopy(result.get("dimension_channel_data") or {})
    dimension_specialty_data = deepcopy(result.get("dimension_specialty_data") or {})
    rebuilt = _rebuild_from_strength_series(_strength_source_payload(dimension_data))
    rebuilt_channel = _rebuild_from_strength_series(_strength_source_payload(dimension_channel_data), nested=True)
    rebuilt_specialty = _rebuild_from_strength_series(_strength_source_payload(dimension_specialty_data), nested=True)
    for field in IRON_IV_DIMENSION_FIELDS:
        dimension_data[field] = rebuilt.get(field, {})
        dimension_channel_data[field] = rebuilt_channel.get(field, {})
        dimension_specialty_data[field] = rebuilt_specialty.get(field, {})
    for payload in (dimension_data, dimension_channel_data, dimension_specialty_data):
        payload.pop(IRON_IV_SRC_STRENGTH_FIELD, None)
    by_dimension = deepcopy(result.get("by_dimension") or {})
    for field in IRON_IV_DIMENSION_FIELDS:
        by_dimension.pop(field, None)
    result.update(
        {
            "dimension_data": dimension_data,
            "dimension_channel_data": dimension_channel_data,
            "dimension_specialty_data": dimension_specialty_data,
            "by_dimension": by_dimension,
        }
    )
    return result
