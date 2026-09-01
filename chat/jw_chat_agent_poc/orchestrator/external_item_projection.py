from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final


PUBLIC_EXTERNAL_FIELDS: Final[tuple[tuple[str, str], ...]] = (
    ("ITEM_NAME", "제품명"),
    ("itemName", "제품명"),
    ("PRDUCT", "제품명"),
    ("GOODS_NAME", "제품명"),
    ("ITEM_INGR_NAME", "주성분"),
    ("MAIN_INGR_ENG", "주성분"),
    ("MTRAL_NM", "성분"),
    ("ITEM_PERMIT_DATE", "허가일"),
    ("ENTP_NAME", "업체명"),
    ("APPROVAL_TIME", "임상 승인일"),
    ("APPLY_ENTP_NAME", "신청자"),
    ("ATC_CODE", "ATC 코드"),
    ("ETC_OTC_CODE", "전문/일반"),
    ("CLINIC_EXAM_TITLE", "임상시험명"),
    ("CLINC_EXAM_TITLE", "임상시험명"),
    ("CLINIC_STEP_NAME", "임상 단계"),
    ("briefTitle", "연구 제목"),
    ("officialTitle", "연구 제목"),
    ("overallStatus", "연구 상태"),
    ("title", "제목"),
    ("date", "날짜"),
    ("published_date", "날짜"),
    ("source", "출처"),
    ("url", "URL"),
    ("snippet", "요약"),
)


def public_external_rows(data: Mapping[str, Any], *, limit: int) -> tuple[tuple[str, Any], ...]:
    rows: list[tuple[str, Any]] = []
    for index, item in enumerate(_public_items(data, limit=limit), start=1):
        for key, label in PUBLIC_EXTERNAL_FIELDS:
            value = item.get(key)
            if value in (None, ""):
                continue
            rows.append((f"결과 {index} · {label}", value))
    return tuple(rows)


def _public_items(data: Mapping[str, Any], *, limit: int) -> tuple[Mapping[str, Any], ...]:
    direct = data.get("items")
    if isinstance(direct, Sequence) and not isinstance(direct, str | bytes):
        return tuple(item for item in direct[:limit] if isinstance(item, Mapping))
    payload = data.get("payload")
    if not isinstance(payload, Mapping):
        return ()
    for key in ("results", "studies", "items"):
        nested = payload.get(key)
        if isinstance(nested, Sequence) and not isinstance(nested, str | bytes):
            return tuple(item for item in nested[:limit] if isinstance(item, Mapping))
    return ()
