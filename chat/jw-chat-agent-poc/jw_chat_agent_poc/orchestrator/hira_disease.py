from __future__ import annotations

import re
from typing import Protocol, TypedDict

from jw_chat_agent_poc.orchestrator.hira_disease_catalog import (
    HiraMapping,
    ResolvedHiraMapping,
    hira_disease_code_for_text,
    hira_disease_code_for_unbranded_query,
    hira_disease_subject_for_unbranded_query,
    mapping_for_unbranded_query as _mapping_for_unbranded_query,
    mappings_for_ingredients as _mappings_for_ingredients,
)
from jw_chat_agent_poc.tools.external import ExternalApiClient, ExternalCall


class HiraResolution(Protocol):
    canonical_brand: str
    molecule_en: tuple[str, ...]
    support_source: str


class HiraUnsuitable(TypedDict):
    reason: str
    reason_label: str
    basis: str


HIRA_TREND_YEARS = tuple(str(year) for year in range(2020, 2025))

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

def is_hira_disease_question(question: str) -> bool:
    normalized = question.strip().rstrip(".?!。？！").strip()
    disease_identity = normalized.endswith(("질환", "질병"))
    return disease_identity or any(
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
    mappings, unmapped_ingredients = _hira_disease_mappings(question, resolution)
    if not mappings:
        molecules = list(resolution.molecule_en)
        return [
            ExternalCall(
                tool="hira_disease_mapping_unresolved",
                source="hira_disease",
                status="mapping_failed",
                summary_text=(
                    f"매핑 없음: {resolution.canonical_brand}의 성분→질병 KCD 매핑이 확정되지 않아 "
                    "HIRA 질병통계 조회를 실행하지 않았습니다."
                ),
                render_data={
                    "brand": resolution.canonical_brand,
                    "reason": "ingredient_to_kcd_mapping_missing",
                    "mapping_source": "ingredient_disease_dictionary",
                    "ingredients": molecules,
                    "unmapped_ingredients": list(unmapped_ingredients or resolution.molecule_en),
                },
            )
        ]
    calls: list[ExternalCall] = []
    total = len(mappings)
    for index, resolved_mapping in enumerate(mappings, start=1):
        mapping = resolved_mapping.mapping
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
                    "mapping_source": resolved_mapping.mapping_source,
                    "matched_ingredients": list(resolved_mapping.matched_ingredients),
                },
            )
        )
        external_calls = _hira_external_calls(question, external, sick_cd)
        for call in external_calls:
            calls.append(
                _with_hira_mapping_context(
                    call,
                    resolution.canonical_brand,
                    resolved_mapping,
                    index,
                    total,
                )
            )
    return calls


def _hira_external_calls(question: str, external: ExternalApiClient, sick_cd: str) -> tuple[ExternalCall, ...]:
    if "추이" in question:
        return (
            external.hira_disease_name_code(sick_cd),
            *(external.hira_disease_hospitalization_outpatient_stats(sick_cd, year) for year in HIRA_TREND_YEARS),
        )
    return (
        external.hira_disease_name_code(sick_cd),
        external.hira_disease_hospitalization_outpatient_stats(sick_cd),
        external.hira_disease_gender_age_stats(sick_cd),
        external.hira_disease_institution_class_stats(sick_cd),
        external.hira_disease_area_stats(sick_cd),
    )


def _hira_disease_mappings(
    question: str,
    resolution: HiraResolution,
) -> tuple[tuple[ResolvedHiraMapping, ...], tuple[str, ...]]:
    if resolution.molecule_en:
        return _mappings_for_ingredients(resolution.molecule_en)
    if resolution.support_source != "hira_disease_dictionary":
        return (), ()
    mapping = _mapping_for_unbranded_query(question)
    return ((mapping,), ()) if mapping is not None else ((), ())


def hira_disease_code_for_resolution(resolution: HiraResolution) -> str | None:
    """Return one KCD code only when every resolved ingredient agrees."""

    mappings, unmapped = _mappings_for_ingredients(resolution.molecule_en)
    codes = {item.mapping["sick_cd"] for item in mappings}
    return next(iter(codes)) if not unmapped and len(codes) == 1 else None


def _with_hira_mapping_context(
    call: ExternalCall,
    brand: str,
    resolved_mapping: ResolvedHiraMapping,
    index: int,
    total: int,
) -> ExternalCall:
    mapping = resolved_mapping.mapping
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
            "mapping_source": resolved_mapping.mapping_source,
            "matched_ingredients": list(resolved_mapping.matched_ingredients),
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
