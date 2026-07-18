from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolDescriptionRecord:
    name: str
    has_spec: bool
    description: str


TOOL_DESCRIPTION_CATALOG: tuple[ToolDescriptionRecord, ...] = (
    ToolDescriptionRecord("local_molecule_lookup", True, "로컬 시장 DB 성분 조회. when to use: 성분/주성분/molecule. when NOT to use: 허가일/특허/부작용."),
    ToolDescriptionRecord("get_drug_main_ingredient", True, "식약처 주성분 조회. when to use: 로컬 DB에 성분이 없을 때. when NOT to use: 로컬 성분 근거가 있으면 local_molecule_lookup을 사용."),
    ToolDescriptionRecord("openfda_label_search", True, "FDA 라벨과 이상반응 조회. when to use: 부작용/adverse/side effect이면 evidence_type=adverse_event, FDA 라벨이면 evidence_type=label. when NOT to use: 성분/허가/특허."),
    ToolDescriptionRecord("web_search", True, "최신 동향/가이드라인/학회/KOL 웹 검색. when to use: DB에 없는 시의성 정보. 최신 가이드라인은 topic=general로 원문을 찾고, 뉴스·최근 사건처럼 발행일이 핵심인 요청만 topic=news. when NOT to use: 시장 수치/매출/점유율."),
    ToolDescriptionRecord("mfds_permission_search", True, "식약처 허가 품목 검색. when to use: 제품명으로 허가 품목과 ITEM_SEQ를 찾을 때. when NOT to use: 성분만 묻고 로컬 근거가 있을 때."),
    ToolDescriptionRecord("mfds_permission_detail", True, "식약처 허가 상세 조회. when to use: 허가 품목의 ITEM_SEQ가 확인된 뒤. when NOT to use: ITEM_SEQ가 없거나 특허/부작용 질의."),
    ToolDescriptionRecord("mfds_clinical_trial_kr", True, "국내 임상시험 조회. when to use: 국내/한국/식약처 임상시험이라고 명시한 질의. when NOT to use: 비한정 임상시험, 글로벌 임상, NCT 질의."),
    ToolDescriptionRecord("clinicaltrials_v2_search", True, "ClinicalTrials.gov 글로벌 임상 조회. when to use: 지역을 한정하지 않은 비한정 임상시험, 글로벌 임상, NCT 질의. when NOT to use: 국내/한국/식약처 임상만 명시한 질의."),
    ToolDescriptionRecord("mfds_patent", True, "국내 의약품 특허 조회. when to use: 국내 특허/독점권 질의. when NOT to use: 미국 Orange Book만 묻는 질의."),
    ToolDescriptionRecord("mfds_fda_orangebook", True, "FDA Orange Book 조회. when to use: 미국 특허/독점권 질의. when NOT to use: 국내 특허만 묻는 질의."),
    ToolDescriptionRecord("hira_disease_name_code", True, "HIRA 질병명과 상병코드 조회. when to use: 질병명/상병코드 확인. when NOT to use: 이미 확정된 코드의 환자 통계."),
    ToolDescriptionRecord("hira_disease_hospitalization_outpatient_stats", True, "HIRA 질병 입원/외래 통계. when to use: 상병코드별 입원·외래 환자 질의. when NOT to use: 진료행위 코드 통계."),
    ToolDescriptionRecord("hira_disease_gender_age_stats", True, "HIRA 질병 성별/연령 통계. when to use: 상병코드별 성별·연령 질의. when NOT to use: 지역/기관종별 통계."),
    ToolDescriptionRecord("hira_disease_institution_class_stats", True, "HIRA 질병 요양기관 종별 통계. when to use: 상병코드별 기관종별 질의. when NOT to use: 성별/연령/지역 통계."),
    ToolDescriptionRecord("hira_disease_area_stats", True, "HIRA 질병 지역 통계. when to use: 상병코드별 지역 질의. when NOT to use: 진료행위 코드 통계."),
    ToolDescriptionRecord("hira_procedure_gender_ipat_opat_stats", True, "HIRA 진료행위 입원/외래 통계. when to use: 행위코드별 입원·외래 질의. when NOT to use: 상병코드 통계."),
    ToolDescriptionRecord("hira_procedure_gender_age_stats", True, "HIRA 진료행위 성별/연령 통계. when to use: 행위코드별 성별·연령 질의. when NOT to use: 질병 상병코드 통계."),
    ToolDescriptionRecord("hira_procedure_institution_class_stats", True, "HIRA 진료행위 기관종별 통계. when to use: 행위코드별 기관종별 질의. when NOT to use: 성별/연령/지역 통계."),
    ToolDescriptionRecord("hira_procedure_area_stats", True, "HIRA 진료행위 지역 통계. when to use: 행위코드별 지역 질의. when NOT to use: 질병 상병코드 통계."),
)
