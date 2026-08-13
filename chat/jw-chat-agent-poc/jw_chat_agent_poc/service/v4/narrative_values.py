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
    "publisher",
    "published_at",
    "summary",
    "active_ingredient",
    "approval_date",
    "label_section",
    "conditions",
    "interventions",
    "brief_summary",
)
GROUP_FIELDS: Final = (
    "overall_status",
    "status",
    "phase",
    "phases",
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
    "publisher": "게시자",
    "published_at": "게시일",
    "summary": "요약",
    "active_ingredient": "성분",
    "approval_date": "승인일",
    "label_section": "라벨 정보",
    "conditions": "적응증",
    "interventions": "개입약물",
    "brief_summary": "시험 요약",
}
_PUBLIC_ENUMS: Final = {
    "RECRUITING": "모집 중",
    "NOT_YET_RECRUITING": "모집 전",
    "ACTIVE_NOT_RECRUITING": "진행 중(모집 종료)",
    "COMPLETED": "완료",
    "ENROLLING_BY_INVITATION": "초청 모집",
    "SUSPENDED": "일시 중단",
    "TERMINATED": "중단",
    "WITHDRAWN": "철회",
    "UNKNOWN": "미확인",
    "NO_DATA": "자료 없음",
    "LIVE": "게재 중",
    "PHASE1": "1상",
    "PHASE2": "2상",
    "PHASE3": "3상",
    "PHASE4": "4상",
    "EARLY_PHASE1": "초기 1상",
    "PHASE_NA": "해당 없음",
    "NA": "해당 없음",
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


def display_field_value(record: EvidenceRecord, field: str | None) -> str | None:
    value = field_value(record, field)
    if value is None:
        return None
    if ", " in value:
        return ", ".join(public_enum_value(item) for item in value.split(", "))
    return public_enum_value(value)


def public_enum_value(value: object) -> str:
    raw = str(value)
    exact = _PUBLIC_ENUMS.get(raw.upper())
    if exact is not None:
        return exact
    output = raw
    for token in sorted(_PUBLIC_ENUMS, key=len, reverse=True):
        output = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])",
            _PUBLIC_ENUMS[token],
            output,
            flags=re.IGNORECASE,
        )
    return output


def record_identity(record: EvidenceRecord, index: int) -> str | None:
    for field in IDENTITY_FIELDS:
        value = field_value(record, field)
        if value:
            return public_enum_value(value)
    return None


def numeric_value(value: str | None) -> float | None:
    if value is None or _NUMBER_RE.fullmatch(value) is None:
        return None
    try:
        return float(value.rstrip("%").replace(",", ""))
    except ValueError:
        return None


def display_number(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)
