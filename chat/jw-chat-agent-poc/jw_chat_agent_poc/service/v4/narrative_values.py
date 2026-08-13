from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Final

from jw_chat_agent_poc.service.v4.evidence_payload import is_request_metadata_key
from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceRecord


IDENTITY_FIELDS: Final = (
    "nct_id",
    "patent_number",
    "patent_no",
    "application_number",
    "item_name",
    "product_name",
    "brand",
    "title",
)
NARRATIVE_FIELDS: Final = (
    "overall_status",
    "status",
    "phase",
    "phases",
    "sponsor",
    "company",
    "start_date",
    "completion_date",
    "expiration_date",
    "patent_type",
    "extinction_reason",
    "owner",
    "pms_end_date",
    "enrollment",
    "sales_krw",
    "market_share",
)
GROUP_FIELDS: Final = (
    "overall_status",
    "status",
    "phase",
    "sponsor",
    "company",
    "patent_type",
    "extinction_reason",
    "owner",
)
DATE_FIELDS: Final = (
    "start_date",
    "completion_date",
    "expiration_date",
    "filing_date",
    "publication_date",
    "pms_end_date",
)
NUMERIC_FIELDS: Final = (
    "enrollment",
    "sales_krw",
    "market_share",
    "amount_krw",
    "patient_count",
)
FIELD_LABELS: Final = {
    "overall_status": "상태",
    "status": "상태",
    "phase": "단계",
    "phases": "단계",
    "sponsor": "의뢰자",
    "company": "회사",
    "start_date": "시작일",
    "completion_date": "완료일",
    "expiration_date": "만료일",
    "patent_type": "특허구분",
    "extinction_reason": "소멸 사유",
    "owner": "권리자",
    "pms_end_date": "재심사기간 종료일",
    "filing_date": "출원일",
    "publication_date": "공개일",
    "enrollment": "등록 인원",
    "sales_krw": "매출",
    "market_share": "점유율",
    "amount_krw": "금액",
    "patient_count": "환자수",
}
_NUMBER_RE: Final = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?%?")


def field_value(record: EvidenceRecord, field: str | None) -> str | None:
    if not field or is_request_metadata_key(field):
        return None
    value = record.payload.get(field)
    if value in (None, "", "원천 미제공"):
        return None
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value if item not in (None, "")) or None
    if isinstance(value, Mapping):
        return None
    return str(value)


def record_identity(record: EvidenceRecord, index: int) -> str:
    for field in IDENTITY_FIELDS:
        value = field_value(record, field)
        if value:
            return value
    return f"확인 레코드 {index}"


def numeric_value(value: str | None) -> float | None:
    if value is None or _NUMBER_RE.fullmatch(value) is None:
        return None
    try:
        return float(value.rstrip("%").replace(",", ""))
    except ValueError:
        return None


def display_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)
