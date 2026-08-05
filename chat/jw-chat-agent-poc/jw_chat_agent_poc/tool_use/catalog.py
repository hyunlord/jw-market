from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ToolDescriptionRecord:
    name: str
    has_spec: bool
    description: str
    not_for: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    does_not_return: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    selection_enabled: bool = True

    @property
    def catalog_description(self) -> str:
        """Return Phase B routing guidance without changing the active prompt."""

        return " ".join(
            (
                self.description,
                f"not_for: {'; '.join(self.not_for)}.",
                f"constraints: {'; '.join(self.constraints)}.",
                f"does NOT return: {'; '.join(self.does_not_return)}.",
                f"examples: {'; '.join(self.examples)}.",
            )
        )


def _record(
    name: str,
    description: str,
    *,
    not_for: tuple[str, ...],
    constraints: tuple[str, ...],
    does_not_return: tuple[str, ...],
    examples: tuple[str, ...],
    selection_enabled: bool = True,
) -> ToolDescriptionRecord:
    return ToolDescriptionRecord(
        name=name,
        has_spec=True,
        description=description,
        not_for=not_for,
        constraints=constraints,
        does_not_return=does_not_return,
        examples=examples,
        selection_enabled=selection_enabled,
    )


EXTERNAL_TOOL_DESCRIPTION_CATALOG: tuple[ToolDescriptionRecord, ...] = (
    _record(
        "local_molecule_lookup",
        "로컬 시장 DB 성분 조회. when to use: 성분/주성분/molecule. when NOT to use: 허가일/특허/부작용.",
        not_for=("허가 상세", "특허", "부작용"),
        constraints=("지원 브랜드의 로컬 성분 근거가 있어야 함",),
        does_not_return=("허가 효능", "용법", "안전성 정보"),
        examples=("리바로 주성분은?", "가드렛 molecule 알려줘"),
    ),
    _record(
        "get_drug_main_ingredient",
        "식약처 주성분 조회. when to use: 로컬 DB에 성분이 없을 때. when NOT to use: 로컬 성분 근거가 있으면 local_molecule_lookup을 사용.",
        not_for=("로컬 성분 근거가 있는 브랜드", "허가 효능과 용법"),
        constraints=("식약처 품목 응답에서 검증 가능한 성분 필드가 필요함",),
        does_not_return=("시장 매출", "보험 급여기준"),
        examples=("식약처에서 아일리아 주성분 찾아줘", "이 제품의 주성분을 확인해줘"),
    ),
    _record(
        "openfda_label_search",
        "FDA 라벨과 이상반응 조회. when to use: 부작용/adverse/side effect이면 evidence_type=adverse_event, FDA 라벨이면 evidence_type=label. when NOT to use: 성분/허가/특허.",
        not_for=("국내 품목 허가", "성분 단독 조회", "특허"),
        constraints=("미국 FDA 라벨 근거이며 국내 허가와 동일하지 않을 수 있음",),
        does_not_return=("국내 급여기준", "시장 지표"),
        examples=("pitavastatin FDA label", "이 성분의 FDA 이상반응은?"),
    ),
    _record(
        "web_search",
        "최신 동향/가이드라인/학회/KOL 웹 검색. when to use: DB에 없는 시의성 정보. 최신 가이드라인은 topic=general로 원문을 찾고, 뉴스·최근 사건처럼 발행일이 핵심인 요청만 topic=news. when NOT to use: 시장 수치/매출/점유율.",
        not_for=("내부 mart 시장 수치", "검증된 로컬 성분 조회"),
        constraints=("검색 결과의 URL과 조회 시점을 근거로 남겨야 함",),
        does_not_return=("내부 시장 원천의 확정 수치", "세션 업로드 파일 값"),
        examples=("최신 고지혈증 가이드라인", "최근 비만 치료제 뉴스"),
    ),
    _record(
        "mfds_permission_search",
        "식약처 허가 품목 검색. when to use: 제품명으로 허가 품목과 ITEM_SEQ를 찾을 때. when NOT to use: 성분만 묻고 로컬 근거가 있을 때.",
        not_for=("ITEM_SEQ가 이미 확정된 상세 조회", "dosage_unit 같은 시장 지표 단위"),
        constraints=("제품명 검색 결과에서 정확한 품목과 ITEM_SEQ를 먼저 확정해야 함",),
        does_not_return=("시장 지표 단위", "급여 인정기준", "제품 상세 필드 전문"),
        examples=("가드렛 허가 품목 찾아줘", "아일리아 ITEM_SEQ 확인해줘"),
    ),
    _record(
        "mfds_permission_detail",
        "식약처 허가 상세 조회. when to use: 허가 품목의 ITEM_SEQ가 확인된 뒤. when NOT to use: ITEM_SEQ가 없거나 특허/부작용 질의.",
        not_for=("ITEM_SEQ 없는 제품 검색", "dosage_unit 같은 시장 지표 단위"),
        constraints=("정확한 ITEM_SEQ가 필수이며 검색 단계와 혼용하지 않음",),
        does_not_return=("시장 지표 단위", "시장 매출", "보험 급여기준"),
        examples=("ITEM_SEQ 200808876의 효능효과", "이 허가 품목의 용법용량 알려줘"),
    ),
    _record(
        "mfds_composition",
        "식약처 제품별 성분·함량 조회. when to use: 제품명에 일치하는 성분 조성 근거가 필요할 때. when NOT to use: 효능·용법·주의사항 질의.",
        not_for=("효능효과", "용법용량", "주의사항"),
        constraints=("제품명과 일치하는 품목의 조성 필드만 사용함",),
        does_not_return=("허가 전문", "시장 매출"),
        examples=("아일리아 성분과 함량", "이 제품 조성 알려줘"),
    ),
    _record(
        "mfds_easy_drug",
        "식약처 e약은요 일반인용 정보 조회. when to use: 제품명과 정확히 일치하는 e약은요 원문 필드가 반환될 때. when NOT to use: 조회 결과가 없거나 다른 제품의 일반인용 정보.",
        not_for=("다른 제품의 유사 문서", "전문가용 허가 상세"),
        constraints=("정확히 일치하는 제품의 e약은요 원문만 사용함",),
        does_not_return=("시장 지표", "급여 인정기준"),
        examples=("일반인용 리바로 복약정보", "이 약을 어떻게 복용해요?"),
    ),
    _record(
        "mfds_clinical_trial_kr",
        "국내 임상시험 조회. when to use: 국내/한국/식약처 임상시험이라고 명시한 질의. when NOT to use: 비한정 임상시험, 글로벌 임상, NCT 질의.",
        not_for=("글로벌 임상 검색", "정확한 NCT ID 상세"),
        constraints=("국내 식약처 임상시험 범위로 한정함",),
        does_not_return=("ClinicalTrials.gov 전체 결과", "시장 매출"),
        examples=("한국에서 진행 중인 리바로 임상", "식약처 국내 임상시험 찾아줘"),
    ),
    _record(
        "clinicaltrials_v2_search",
        "ClinicalTrials.gov 글로벌 임상 조회. when to use: 지역을 한정하지 않은 비한정 임상시험, 글로벌 임상, NCT 질의. when NOT to use: 국내/한국/식약처 임상만 명시한 질의.",
        not_for=("국내 식약처 임상만 요청", "정확한 NCT ID의 상세 필드 조회"),
        constraints=("검색 조건에 맞는 시험 목록을 반환하며 NCT ID 없는 탐색에 사용함",),
        does_not_return=("개별 시험의 전체 상세", "식약처 허가 현황"),
        examples=("뇌경색 글로벌 임상시험 찾아줘", "비만 치료제 임상 검색"),
    ),
    _record(
        "clinicaltrials_study_details",
        "ClinicalTrials.gov NCT 상세 조회. when to use: 정확한 NCT ID가 있는 질의. 선정·제외 기준은 원문 앞 200자까지만 제공됨을 고지합니다. when NOT to use: NCT ID 없는 임상 검색.",
        not_for=("NCT ID 없는 임상 검색", "국내 식약처 임상 목록"),
        constraints=("정확한 NCT ID가 필수이고 선정·제외 기준은 앞 200자만 제공됨",),
        does_not_return=("조건별 임상시험 목록", "허가 품목 정보"),
        examples=("NCT05151731 상세", "NCT01234567의 평가변수 알려줘"),
    ),
    _record(
        "mfds_patent",
        "국내 의약품 특허 조회. when to use: 국내 특허/독점권 질의. when NOT to use: 미국 Orange Book만 묻는 질의.",
        not_for=("미국 Orange Book 전용 질의", "일반 허가 상세"),
        constraints=("국내 의약품 특허 근거 범위로 한정함",),
        does_not_return=("미국 독점권", "시장 매출"),
        examples=("리바로 국내 특허", "이 성분의 한국 특허 만료일"),
    ),
    _record(
        "mfds_fda_orangebook",
        "FDA Orange Book 조회. when to use: 미국 특허/독점권 질의. when NOT to use: 국내 특허만 묻는 질의.",
        not_for=("국내 특허 전용 질의", "식약처 효능효과"),
        constraints=("미국 Orange Book 근거 범위로 한정함",),
        does_not_return=("국내 특허 상태", "보험 급여기준"),
        examples=("pitavastatin Orange Book", "미국 독점권 만료일"),
    ),
    _record(
        "hira_disease_name_code",
        "HIRA 질병명과 상병코드 조회. when to use: 질병명/상병코드 확인. when NOT to use: 이미 확정된 코드의 환자 통계.",
        not_for=("확정 상병코드의 환자수", "진료행위 코드"),
        constraints=("질병명을 통계 조회용 상병코드로 해소하는 선행 도구임",),
        does_not_return=("환자 통계", "급여 인정기준"),
        examples=("뇌경색 상병코드", "D69.3은 무슨 질병이야?"),
    ),
    _record(
        "hira_disease_hospitalization_outpatient_stats",
        "HIRA 질병 입원/외래 통계. when to use: 상병코드별 입원·외래 환자 질의. when NOT to use: 진료행위 코드 통계.",
        not_for=("진료행위 코드 통계", "급여 인정기준"),
        constraints=("확정된 상병코드와 조회 기간이 필요함",),
        does_not_return=("보험 급여 기준", "시장 매출"),
        examples=("I63 입원 외래 환자수", "D69.3 환자 통계"),
    ),
    _record(
        "hira_disease_gender_age_stats",
        "HIRA 질병 성별/연령 통계. when to use: 상병코드별 성별·연령 질의. when NOT to use: 지역/기관종별 통계.",
        not_for=("지역 통계", "기관종별 통계"),
        constraints=("확정된 상병코드 기준으로 성별·연령 축만 조회함",),
        does_not_return=("입원·외래 구분", "급여 기준"),
        examples=("I63 연령별 환자수", "D69.3 성별 통계"),
    ),
    _record(
        "hira_disease_institution_class_stats",
        "HIRA 질병 요양기관 종별 통계. when to use: 상병코드별 기관종별 질의. when NOT to use: 성별/연령/지역 통계.",
        not_for=("성별·연령 통계", "지역 통계"),
        constraints=("확정된 상병코드 기준으로 요양기관 종별 축만 조회함",),
        does_not_return=("진료행위 통계", "급여 기준"),
        examples=("I63 기관종별 환자수", "D69.3 병원급별 통계"),
    ),
    _record(
        "hira_disease_area_stats",
        "HIRA 질병 지역 통계. when to use: 상병코드별 지역 질의. when NOT to use: 진료행위 코드 통계.",
        not_for=("진료행위 코드 통계", "성별·연령 통계"),
        constraints=("확정된 상병코드 기준으로 지역 축만 조회함",),
        does_not_return=("급여 인정기준", "시장 지표"),
        examples=("I63 지역별 환자수", "D69.3 시도별 통계"),
    ),
    _record(
        "hira_reimbursement_criteria",
        "심사평가원 보험인정기준 조회. when to use: 제품별 급여기준·보험인정기준 질의. when NOT to use: 식약처 허가 효능·용법 또는 상병코드 환자 통계.",
        not_for=("식약처 허가 효능·용법", "상병코드 환자 통계"),
        constraints=("제품별 HIRA 보험인정기준 근거에 한정하며 최신성 시점을 표시함",),
        does_not_return=("질병 환자수", "식약처 허가 전문"),
        examples=("가드렛 급여기준", "아일리아 보험 인정기준"),
    ),
    _record(
        "hira_procedure_gender_ipat_opat_stats",
        "HIRA 진료행위 입원/외래 통계. when to use: 행위코드별 입원·외래 질의. when NOT to use: 상병코드 통계.",
        not_for=("상병코드 환자 통계", "성별·연령 분해"),
        constraints=("확정된 진료행위 코드가 필요함",),
        does_not_return=("질병 통계", "급여 인정기준"),
        examples=("행위코드 C123 입원 외래 통계", "이 시술의 외래 건수"),
    ),
    _record(
        "hira_procedure_gender_age_stats",
        "HIRA 진료행위 성별/연령 통계. when to use: 행위코드별 성별·연령 질의. when NOT to use: 질병 상병코드 통계.",
        not_for=("질병 상병코드 통계", "기관종별 통계"),
        constraints=("확정된 진료행위 코드 기준으로 성별·연령 축만 조회함",),
        does_not_return=("질병 환자수", "지역 통계"),
        examples=("행위코드 C123 연령별 건수", "이 시술의 성별 통계"),
    ),
    _record(
        "hira_procedure_institution_class_stats",
        "HIRA 진료행위 기관종별 통계. when to use: 행위코드별 기관종별 질의. when NOT to use: 성별/연령/지역 통계.",
        not_for=("성별·연령 통계", "지역 통계"),
        constraints=("확정된 진료행위 코드 기준으로 기관종별 축만 조회함",),
        does_not_return=("질병 통계", "급여 기준"),
        examples=("행위코드 C123 기관종별 건수", "이 시술의 병원급별 통계"),
    ),
    _record(
        "hira_procedure_area_stats",
        "HIRA 진료행위 지역 통계. when to use: 행위코드별 지역 질의. when NOT to use: 질병 상병코드 통계.",
        not_for=("질병 상병코드 통계", "기관종별 통계"),
        constraints=("확정된 진료행위 코드 기준으로 지역 축만 조회함",),
        does_not_return=("질병 환자수", "시장 지표"),
        examples=("행위코드 C123 지역별 건수", "이 시술의 시도별 통계"),
    ),
)


INTERNAL_TOOL_DESCRIPTION_CATALOG: tuple[ToolDescriptionRecord, ...] = (
    _record(
        "market.get_brand_metric",
        "내부 mart의 브랜드 단일 지표 조회. 전략시장 멤버십이 있으면 전략뷰, 없으면 브랜드의 단일 ATC4 일반뷰를 사용한다. when to use: 브랜드 매출·점유율·순위의 특정 기간 값. when NOT to use: 시장 전체 규모나 규제·급여 정보.",
        not_for=("시장 전체 규모", "규제·급여 정보"),
        constraints=("brand·metric·period와 호환 가능한 scope·source가 필요하며 일반뷰는 단일 ATC4 또는 확인된 복합 필터 scope를 사용함",),
        does_not_return=("시장 정의 변경 사유", "규제 정보"),
        examples=("리바로 2026-05 UBIST 매출", "가드렛 최근 점유율"),
        selection_enabled=False,
    ),
    _record(
        "market.get_market_size",
        "내부 mart의 시장 전체 규모 조회. 전략시장 멤버십이 없는 브랜드는 단일 ATC4 일반뷰 시장을 사용한다. when to use: 브랜드가 속한 시장의 전체 매출 규모. when NOT to use: 개별 브랜드 매출이나 비교.",
        not_for=("개별 브랜드 매출", "다중 브랜드 비교"),
        constraints=("anchor brand 또는 scope로 시장 범위를 해소하며 일반뷰는 단일 ATC4 또는 확인된 복합 필터 scope를 사용함",),
        does_not_return=("시장 정의 변경 사유", "브랜드별 시계열"),
        examples=("리바로가 속한 시장 규모", "고지혈증 시장 전체 크기"),
        selection_enabled=False,
    ),
    _record(
        "market.get_market_members",
        "내부 mart의 시장 구성 브랜드 조회. 전략시장 멤버십이 없는 브랜드는 단일 ATC4 일반뷰 구성원을 사용한다. when to use: 시장에 포함된 브랜드 목록과 범위. when NOT to use: 브랜드별 수치나 정의 변경 사유.",
        not_for=("브랜드별 매출 값", "시장 정의 변경 사유"),
        constraints=("anchor brand 또는 scope로 시장을 해소하며 일반뷰는 단일 ATC4 또는 확인된 복합 필터 scope를 사용함",),
        does_not_return=("브랜드 시계열", "정의 변경 이력"),
        examples=("리바로 시장 구성 브랜드", "이 시장에 어떤 제품이 있어?"),
        selection_enabled=False,
    ),
    _record(
        "market.get_timeseries",
        "내부 mart의 브랜드 기간별 추이 조회. 전략시장 멤버십이 없으면 단일 ATC4 일반뷰의 브랜드 이력을 사용한다. when to use: 매출·점유율 등 한 지표의 시계열. when NOT to use: 단일 시점 값이나 미래 예측.",
        not_for=("단일 시점 값", "미래 예측"),
        constraints=("brand·metric·period·source·scope 축이 호환되어야 함",),
        does_not_return=("예측치", "원인 추론"),
        examples=("리바로 최근 12개월 매출 추이", "가드렛 점유율 시계열"),
        selection_enabled=False,
    ),
    _record(
        "market.get_channel_breakdown",
        "내부 mart의 채널별 분해 조회. when to use: 브랜드 매출을 병원·의원 등 채널로 분해. when NOT to use: 단일 ATC4 일반뷰 기본 표면, 진료과 분해나 전체 시장 목록.",
        not_for=("진료과 분해", "시장 구성 브랜드 목록"),
        constraints=("채널 축을 제공하는 source와 general_composite scope 조합이어야 함",),
        does_not_return=("채널 변화의 원인", "규제 정보"),
        examples=("리바로 채널별 매출", "가드렛 병원 의원 비중"),
        selection_enabled=False,
    ),
    _record(
        "market.get_hhi",
        "내부 mart의 시장 집중도 HHI 조회. 전략시장 멤버십이 없으면 단일 ATC4 일반뷰의 사전 계산 HHI를 사용한다. when to use: 특정 시장의 경쟁 집중도. when NOT to use: 개별 브랜드 점유율이나 집중도 원인 설명.",
        not_for=("개별 브랜드 점유율", "집중도 원인 추론"),
        constraints=("동일 scope·source·period의 브랜드 값으로 계산된 HHI만 사용하며 일반뷰 값은 mart 정밀도를 보존함",),
        does_not_return=("인과 설명", "시장 정의 변경 사유"),
        examples=("리바로 시장 HHI", "이 시장 집중도 알려줘"),
        selection_enabled=False,
    ),
    _record(
        "market.get_growth_contribution",
        "내부 mart의 브랜드 성장 기여도 조회. when to use: 전략시장의 전년 대비 절대 성장에서 브랜드 기여 비중. when NOT to use: 단일 ATC4 일반뷰 기본 표면, 단순 성장률이나 인과 분석.",
        not_for=("단순 성장률", "성장 원인 추론"),
        constraints=("같은 strategic 또는 general_composite scope·source의 비교 가능한 기간 값이 필요함",),
        does_not_return=("인과 설명", "미래 성장 예측"),
        examples=("리바로 시장 성장 기여도", "어떤 브랜드가 성장에 기여했어?"),
        selection_enabled=False,
    ),
    _record(
        "market.compare_brands",
        "내부 mart의 다중 브랜드 비교 조회. 전략시장 멤버십이 없으면 두 브랜드가 같은 단일 ATC4 일반뷰에 있는지 확인한다. when to use: 같은 시장·소스·지표에서 두 브랜드 이상 비교. when NOT to use: 서로 다른 소스 수치의 직접 비교.",
        not_for=("서로 다른 source의 직접 비교", "시장 정의가 다른 브랜드 비교"),
        constraints=("비교 브랜드가 같은 scope·source·metric 축에서 호환되어야 함",),
        does_not_return=("비교 우열의 인과 설명", "규제 정보"),
        examples=("리바로와 리피토 매출 비교", "두 브랜드 점유율 추이 비교"),
        selection_enabled=False,
    ),
    _record(
        "market.get_definition",
        "기존 시장 카탈로그에서 일반뷰 또는 전략뷰의 정의와 브랜드 포함 조건을 조회한다. when to use: ATC4 일반뷰 정의, market_landscape·competitive_dynamics 전략뷰 구성, 브랜드가 정의 조건에 포함되는지 확인, 사유 질문에서 기록된 정의와 선정 사유 부재를 구분. when NOT to use: 시장 선정 사유나 의사결정 배경 추론.",
        not_for=("시장 선정 사유 추론", "정의 변경의 조직적 의사결정 배경"),
        constraints=("일반뷰는 ATC4 단일 코드 기준이며 market_landscape와 competitive_dynamics는 모두 전략뷰임", "런타임 카탈로그에 실제 기록된 조건만 반환함"),
        does_not_return=("시장 선정 사유", "의사결정 배경", "기록되지 않은 분류 이유"),
        examples=("악템라 시장은 어떻게 정의돼?", "competitive_dynamics는 market_landscape에서 무엇이 달라?", "왜 전략시장을 골랐나? 선정 사유 미기록 여부 확인"),
        selection_enabled=False,
    ),
    _record(
        "market.get_deep_analysis",
        "기존 심층분석 API의 시스템 예측·시뮬레이션·브랜드 프로파일을 조회한다. when to use: 브랜드 매출/처방량 예측, 사전 계산된 시뮬레이션, 브랜드 프로파일링. when NOT to use: 실적 조회, 임의 추세 외삽, 일반 복합시장 분석.",
        not_for=("과거 실적 단독 조회", "LLM의 임의 추세 외삽", "general_composite scope"),
        constraints=("general·strategic_ml·strategic_cd 중 하나와 해당 market_id·source가 필요함", "예측·시뮬레이션 값은 시스템 예측으로 명시해야 함"),
        does_not_return=("의사결정 배경", "모델 예측의 근거·가정", "AI 인사이트를 사실로 확정한 결과"),
        examples=("리바로 시스템 예측 매출", "아일리아 사전 계산 시뮬레이션", "리바로 브랜드 프로파일링"),
        selection_enabled=False,
    ),
    _record(
        "file.get_schema",
        "세션 업로드 파일의 스키마 조회. when to use: 시트·컬럼 구조를 먼저 확인. when NOT to use: 셀 값이나 집계 결과 조회.",
        not_for=("셀 값 조회", "집계 결과 조회"),
        constraints=("현재 세션에 귀속된 파일 source만 조회함",),
        does_not_return=("셀 데이터", "파일 외부 DB 데이터"),
        examples=("업로드 파일 컬럼 알려줘", "이 엑셀 구조를 보여줘"),
        selection_enabled=False,
    ),
    _record(
        "file.query",
        "세션 업로드 파일의 read-only 질의. when to use: 파일 내 필터·집계·비교. when NOT to use: 파일 수정이나 시스템 mart 조회.",
        not_for=("파일 수정", "시스템 mart 시장 조회"),
        constraints=("현재 세션 소유 파일에 대한 read-only 쿼리만 허용함",),
        does_not_return=("파일 쓰기 결과", "세션 밖 문서"),
        examples=("업로드 파일에서 제품별 매출 합계", "이 파일의 상위 10개 제품"),
        selection_enabled=False,
    ),
)


TOOL_DESCRIPTION_CATALOG = (
    EXTERNAL_TOOL_DESCRIPTION_CATALOG + INTERNAL_TOOL_DESCRIPTION_CATALOG
)
