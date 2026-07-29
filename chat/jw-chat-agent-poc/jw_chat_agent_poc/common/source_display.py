from __future__ import annotations

import re
from typing import Final


TOOL_STEP_LABELS: Final[dict[str, str]] = {
    "get_metric": "시장 지표 조회",
    "get_market_scope": "시장 범위 확인",
    "get_brand_metric": "시장 데이터 집계",
    "get_brand_sales": "브랜드 매출 조회",
    "get_brand_share": "브랜드 점유율 확인",
    "get_brand_series": "브랜드 추이 확인",
    "get_top_brands": "상위 브랜드 확인",
    "get_market_landscape": "경쟁 구도 조회",
    "local_molecule_lookup": "성분 정보 조회 중",
    "get_drug_main_ingredient": "NeDrug 주성분 조회 중",
    "openfda_label_search": "OpenFDA 안전성정보 조회 중",
    "openfda_combo_label_search": "OpenFDA 복합제 안전성정보 조회 중",
    "web_search": "최신 웹 자료 검색",
    "mfds_permission_search": "NeDrug 허가정보 조회 중",
    "mfds_permission_detail": "NeDrug 허가상세 조회 중",
    "mfds_composition": "NeDrug 성분정보 조회 중",
    "mfds_easy_drug": "NeDrug 복약정보 조회 중",
    "mfds_clinical_trial_kr": "NeDrug 국내 임상정보 조회 중",
    "clinicaltrials_v2_search": "ClinicalTrials.gov 조회 중",
    "clinicaltrials_study_details": "ClinicalTrials.gov 상세 조회 중",
    "mfds_patent": "NeDrug 특허정보 조회 중",
    "mfds_fda_orangebook": "FDA Orange Book 조회 중",
    "get_patent_expiry": "NeDrug 특허정보 조회 중",
    "hira_disease_name_code": "HIRA 질병코드 조회 중",
    "hira_disease_hospitalization_outpatient_stats": "HIRA 질병통계 조회 중",
    "hira_disease_gender_age_stats": "HIRA 성별·연령 질병통계 조회 중",
    "hira_disease_institution_class_stats": "HIRA 의료기관별 질병통계 조회 중",
    "hira_disease_area_stats": "HIRA 지역별 질병통계 조회 중",
    "hira_reimbursement_criteria": "HIRA 보험인정기준 조회 중",
    "hira_procedure_gender_ipat_opat_stats": "HIRA 진료행위통계 조회 중",
    "hira_procedure_gender_age_stats": "HIRA 성별·연령 진료행위통계 조회 중",
    "hira_procedure_institution_class_stats": "HIRA 의료기관별 진료행위통계 조회 중",
    "hira_procedure_area_stats": "HIRA 지역별 진료행위통계 조회 중",
    "search_clinical": "임상시험 통합 조회",
    "clinical_scope_notice": "임상 조회 범위 확인",
    "competitor_molecule_candidates": "경쟁 성분 확인",
    "search_drug_info": "식약처 허가 정보 확인",
    "search_patent": "특허 정보 통합 조회",
    "search_safety": "FDA 안전성 정보 확인",
    "hira_disease": "건강보험 환자 정보 확인",
    "get_disease_stats": "건강보험 환자 정보 확인",
    "search_news": "뉴스·이슈 확인",
    "csd_activity_trend": "영업 활동 추이 확인",
    "matching_policy_notice": "의약품 일치 기준 확인",
}

TOOL_SOURCE_LABELS: Final[dict[str, str]] = {
    "local_molecule_lookup": "JW 성분 기준정보",
    "get_drug_main_ingredient": "식약처 의약품안전나라(NeDrug)",
    "mfds_permission_search": "식약처 의약품안전나라(NeDrug)",
    "mfds_permission_detail": "식약처 의약품안전나라(NeDrug)",
    "mfds_composition": "식약처 의약품안전나라(NeDrug)",
    "mfds_easy_drug": "식약처 의약품안전나라(NeDrug)",
    "mfds_clinical_trial_kr": "식약처 의약품안전나라(NeDrug)",
    "mfds_patent": "식약처 의약품안전나라(NeDrug)",
    "mfds_fda_orangebook": "FDA Orange Book",
    "get_patent_expiry": "식약처 의약품 특허 정보",
    "openfda_label_search": "OpenFDA",
    "openfda_combo_label_search": "OpenFDA",
    "clinicaltrials_v2_search": "ClinicalTrials.gov",
    "clinicaltrials_study_details": "ClinicalTrials.gov",
    "hira_disease_name_code": "심사평가원(HIRA) 질병통계",
    "hira_disease_hospitalization_outpatient_stats": "심사평가원(HIRA) 질병통계",
    "hira_disease_gender_age_stats": "심사평가원(HIRA) 질병통계",
    "hira_disease_institution_class_stats": "심사평가원(HIRA) 질병통계",
    "hira_disease_area_stats": "심사평가원(HIRA) 질병통계",
    "hira_reimbursement_criteria": "심사평가원(HIRA) 보험인정기준",
    "hira_procedure_gender_ipat_opat_stats": "심사평가원(HIRA) 진료행위통계",
    "hira_procedure_gender_age_stats": "심사평가원(HIRA) 진료행위통계",
    "hira_procedure_institution_class_stats": "심사평가원(HIRA) 진료행위통계",
    "hira_procedure_area_stats": "심사평가원(HIRA) 진료행위통계",
    "web_search": "웹 검색 결과(미검증)",
}

SOURCE_LABELS: Final[dict[str, str]] = {
    "cache": "UBIST",
    "metrics": "UBIST",
    "UBIST": "UBIST",
    "IQVIA": "IQVIA",
    "jw-market-direct-mart": "JW Market 직접 Mart",
    "jw-market-backend-api": "JW Market Backend API",
    "external": "외부 데이터 원천",
    "external_api": "외부 데이터 원천",
    "hira_disease": "심사평가원(HIRA) 질병통계",
    "hira_procedure": "심사평가원(HIRA) 진료행위통계",
    "web_search": "웹 검색 결과(미검증)",
    "deep_analysis_events": "뉴스/이슈",
    "nedrug_mcp": "식약처 의약품안전나라(NeDrug)",
    "openfda_mcp": "OpenFDA",
    "clinicaltrials_mcp": "ClinicalTrials.gov",
    "document": "업로드 문서",
    "none": "데이터 없음",
    "unsupported_brand": "브랜드 식별 미확인",
    "ambiguous_brand": "브랜드 식별 후보",
    "strategic_market_not_member": "전략시장 정의 미포함",
    "brand_unresolved": "브랜드 식별 미지정",
}

LEGACY_SOURCE_LABELS: Final[dict[str, str]] = {
    "HIRA 질병정보서비스": "심사평가원(HIRA) 질병통계",
    "식약처 의약품 정보": "식약처 의약품안전나라(NeDrug)",
    "식약처 의약품 허가 상세": "식약처 의약품안전나라(NeDrug)",
    "ClinicalTrials.gov 임상시험 정보": "ClinicalTrials.gov",
    "ClinicalTrials.gov 임상시험 상세": "ClinicalTrials.gov",
}

_MACHINE_IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(
    r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*"
)
_EXTERNAL_TOOL_HINT_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:external|provider|web|hira|mfds|nedrug|openfda|clinical|drug|patent)"
)


def tool_step_label(tool_name: str) -> str:
    label = TOOL_STEP_LABELS.get(tool_name)
    if label:
        return label
    if _EXTERNAL_TOOL_HINT_RE.search(tool_name):
        return "외부 데이터 조회 중"
    return "관련 데이터 조회"


def source_label_for_tool(tool_name: str) -> str:
    return TOOL_SOURCE_LABELS.get(tool_name, "")


def public_source_label(source: str | None) -> str:
    value = "" if source is None else str(source).strip()
    if not value:
        return "도구 결과"
    label = SOURCE_LABELS.get(value)
    if label:
        return label
    label = LEGACY_SOURCE_LABELS.get(value)
    if label:
        return label
    legacy_source, separator, detail = value.partition(" · ")
    label = LEGACY_SOURCE_LABELS.get(legacy_source)
    if label and separator:
        return f"{label}{separator}{detail}"
    if _MACHINE_IDENTIFIER_RE.fullmatch(value):
        return "외부 데이터 원천"
    return value
