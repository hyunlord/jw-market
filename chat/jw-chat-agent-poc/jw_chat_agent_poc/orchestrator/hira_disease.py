from __future__ import annotations

from typing import Protocol, TypeAlias, TypedDict

from jw_chat_agent_poc.tools.external import ExternalApiClient, ExternalCall


class HiraResolution(Protocol):
    canonical_brand: str


class HiraMapping(TypedDict):
    sick_cd: str
    disease_name: str
    basis: str


class HiraUnsuitable(TypedDict):
    reason: str
    reason_label: str
    basis: str


HiraMappingEntry: TypeAlias = HiraMapping | tuple[HiraMapping, ...]


def _hira_mapping(sick_cd: str, disease_name: str, basis: str) -> HiraMapping:
    return {"sick_cd": sick_cd, "disease_name": disease_name, "basis": basis}


HIRA_DISEASE_MAPPINGS: dict[str, HiraMappingEntry] = {
    "라베칸": _hira_mapping("K21", "위-식도역류병", "MFDS 효능효과의 위식도역류질환 적응증 + HIRA getDissNameCodeList1 SICK_CD=K21 실호출 확인"),
    "라베칸듀오": _hira_mapping("K21", "위-식도역류병", "MFDS 효능효과의 위식도역류질환 적응증 + HIRA getDissNameCodeList1 SICK_CD=K21 실호출 확인"),
    "가드렛": _hira_mapping("E11", "2형 당뇨병", "MFDS 효능효과의 제2형 당뇨병 적응증 + HIRA getDissNameCodeList1 SICK_CD=E11 실호출 확인"),
    "타발리스": _hira_mapping("D69", "자반 및 기타 출혈성 병태", "MFDS 효능효과의 만성 면역 혈소판 감소증 적응증 + HIRA SICK_NM 자반/D69 실호출 확인"),
    "시그마트": _hira_mapping("I20", "협심증", "MFDS 효능효과의 협심증 적응증 + HIRA getDissNameCodeList1 SICK_CD=I20 실호출 확인"),
    "리바로": _hira_mapping("E78", "지질단백질대사장애 및 기타 지질증", "HIRA getDissNameCodeList1 SICK_CD=E78 실호출 확인"),
    "리바로젯": _hira_mapping("E78", "지질단백질대사장애 및 기타 지질증", "HIRA getDissNameCodeList1 SICK_CD=E78 실호출 확인"),
    "리바로페노": _hira_mapping("E78", "지질단백질대사장애 및 기타 지질증", "MFDS 효능효과의 복합형 이상지질혈증 적응증 + HIRA getDissNameCodeList1 SICK_CD=E78 실호출 확인"),
    "리바로하이": (
        _hira_mapping("I10", "본태성 고혈압", "MFDS 효능효과의 본태성 고혈압 적응증 + HIRA getDissNameCodeList1 SICK_CD=I10 실호출 확인"),
        _hira_mapping("E78", "지질단백질대사장애 및 기타 지질증", "MFDS 효능효과의 원발성 고콜레스테롤혈증/혼합형 이상지질혈증 적응증 + HIRA getDissNameCodeList1 SICK_CD=E78 실호출 확인"),
    ),
    "리바로브이": (
        _hira_mapping("E78", "지질단백질대사장애 및 기타 지질증", "MFDS 효능효과의 고콜레스테롤혈증/혼합형 이상지질혈증 적응증 + HIRA getDissNameCodeList1 SICK_CD=E78 실호출 확인"),
        _hira_mapping("I10", "본태성 고혈압", "MFDS 효능효과의 고혈압 동반 심혈관계 위험 적응증 + HIRA getDissNameCodeList1 SICK_CD=I10 실호출 확인"),
    ),
    "트루패스": _hira_mapping("N40", "전립선증식증", "MFDS 효능효과의 전립선 비대증에 수반하는 배뇨장애 적응증 + HIRA getDissNameCodeList1 SICK_CD=N40 실호출 확인"),
    "피나스타": _hira_mapping("N40", "전립선증식증", "MFDS 효능효과의 양성전립샘비대증 적응증 + HIRA getDissNameCodeList1 SICK_CD=N40 실호출 확인"),
    "제이다트": _hira_mapping("N40", "전립선증식증", "MFDS 효능효과의 양성 전립선 비대증 적응증 + HIRA getDissNameCodeList1 SICK_CD=N40 실호출 확인"),
    "뉴트로진": _hira_mapping("D70", "무과립구증", "MFDS 효능효과의 항암화학요법 관련 호중구감소증 등 적응증 + HIRA getDissNameCodeList1 SICK_CD=D70 실호출 확인"),
    "가드메트": _hira_mapping("E11", "2형 당뇨병", "HIRA getDissNameCodeList1 SICK_CD=E11 실호출 확인"),
    "악템라": _hira_mapping("M05", "혈청검사양성 류마티스관절염", "HIRA getDissNameCodeList1 SICK_CD=M05 실호출 확인; M06는 보조 후보"),
    "페린젝트": _hira_mapping("D50", "철결핍빈혈", "HIRA getDissNameCodeList1 SICK_CD=D50 실호출 확인"),
    "베노훼럼": _hira_mapping("D50", "철결핍빈혈", "HIRA getDissNameCodeList1 SICK_CD=D50 실호출 확인"),
    "헴리브라": _hira_mapping("D66", "유전성 제8인자결핍", "HIRA getDissNameCodeList1 SICK_CD=D66 실호출 확인"),
}

HIRA_DISEASE_UNSUITABLE_BRANDS: dict[str, HiraUnsuitable] = {
    "제이클": {
        "reason": "nutrition_infusion_or_procedure_product",
        "reason_label": "영양/수액/처치제로 특정 질병 유병 통계 조회가 부적합",
        "basis": "복수 적응증 질병 유병 대상이 아니라 처치·전처치/영양 보조 성격으로 HIRA KCD 매핑을 억지로 부여하지 않음",
    },
    "위너프": {
        "reason": "nutrition_infusion_or_procedure_product",
        "reason_label": "영양/수액/처치제로 특정 질병 유병 통계 조회가 부적합",
        "basis": "수액/영양 공급 제품은 특정 질병 유병 통계를 대표하지 않으므로 HIRA KCD 매핑 제외",
    },
    "위너프A+": {
        "reason": "nutrition_infusion_or_procedure_product",
        "reason_label": "영양/수액/처치제로 특정 질병 유병 통계 조회가 부적합",
        "basis": "수액/영양 공급 제품은 특정 질병 유병 통계를 대표하지 않으므로 HIRA KCD 매핑 제외",
    },
    "엔커버": {
        "reason": "nutrition_infusion_or_procedure_product",
        "reason_label": "영양/수액/처치제로 특정 질병 유병 통계 조회가 부적합",
        "basis": "영양 공급 제품은 특정 질병 유병 통계를 대표하지 않으므로 HIRA KCD 매핑 제외",
    },
    "모빌리아": {
        "reason": "nutrition_infusion_or_procedure_product",
        "reason_label": "영양/수액/처치제로 특정 질병 유병 통계 조회가 부적합",
        "basis": "처치 보조 성격의 제품은 특정 질병 유병 통계를 대표하지 않으므로 HIRA KCD 매핑 제외",
    },
}

HIRA_DISEASE_TEXT_MAPPINGS: dict[str, HiraMappingEntry] = {
    "이상지질": HIRA_DISEASE_MAPPINGS["리바로"],
    "고지혈": HIRA_DISEASE_MAPPINGS["리바로"],
    "지질단백질": HIRA_DISEASE_MAPPINGS["리바로"],
    "당뇨": HIRA_DISEASE_MAPPINGS["가드메트"],
    "혈우": HIRA_DISEASE_MAPPINGS["헴리브라"],
    "빈혈": HIRA_DISEASE_MAPPINGS["페린젝트"],
    "류마티스": HIRA_DISEASE_MAPPINGS["악템라"],
}


def is_hira_disease_question(question: str) -> bool:
    return any(
        token in question
        for token in (
            "환자수",
            "환자 수",
            "환자통계",
            "환자 통계",
            "환자분포",
            "환자 분포",
            "질병통계",
            "질병 통계",
            "질환통계",
            "질환 통계",
            "질병 환자",
            "질환 환자",
            "관련 질병",
            "관련 질환",
        )
    )


def hira_disease_calls(question: str, resolution: HiraResolution, external: ExternalApiClient) -> list[ExternalCall]:
    unsuitable = HIRA_DISEASE_UNSUITABLE_BRANDS.get(resolution.canonical_brand)
    if unsuitable is not None:
        return [
            ExternalCall(
                tool="hira_disease_mapping_unsuitable",
                source="hira_disease",
                status="unsupported",
                summary_text=f"{resolution.canonical_brand}은 {unsuitable['reason_label']}합니다.",
                render_data={"brand": resolution.canonical_brand, **unsuitable},
            )
        ]
    mappings = _hira_disease_mappings(question, resolution.canonical_brand)
    if mappings is None:
        return [
            ExternalCall(
                tool="hira_disease_mapping_unresolved",
                source="hira_disease",
                status="unsupported",
                summary_text=(
                    f"{resolution.canonical_brand}의 대표 질병 KCD 매핑이 아직 확정되지 않아 "
                    "HIRA 질병통계 조회를 실행하지 않았습니다."
                ),
                render_data={
                    "brand": resolution.canonical_brand,
                    "reason": "unconfirmed_brand_to_kcd_mapping",
                },
            )
        ]
    calls: list[ExternalCall] = []
    total = len(mappings)
    for index, mapping in enumerate(mappings, start=1):
        sick_cd = mapping["sick_cd"]
        disease_name = mapping["disease_name"]
        basis = mapping["basis"]
        calls.append(
            ExternalCall(
                tool="hira_disease_mapping",
                source="hira_disease",
                status="mapped",
                summary_text=f"{resolution.canonical_brand} 관련 질병을 HIRA KCD {sick_cd}({disease_name})로 매핑했습니다.",
                render_data={
                    "brand": resolution.canonical_brand,
                    "sickCd": sick_cd,
                    "disease_name": disease_name,
                    "basis": basis,
                    "mapping_index": index,
                    "mapping_total": total,
                },
            )
        )
        for call in (
            external.hira_disease_name_code(sick_cd),
            external.hira_disease_hospitalization_outpatient_stats(sick_cd),
            external.hira_disease_gender_age_stats(sick_cd),
            external.hira_disease_institution_class_stats(sick_cd),
            external.hira_disease_area_stats(sick_cd),
        ):
            calls.append(_with_hira_mapping_context(call, resolution.canonical_brand, mapping, index, total))
    return calls


def _hira_disease_mappings(question: str, canonical_brand: str) -> tuple[HiraMapping, ...] | None:
    mapping = HIRA_DISEASE_MAPPINGS.get(canonical_brand)
    if mapping is not None:
        return _normalize_hira_mappings(mapping)
    for token, mapping in HIRA_DISEASE_TEXT_MAPPINGS.items():
        if token in question:
            return _normalize_hira_mappings(mapping)
    return None


def _normalize_hira_mappings(mapping: HiraMappingEntry) -> tuple[HiraMapping, ...]:
    if isinstance(mapping, dict):
        return (mapping,)
    return tuple(mapping)


def _with_hira_mapping_context(
    call: ExternalCall,
    brand: str,
    mapping: HiraMapping,
    index: int,
    total: int,
) -> ExternalCall:
    return ExternalCall(
        tool=call.tool,
        source=call.source,
        status=call.status,
        summary_text=_hira_call_summary(call.tool, mapping),
        render_data={
            **call.render_data,
            "mapping_brand": brand,
            "mapping_sickCd": mapping["sick_cd"],
            "mapping_disease_name": mapping["disease_name"],
            "mapping_index": index,
            "mapping_total": total,
        },
        safe_url=call.safe_url,
        elapsed_ms=call.elapsed_ms,
    )


def _hira_call_summary(tool: str, mapping: HiraMapping) -> str:
    sick_cd = mapping["sick_cd"]
    disease_name = mapping["disease_name"]
    summaries = {
        "hira_disease_name_code": f"HIRA 질병명칭/코드조회에서 {sick_cd}({disease_name}) 코드를 확인했습니다.",
        "hira_disease_hospitalization_outpatient_stats": f"HIRA 질병입원외래별통계에서 {sick_cd}({disease_name}) 연간 입원/외래 환자수 분포를 확인했습니다.",
        "hira_disease_gender_age_stats": f"HIRA 질병성별연령별통계 API를 KCD {sick_cd} 기준으로 조회했습니다.",
        "hira_disease_institution_class_stats": f"HIRA 질병요양기관종별통계 API를 KCD {sick_cd} 기준으로 조회했습니다.",
        "hira_disease_area_stats": f"HIRA 질병지역별통계 API를 KCD {sick_cd} 기준으로 조회했습니다.",
    }
    return summaries.get(tool, f"HIRA 질병정보서비스 API를 KCD {sick_cd} 기준으로 조회했습니다.")
