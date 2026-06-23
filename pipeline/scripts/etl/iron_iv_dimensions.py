"""페린젝트/베노훼럼 IV iron dimension ground truth helpers.

MI Master의 철 시장은 시장 총합에는 IV+경구 전체를 포함하지만,
Molecule/Strength/1ml당 Fe함량 3개 dimension은 IV iron pack만 써야 한다.
경구 제품을 market membership에서 빼는 대안은 sales/MS/rank 분모를 바꾸므로
기각하고, mart dimension payload만 IV pack overlay에서 재구성한다.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


IRON_ML_ID = "ml_012"
IRON_CD_ID = "cd_015"
FE_CONTENT_FIELD = "fe_content_per_ml"
FE_CONTENT_LEVEL = "1ml당 Fe함량"
IRON_IV_DIMENSION_FIELDS = ("molecule", "strength_pack", FE_CONTENT_FIELD)
IRON_IV_SRC_STRENGTH_FIELD = "_iron_iv_src_strength_pack"
IRON_IV_DIMENSION_PAYLOAD_KEYS = ("dimension_data", "dimension_channel_data", "dimension_specialty_data")


def _norm(value: Any) -> str:
    return str(value or "").strip().upper().replace(",", "")


IRON_IV_PACK_OVERLAY: dict[str, dict[str, str]] = {
    # Ground truth: /Users/rexxa/Downloads/GROUND_TRUTH_FERINJECT_IV_DIMENSIONS.md
    # 무엇: IV pack label을 3개 dimension overlay로 치환한다.
    # 왜: raw IQVIA strength/molecule에는 경구 제품과 IRON FERRIC raw desc가 섞인다.
    # 도메인 근거: 2026-06-11 PL 제공 16 pack 표. Fe/ml은 molecule와 1:1.
    # 기각 대안: 경구 브랜드를 시장에서 제거하면 철 전체 market total이 바뀐다.
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
_OVERLAY_BY_NORM = {_norm(pack): overlay for pack, overlay in IRON_IV_PACK_OVERLAY.items()}


def is_iron_iv_dimension_market(market_id: Any) -> bool:
    return str(market_id or "").strip() in {IRON_ML_ID, IRON_CD_ID}


def overlay_for_pack_label(label: Any) -> dict[str, str] | None:
    return _OVERLAY_BY_NORM.get(_norm(label))


def capture_iron_iv_source_strength(row: dict[str, Any], *, market_id: Any) -> None:
    # 무엇: IQVIA strength_pack을 catalog recode로 묶기 직전의 raw IV pack 라벨을
    # 철 시장 행에만 sidecar로 보존한다.
    # 왜: display용 strength_pack은 A2 원칙에 따라 recode여야 하지만,
    # Iron IV overlay는 V.IV/A.IV pack 라벨을 키로 삼는다. recode 후에는
    # overlay 매칭이 0이 되어 Molecule/Strength/Fe/ml dimension이 비었다.
    # 도메인 근거: ml_012/cd_015는 시장 총합은 IV+경구를 유지하되 세 dimension은
    # IV pack ground truth에서만 재구성한다.
    # 기각 대안: display strength_pack을 raw pack으로 되돌리면 IQVIA raw pack 누출
    # 회귀가 생긴다. 그래서 내부 sidecar만 쓰고 cache payload에서는 제거한다.
    if not is_iron_iv_dimension_market(market_id):
        return
    if str(row.get("source") or "").strip().lower() != "iqvia_nsa":
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
        try:
            return float(item.get("raw_value") or 0.0)
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(item or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _add_series(target: dict[str, Any], series: dict[str, Any]) -> None:
    for period, item in (series or {}).items():
        value = _series_value(item)
        bucket = target.setdefault(str(period), {"raw_value": 0.0})
        bucket["raw_value"] = _series_value(bucket) + value


def _add_nested_series(target: dict[str, Any], nested: dict[str, Any]) -> None:
    for key, series in (nested or {}).items():
        if isinstance(series, dict):
            _add_series(target.setdefault(str(key), {}), series)


def _rebuild_from_strength_series(strength_payload: dict[str, Any], *, nested: bool = False) -> dict[str, Any]:
    rebuilt: dict[str, dict[str, Any]] = {field: {} for field in IRON_IV_DIMENSION_FIELDS}
    for raw_label, series in (strength_payload or {}).items():
        overlay = overlay_for_pack_label(raw_label)
        if not overlay:
            continue
        for field in IRON_IV_DIMENSION_FIELDS:
            label = overlay[field]
            bucket = rebuilt[field].setdefault(label, {})
            if nested:
                _add_nested_series(bucket, series)
            else:
                _add_series(bucket, series)
    return rebuilt


def _strength_source_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sidecar = payload.get(IRON_IV_SRC_STRENGTH_FIELD)
    if isinstance(sidecar, dict) and sidecar:
        return sidecar
    strength_payload = payload.get("strength_pack")
    return strength_payload if isinstance(strength_payload, dict) else {}


def apply_iron_iv_dimension_rule(row: dict[str, Any], *, market_id: Any) -> dict[str, Any]:
    if not is_iron_iv_dimension_market(market_id):
        return row

    result = dict(row)
    dimension_data = deepcopy(result.get("dimension_data") or {})
    dimension_channel_data = deepcopy(result.get("dimension_channel_data") or {})
    dimension_specialty_data = deepcopy(result.get("dimension_specialty_data") or {})

    rebuilt = _rebuild_from_strength_series(_strength_source_payload(dimension_data))
    rebuilt_channel = _rebuild_from_strength_series(
        _strength_source_payload(dimension_channel_data),
        nested=True,
    )
    rebuilt_specialty = _rebuild_from_strength_series(
        _strength_source_payload(dimension_specialty_data),
        nested=True,
    )

    for field in IRON_IV_DIMENSION_FIELDS:
        dimension_data[field] = rebuilt.get(field, {})
        dimension_channel_data[field] = rebuilt_channel.get(field, {})
        dimension_specialty_data[field] = rebuilt_specialty.get(field, {})
    for payload in (dimension_data, dimension_channel_data, dimension_specialty_data):
        payload.pop(IRON_IV_SRC_STRENGTH_FIELD, None)

    by_dimension = deepcopy(result.get("by_dimension") or {})
    for field in IRON_IV_DIMENSION_FIELDS:
        by_dimension.pop(field, None)

    result["dimension_data"] = dimension_data
    result["dimension_channel_data"] = dimension_channel_data
    result["dimension_specialty_data"] = dimension_specialty_data
    result["by_dimension"] = by_dimension
    return result
