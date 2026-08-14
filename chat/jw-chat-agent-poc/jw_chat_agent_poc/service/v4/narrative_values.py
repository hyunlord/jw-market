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
    "notice_number",
    "title",
)
NARRATIVE_FIELDS: Final = (
    "overall_status",
    "status",
    "phase",
    "phases",
    "brief_title",
    "official_title",
    "study_type",
    "sponsor",
    "company",
    "start_date",
    "completion_date",
    "primary_completion_date",
    "expiration_date",
    "extinction_date",
    "invention_title",
    "patent_type",
    "extinction_reason",
    "owner",
    "pms_end_date",
    "enrollment",
    "primary_outcomes",
    "secondary_outcomes",
    "sales_krw",
    "sales",
    "unit",
    "period",
    "delta_krw",
    "sales_delta",
    "market_share",
    "share_pct",
    "publisher",
    "published_at",
    "summary",
    "active_ingredient",
    "approval_date",
    "label_section",
    "conditions",
    "interventions",
    "intervention_details",
    "comparators",
    "collaborators",
    "countries",
    "facilities",
    "brief_summary",
    "detailed_description",
    "eligibility_criteria",
    "sex",
    "minimum_age",
    "maximum_age",
    "has_results",
    "last_update_date",
    "notice_name",
    "notice_number",
    "effective_date",
    "target_product",
    "target_ingredient",
    "matching_basis",
    "match_candidates",
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
    "primary_completion_date": "1차 완료일",
    "expiration_date": "만료일",
    "extinction_date": "소멸일",
    "brief_title": "시험명",
    "official_title": "공식 시험명",
    "study_type": "시험 유형",
    "invention_title": "발명명",
    "patent_type": "특허구분",
    "extinction_reason": "소멸 사유",
    "owner": "권리자",
    "pms_end_date": "재심사기간 종료일",
    "filing_date": "출원일",
    "publication_date": "공개일",
    "enrollment": "등록 인원",
    "primary_outcomes": "1차 평가변수",
    "secondary_outcomes": "2차 평가변수",
    "sales_krw": "매출",
    "sales": "매출",
    "unit": "단위",
    "period": "기간",
    "delta_krw": "증감",
    "sales_delta": "증감",
    "market_share": "점유율",
    "share_pct": "점유율",
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
    "intervention_details": "개입 상세",
    "comparators": "대조군",
    "collaborators": "협력기관",
    "countries": "국가",
    "facilities": "수행기관",
    "brief_summary": "시험 요약",
    "detailed_description": "상세 설명",
    "eligibility_criteria": "선정·제외 기준",
    "sex": "성별",
    "minimum_age": "최소 연령",
    "maximum_age": "최대 연령",
    "has_results": "결과 공개 여부",
    "last_update_date": "최종 갱신일",
    "notice_name": "고시명",
    "notice_number": "고시번호",
    "effective_date": "시행일",
    "target_product": "대상 품명",
    "target_ingredient": "대상 성분",
    "matching_basis": "매칭 근거",
    "match_candidates": "매칭 후보",
}
_LONG_NARRATIVE_FIELDS: Final = {
    "brief_summary",
    "detailed_description",
    "eligibility_criteria",
    "invention_title",
    "intervention_details",
    "official_title",
    "primary_outcomes",
    "secondary_outcomes",
    "summary",
}
_LONG_NARRATIVE_LIMIT: Final = 320
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
    "ACTUAL": "실제",
    "ESTIMATED": "예상",
    "ALL": "전체",
    "MALE": "남성",
    "FEMALE": "여성",
    "DRUG": "의약품",
    "INTERVENTIONAL": "중재 연구",
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


def narrative_field_value(record: EvidenceRecord, field: str) -> str | None:
    value = record.payload.get(field)
    if value in (None, "", "원천 미제공"):
        return None
    if field == "enrollment" and isinstance(value, Mapping):
        count = value.get("count")
        kind = value.get("type")
        if count in (None, "") and kind in (None, ""):
            return None
        count_text = f"{count}명" if count not in (None, "") else ""
        kind_text = public_enum_value(kind) if kind not in (None, "") else ""
        return f"{count_text} ({kind_text})".strip() if kind_text else count_text
    if isinstance(value, Mapping):
        text = _mapping_narrative(value)
    elif isinstance(value, (list, tuple)):
        parts = tuple(
            part
            for item in value
            if (
                part := (
                    _mapping_narrative(item)
                    if isinstance(item, Mapping)
                    else str(item).strip()
                )
            )
        )
        text = "; ".join(dict.fromkeys(parts)) or None
    else:
        text = str(value).strip() or None
    if text is None:
        return None
    text = public_enum_value(text)
    if field in _LONG_NARRATIVE_FIELDS and len(text) > _LONG_NARRATIVE_LIMIT:
        return f"{text[:_LONG_NARRATIVE_LIMIT].rstrip()}…"
    return text


def _mapping_narrative(value: Mapping[object, object]) -> str | None:
    preferred = (
        ("measure", "평가변수"),
        ("description", "설명"),
        ("other_names", "다른 명칭"),
        ("time_frame", "평가기간"),
        ("timeFrame", "평가기간"),
        ("name", "기관"),
        ("city", "도시"),
        ("country", "국가"),
        ("count", "수"),
        ("type", "구분"),
    )
    parts = tuple(
        f"{label} {rendered}"
        for key, label in preferred
        if (rendered := _nested_public_value(value.get(key))) is not None
    )
    return ", ".join(dict.fromkeys(parts)) or None


def _nested_public_value(value: object) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, Mapping):
        return _mapping_narrative(value)
    if isinstance(value, (list, tuple, set)):
        parts = tuple(
            rendered
            for item in value
            if (rendered := _nested_public_value(item)) is not None
        )
        return ", ".join(dict.fromkeys(parts)) or None
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
