from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from jw_chat_agent_poc.tools.external.client import ExternalCall


INAPPLICABLE_EXTERNAL_BRANDS: Final[frozenset[str]] = frozenset({"엔커버", "위너프", "위너프A+", "플라주오피"})
SEEDED_FALSE_POSITIVE_BRANDS: Final[frozenset[str]] = frozenset({"뉴트로진"})


@dataclass(frozen=True, slots=True)
class MatchingNotice:
    brand: str
    message: str
    render_data: dict[str, str | tuple[str, ...]]

    def to_call(self) -> ExternalCall:
        return ExternalCall(
            tool="matching_policy_notice",
            source="external_api",
            status="policy",
            summary_text=self.message,
            render_data=self.render_data,
        )


def is_external_inapplicable_brand(brand: str) -> bool:
    return brand in INAPPLICABLE_EXTERNAL_BRANDS


def combo_query(molecules: tuple[str, ...]) -> str:
    return " AND ".join(molecules)


def molecule_query(molecules: tuple[str, ...]) -> str:
    return ", ".join(molecules)


def inapplicable_call(brand: str, molecules: tuple[str, ...]) -> ExternalCall:
    return ExternalCall(
        tool="external_api_inapplicable",
        source="external_api",
        status="inapplicable",
        summary_text=(
            f"브랜드 {brand}는 검증서에서 영양/수액/전해질 제제로 분류되어 외부 임상·특허 조회가 부적합합니다. "
            "제품 무관 성분 결과를 억지로 표시하지 않습니다."
        ),
        render_data={
            "brand": brand,
            "molecule_en": molecules,
            "reason": "nutrition_infusion_electrolyte_false_positive_risk",
        },
    )


def clinical_scope_notice(brand: str, molecules: tuple[str, ...], is_combo: bool) -> MatchingNotice:
    if is_combo:
        return MatchingNotice(
            brand=brand,
            message=(
                f"{brand} 임상은 {combo_query(molecules)} 복합제 조합 임상을 우선 조회하고, "
                f"성분별 참고 결과는 {molecule_query(molecules)} 성분 기준 동향으로 분리합니다. "
                "해외 CT/OpenFDA 결과는 특정 제품에 한정되지 않음에 유의해야 합니다."
            ),
            render_data={"brand": brand, "scope": "combo_and_plus_component_reference", "molecule_en": molecules},
        )
    return MatchingNotice(
        brand=brand,
        message=(
            f"해외 CT/OpenFDA 결과는 {molecule_query(molecules)} 성분 기준 동향이며 특정 제품에 한정되지 않음. "
            f"조회 영문 성분: {molecule_query(molecules)}."
        ),
        render_data={"brand": brand, "scope": "overseas_molecule_trend", "molecule_en": molecules},
    )


def label_patent_scope_notice(brand: str, molecules: tuple[str, ...]) -> MatchingNotice:
    return MatchingNotice(
        brand=brand,
        message=(
            f"국내 식약처/특허는 {brand} 제품 기준으로 우선 해석합니다. "
            f"해외 FDA/OpenFDA/Orange Book은 {molecule_query(molecules)} 성분 기준 자료이며 제품 특정성이 낮거나 누락될 수 있습니다. "
            f"조회 영문 성분: {molecule_query(molecules)}."
        ),
        render_data={"brand": brand, "scope": "domestic_product_overseas_molecule", "molecule_en": molecules},
    )


def annotate_clinical_call(call: ExternalCall, brand: str, molecules: tuple[str, ...], match_scope: str) -> ExternalCall:
    data = {
        **call.render_data,
        "brand": brand,
        "molecule_en": molecules,
        "match_scope": match_scope,
        "intervention_filter": _intervention_filter(call, molecules),
    }
    suffix = "상위 intervention 성분 언급을 확인해 관련성 낮은 결과는 참고/제외 대상으로 봅니다."
    return ExternalCall(
        tool=call.tool,
        source=call.source,
        status=call.status,
        summary_text=f"{call.summary_text} {suffix}",
        render_data=data,
        safe_url=call.safe_url,
        elapsed_ms=call.elapsed_ms,
    )


def needs_seeded_false_positive_filter(brand: str) -> bool:
    return brand in SEEDED_FALSE_POSITIVE_BRANDS


def _intervention_filter(call: ExternalCall, molecules: tuple[str, ...]) -> dict[str, str | int | bool]:
    studies = call.render_data.get("payload", {}).get("studies", [])
    if not isinstance(studies, list):
        return {"checked": False, "matching_top_studies": 0, "checked_top_studies": 0, "action": "no_studies_payload"}
    checked = 0
    matched = 0
    lowered = tuple(molecule.lower() for molecule in molecules)
    for study in studies[:5]:
        if not isinstance(study, dict):
            continue
        arms = study.get("protocolSection", {}).get("armsInterventionsModule", {}).get("interventions", [])
        if not isinstance(arms, list):
            continue
        text = " ".join(str(item.get("name", "")) for item in arms if isinstance(item, dict)).lower()
        checked += 1
        if all(molecule in text for molecule in lowered):
            matched += 1
    action = "keep" if matched else "mark_low_relevance"
    return {"checked": True, "matching_top_studies": matched, "checked_top_studies": checked, "action": action}
