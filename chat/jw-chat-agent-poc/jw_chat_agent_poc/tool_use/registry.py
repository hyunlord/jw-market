from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from functools import partial
from typing import Any

from pydantic import BaseModel

from jw_chat_agent_poc.agent_loop.external_tools import _first_matching_mfds_item
from jw_chat_agent_poc.resolver import BrandResolver, UnsupportedBrandError
from jw_chat_agent_poc.tool_use.catalog import TOOL_DESCRIPTION_CATALOG
from jw_chat_agent_poc.tool_use.contracts import EvidenceFact, ToolEnvelope
from jw_chat_agent_poc.tool_use.specs import (
    BrandInput,
    ClinicalQueryInput,
    DiseaseCodeInput,
    IngredientInput,
    ItemSequenceInput,
    ProcedureCodeInput,
    QueryInput,
    ToolSpec,
)
from jw_chat_agent_poc.tools.external import ExternalApiClient, ExternalCall


_DESCRIPTIONS = {record.name: record.description for record in TOOL_DESCRIPTION_CATALOG}
_FAILED_STATUSES = frozenset({"error", "unsupported", "inapplicable", "no_data"})


class ExternalToolRegistry:
    """Expose the external evidence pack without question-specific routing rules."""

    def __init__(self, *, resolver: BrandResolver, external: ExternalApiClient) -> None:
        self._resolver = resolver
        self._external = external

    def list_for_query(self, _user_text: str) -> tuple[ToolSpec, ...]:
        definitions = (
            ("local_molecule_lookup", BrandInput, self._local_molecule, 1.0, ("local", "molecule")),
            ("get_drug_main_ingredient", BrandInput, self._mfds_main_ingredient, 12.0, ("external", "mfds")),
            ("openfda_label_search", IngredientInput, partial(self._ingredient_call, "openfda_label_search", "라벨/이상반응"), 12.0, ("external", "openfda")),
            ("web_search", QueryInput, self._web_search, 8.0, ("external", "web")),
            ("mfds_permission_search", BrandInput, self._permission_search, 12.0, ("external", "mfds")),
            ("mfds_permission_detail", ItemSequenceInput, self._permission_detail, 12.0, ("external", "mfds")),
            ("mfds_clinical_trial_kr", ClinicalQueryInput, self._clinical_kr, 12.0, ("external", "mfds")),
            ("clinicaltrials_v2_search", ClinicalQueryInput, self._clinical_global, 12.0, ("external", "clinicaltrials")),
            ("mfds_patent", IngredientInput, partial(self._ingredient_call, "mfds_patent", "국내 특허"), 12.0, ("external", "mfds")),
            ("mfds_fda_orangebook", IngredientInput, partial(self._ingredient_call, "mfds_fda_orangebook", "미국 특허/독점권"), 12.0, ("external", "orangebook")),
            ("hira_disease_name_code", DiseaseCodeInput, partial(self._disease_call, "hira_disease_name_code", "질병명/상병코드"), 12.0, ("external", "hira")),
            ("hira_disease_hospitalization_outpatient_stats", DiseaseCodeInput, partial(self._disease_call, "hira_disease_hospitalization_outpatient_stats", "질병 입원/외래 통계"), 12.0, ("external", "hira")),
            ("hira_disease_gender_age_stats", DiseaseCodeInput, partial(self._disease_call, "hira_disease_gender_age_stats", "질병 성별/연령 통계"), 12.0, ("external", "hira")),
            ("hira_disease_institution_class_stats", DiseaseCodeInput, partial(self._disease_call, "hira_disease_institution_class_stats", "질병 기관종별 통계"), 12.0, ("external", "hira")),
            ("hira_disease_area_stats", DiseaseCodeInput, partial(self._disease_call, "hira_disease_area_stats", "질병 지역 통계"), 12.0, ("external", "hira")),
            ("hira_procedure_gender_ipat_opat_stats", ProcedureCodeInput, partial(self._procedure_call, "hira_procedure_gender_ipat_opat_stats", "진료행위 입원/외래 통계"), 12.0, ("external", "hira")),
            ("hira_procedure_gender_age_stats", ProcedureCodeInput, partial(self._procedure_call, "hira_procedure_gender_age_stats", "진료행위 성별/연령 통계"), 12.0, ("external", "hira")),
            ("hira_procedure_institution_class_stats", ProcedureCodeInput, partial(self._procedure_call, "hira_procedure_institution_class_stats", "진료행위 기관종별 통계"), 12.0, ("external", "hira")),
            ("hira_procedure_area_stats", ProcedureCodeInput, partial(self._procedure_call, "hira_procedure_area_stats", "진료행위 지역 통계"), 12.0, ("external", "hira")),
        )
        return tuple(
            ToolSpec(name, _DESCRIPTIONS[name], input_model, execute, timeout_s, tags)
            for name, input_model, execute, timeout_s, tags in definitions
        )

    def _local_molecule(self, payload: BaseModel) -> ToolEnvelope:
        request = BrandInput.model_validate(payload.model_dump())
        try:
            resolution = self._resolver.resolve(request.brand, allow_default=False)
        except UnsupportedBrandError:
            return _error("UNSUPPORTED_QUERY", f"지원 브랜드가 아닙니다: {request.brand}")
        facts = tuple(
            EvidenceFact(
                fact_id=f"local_molecule:{resolution.canonical_brand}:{index}",
                subject=resolution.canonical_brand,
                metric="성분",
                value=None,
                unit=None,
                period=None,
                source_name="로컬 시장 DB 성분 정보",
                source_locator=molecule,
                raw_ref=None,
            )
            for index, molecule in enumerate(resolution.molecule_en, start=1)
        )
        if not facts:
            return _error("NO_EVIDENCE", f"{resolution.canonical_brand} 성분 근거가 없습니다.")
        return _success(f"{resolution.canonical_brand} 성분 {len(facts)}건", facts)

    def _mfds_main_ingredient(self, payload: BaseModel) -> ToolEnvelope:
        request = BrandInput.model_validate(payload.model_dump())
        canonical = self._canonical_brand(request.brand)
        call = self._external.mfds_permission_search(canonical)
        item = _first_matching_mfds_item(call, canonical)
        ingredient = None if item is None else item.get("ITEM_INGR_NAME") or item.get("MAIN_INGR_ENG")
        if not ingredient:
            return ToolEnvelope(
                ok=False,
                preview=call.summary_text,
                evidence=(),
                raw=asdict(call),
                error_code="NO_EVIDENCE",
                error_message="식약처 응답에 검증 가능한 주성분 필드가 없습니다.",
            )
        fact = EvidenceFact(
            fact_id=f"{call.tool}:ingredient:1",
            subject=canonical,
            metric="성분",
            value=None,
            unit=None,
            period=None,
            source_name="식약처 의약품 성분 정보",
            source_locator=str(ingredient),
            raw_ref=f"{call.tool}:1",
        )
        return ToolEnvelope(ok=True, preview=f"{canonical} 주성분 확인", evidence=(fact,), raw=asdict(call), error_code=None, error_message=None)

    def _permission_search(self, payload: BaseModel) -> ToolEnvelope:
        request = BrandInput.model_validate(payload.model_dump())
        canonical = self._canonical_brand(request.brand)
        call = self._external.mfds_permission_search(canonical)
        item = _first_matching_mfds_item(call, canonical)
        if item is None:
            return ToolEnvelope(
                ok=False,
                preview=call.summary_text,
                evidence=(),
                raw=asdict(call),
                error_code="NO_EVIDENCE",
                error_message="식약처 검색에서 canonical 제품군 근거를 찾지 못했습니다.",
            )
        fact = _row_fact(call, canonical, "허가 품목", item, 1)
        return ToolEnvelope(ok=True, preview=call.summary_text, evidence=(fact,), raw=asdict(call), error_code=None, error_message=None)

    def _canonical_brand(self, brand: str) -> str:
        try:
            return self._resolver.resolve(brand, allow_default=False).canonical_brand
        except UnsupportedBrandError:
            return brand.strip()

    def _brand_call(self, method: str, metric: str, payload: BaseModel) -> ToolEnvelope:
        request = BrandInput.model_validate(payload.model_dump())
        call = getattr(self._external, method)(request.brand)
        return _external_call_envelope(call, request.brand, metric)

    def _ingredient_call(self, method: str, metric: str, payload: BaseModel) -> ToolEnvelope:
        request = IngredientInput.model_validate(payload.model_dump())
        call = getattr(self._external, method)(request.ingredient)
        return _external_call_envelope(call, request.ingredient, metric)

    def _permission_detail(self, payload: BaseModel) -> ToolEnvelope:
        request = ItemSequenceInput.model_validate(payload.model_dump())
        call = self._external.mfds_permission_detail(request.item_seq)
        return _external_call_envelope(call, request.item_seq, "허가 상세")

    def _clinical_kr(self, payload: BaseModel) -> ToolEnvelope:
        request = ClinicalQueryInput.model_validate(payload.model_dump())
        call = self._external.mfds_clinical_trial_kr(request.query)
        return _external_call_envelope(call, request.query, "국내 임상시험")

    def _clinical_global(self, payload: BaseModel) -> ToolEnvelope:
        request = ClinicalQueryInput.model_validate(payload.model_dump())
        call = self._external.clinicaltrials_v2_search(request.query)
        return _external_call_envelope(call, request.query, "글로벌 임상시험")

    def _web_search(self, payload: BaseModel) -> ToolEnvelope:
        request = QueryInput.model_validate(payload.model_dump())
        call = self._external.web_search(request.query)
        return _external_call_envelope(call, request.brand or request.query, "웹 검색")

    def _disease_call(self, method: str, metric: str, payload: BaseModel) -> ToolEnvelope:
        request = DiseaseCodeInput.model_validate(payload.model_dump())
        function = getattr(self._external, method)
        call = function(request.sick_cd) if method == "hira_disease_name_code" else function(request.sick_cd, year=request.year)
        return _external_call_envelope(call, request.sick_cd, metric)

    def _procedure_call(self, method: str, metric: str, payload: BaseModel) -> ToolEnvelope:
        request = ProcedureCodeInput.model_validate(payload.model_dump())
        call = getattr(self._external, method)(request.st5_cd, year=request.year, std_type=request.std_type)
        return _external_call_envelope(call, request.st5_cd, metric)


def _external_call_envelope(call: ExternalCall, subject: str, metric: str) -> ToolEnvelope:
    evidence = _facts_from_external_call(call, subject, metric)
    ok = call.status not in _FAILED_STATUSES and bool(evidence)
    if not ok:
        return ToolEnvelope(
            ok=False,
            preview=call.summary_text,
            evidence=(),
            raw=asdict(call),
            error_code="NO_EVIDENCE" if call.status not in _FAILED_STATUSES else call.status.upper(),
            error_message="도구 응답에서 검증 가능한 근거를 찾지 못했습니다.",
        )
    return ToolEnvelope(ok=True, preview=call.summary_text, evidence=evidence, raw=asdict(call), error_code=None, error_message=None)


def _facts_from_external_call(call: ExternalCall, subject: str, metric: str) -> tuple[EvidenceFact, ...]:
    data = call.render_data
    rows = _external_rows(data)
    if rows:
        return tuple(_row_fact(call, subject, metric, item, index) for index, item in enumerate(rows[:5], start=1))
    sections = data.get("label_sections")
    if isinstance(sections, list):
        return tuple(
            _text_fact(call, subject, "라벨 섹션", str(section), index)
            for index, section in enumerate(sections[:5], start=1)
            if section
        )
    nct_ids = data.get("nct_ids")
    if isinstance(nct_ids, list):
        title = str(data.get("briefTitle") or "").strip()
        return tuple(
            _text_fact(call, subject, metric, " · ".join(part for part in (str(nct_id), title) if part), index)
            for index, nct_id in enumerate(nct_ids[:5], start=1)
            if nct_id
        )
    return ()


def _external_rows(data: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    candidates: Any = data.get("items")
    payload = data.get("payload")
    if not isinstance(candidates, list) and isinstance(payload, dict):
        candidates = payload.get("results") or payload.get("studies")
    if not isinstance(candidates, list):
        return ()
    return tuple(item for item in candidates if isinstance(item, dict))


def _row_fact(call: ExternalCall, subject: str, metric: str, item: dict[str, Any], index: int) -> EvidenceFact:
    value = next((_decimal_or_none(item.get(key)) for key in ("value", "rank", "ptntCnt") if item.get(key) not in (None, "")), None)
    period = next((str(item[key]) for key in ("date", "period", "year") if item.get(key)), None)
    return EvidenceFact(
        fact_id=f"{call.tool}:{index}",
        subject=subject,
        metric=metric,
        value=value,
        unit=None,
        period=period,
        source_name=_source_name(call),
        source_locator=_item_locator(item),
        raw_ref=f"{call.tool}:{index}",
    )


def _text_fact(call: ExternalCall, subject: str, metric: str, text: str, index: int) -> EvidenceFact:
    return EvidenceFact(
        fact_id=f"{call.tool}:{index}",
        subject=subject,
        metric=metric,
        value=None,
        unit=None,
        period=None,
        source_name=_source_name(call),
        source_locator=text,
        raw_ref=f"{call.tool}:{index}",
    )


def _item_locator(item: dict[str, Any]) -> str | None:
    keys = (
        "title", "ITEM_NAME", "itemName", "briefTitle", "sickNm", "st5Nm",
        "GOODS_NAME", "INGR_ENG_NAME", "DOMESTIC_PATENT_NO", "PRT_NAME",
        "inpatOpat", "sex", "areaNm", "orgType",
    )
    values = tuple(dict.fromkeys(str(item[key]).strip() for key in keys if item.get(key)))
    return " · ".join(values[:3]) or None


def _source_name(call: ExternalCall) -> str:
    if call.tool.startswith("openfda"):
        return "FDA 의약품 라벨 정보"
    if call.tool.startswith("clinicaltrials"):
        return "ClinicalTrials.gov 임상시험 정보"
    if call.tool.startswith("hira"):
        return "건강보험심사평가원 통계"
    if call.tool.startswith("mfds_fda"):
        return "FDA Orange Book 정보"
    if call.tool.startswith("mfds"):
        return "식약처 의약품 정보"
    if call.tool == "web_search":
        return "웹 검색 결과"
    return call.source


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except (ArithmeticError, TypeError, ValueError):
        return None


def _success(preview: str, evidence: tuple[EvidenceFact, ...]) -> ToolEnvelope:
    return ToolEnvelope(ok=True, preview=preview, evidence=evidence, raw=None, error_code=None, error_message=None)


def _error(code: str, message: str) -> ToolEnvelope:
    return ToolEnvelope(ok=False, preview=message, evidence=(), raw=None, error_code=code, error_message=message)
