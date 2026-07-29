from __future__ import annotations

import logging
import re
from dataclasses import asdict
from decimal import Decimal
from functools import partial
from typing import Any

from pydantic import BaseModel

from jw_chat_agent_poc.agent_loop.external_tools import (
    _first_matching_mfds_item,
    _matching_mfds_items,
)
from jw_chat_agent_poc.agent_loop.planner import needs_external_context
from jw_chat_agent_poc.orchestrator.hira_disease import (
    HiraDiseaseCodeAbsent,
    HiraDiseaseCodeAmbiguous,
    HiraDiseaseCodeResolved,
    resolve_hira_disease_code,
)
from jw_chat_agent_poc.orchestrator.narrative_intent import wants_market_narrative
from jw_chat_agent_poc.resolver import BrandResolver, UnsupportedBrandError
from jw_chat_agent_poc.tool_use.catalog import TOOL_DESCRIPTION_CATALOG
from jw_chat_agent_poc.tool_use.contracts import EvidenceFact, ToolEnvelope
from jw_chat_agent_poc.tool_use.reimbursement_evidence import reimbursement_envelope
from jw_chat_agent_poc.tool_use.specs import (
    BrandInput,
    ClinicalQueryInput,
    DiseaseCodeInput,
    IngredientInput,
    ItemSequenceInput,
    NctIdInput,
    OpenFdaInput,
    ProcedureCodeInput,
    QueryInput,
    ToolSpec,
)
from jw_chat_agent_poc.tools.external import (
    ExternalApiClient,
    ExternalCall,
    is_hira_disease_code,
)
from jw_chat_agent_poc.tools.external.hira_reimbursement import (
    HiraReimbursementHttpClient,
    ReimbursementLookupService,
    configured_reimbursement_store,
)
from jw_chat_agent_poc.tools.external.mcp_client import MCP_FIRST_ATTEMPT_TIMEOUT_S

_DESCRIPTIONS = {record.name: record.description for record in TOOL_DESCRIPTION_CATALOG}
_FAILED_STATUSES = frozenset({"error", "unsupported", "inapplicable", "no_data"})
LOGGER = logging.getLogger(__name__)
_INTERNAL_METRIC_WEB_EXCLUSION_TOKENS = (
    "매출",
    "판매",
    "실적",
    "팔렸",
    "장사",
    "점유",
    "순위",
    "몇 등",
    "몇등",
    "시장 규모",
    "시장규모",
    "hhi",
)
_CLINICAL_DETAIL_DESIGN_FIELDS = frozenset(
    {
        "allocation",
        "enrollment",
        "intervention_model",
        "masking",
        "outcomes",
        "start_date",
        "primary_completion_date",
    }
)
_CLINICAL_DETAIL_EXPLICIT_ONLY_FIELDS = frozenset(
    {"allocation", "intervention_model", "masking"}
)


def _clinical_detail_value_present(value: Any) -> bool:
    return value is not None and value != "" and value != () and value != []


def _clinical_detail_requested_fields(user_text: str) -> frozenset[str] | None:
    lowered = user_text.casefold()
    requested: set[str] = set()
    if any(token in lowered for token in ("inclusion", "exclusion", "선정기준", "제외기준")):
        requested.add("eligibility")
    if any(
        token in lowered
        for token in ("임상 디자인", "시험 디자인", "대상", "평가변수", "기간")
    ):
        requested.update(_CLINICAL_DETAIL_DESIGN_FIELDS)
    elif any(token in lowered for token in ("outcome", "결과지표", "평가 변수")):
        requested.add("outcomes")
    return frozenset(requested) or None


def _is_internal_metric_only_question(user_text: str) -> bool:
    normalized = user_text.casefold()
    if normalized.lstrip().startswith("/deep"):
        return False
    return any(
        token.casefold() in normalized for token in _INTERNAL_METRIC_WEB_EXCLUSION_TOKENS
    ) and wants_market_narrative(user_text) and not needs_external_context(user_text)


class ExternalToolRegistry:
    """Expose the external evidence pack without question-specific routing rules."""

    def __init__(
        self,
        *,
        resolver: BrandResolver,
        external: ExternalApiClient,
        reimbursement: ReimbursementLookupService | None = None,
    ) -> None:
        self._resolver = resolver
        self._external = external
        self._reimbursement = reimbursement or ReimbursementLookupService(
            store=configured_reimbursement_store(),
            realtime=HiraReimbursementHttpClient(),
        )

    def list_for_query(self, user_text: str) -> tuple[ToolSpec, ...]:
        mcp_timeout_s = MCP_FIRST_ATTEMPT_TIMEOUT_S + float(self._external.timeout_s) + 1.0
        clinical_detail = partial(
            self._clinical_detail,
            requested_fields=_clinical_detail_requested_fields(user_text),
        )
        definitions = (
            ("local_molecule_lookup", BrandInput, self._local_molecule, 1.0, ("local", "molecule")),
            ("get_drug_main_ingredient", BrandInput, self._mfds_main_ingredient, mcp_timeout_s, ("external", "mfds")),
            ("openfda_label_search", OpenFdaInput, self._openfda_call, mcp_timeout_s, ("external", "openfda")),
            ("web_search", QueryInput, self._web_search, 8.0, ("external", "web")),
            ("mfds_permission_search", BrandInput, self._permission_search, mcp_timeout_s, ("external", "mfds")),
            ("mfds_permission_detail", ItemSequenceInput, self._permission_detail, mcp_timeout_s, ("external", "mfds")),
            ("mfds_composition", BrandInput, self._mfds_composition, mcp_timeout_s, ("external", "mfds")),
            ("mfds_easy_drug", BrandInput, self._mfds_easy_drug, mcp_timeout_s, ("external", "mfds")),
            ("mfds_clinical_trial_kr", ClinicalQueryInput, self._clinical_kr, mcp_timeout_s, ("external", "mfds")),
            ("clinicaltrials_v2_search", ClinicalQueryInput, self._clinical_global, mcp_timeout_s, ("external", "clinicaltrials")),
            ("clinicaltrials_study_details", NctIdInput, clinical_detail, mcp_timeout_s, ("external", "clinicaltrials")),
            ("mfds_patent", IngredientInput, partial(self._ingredient_call, "mfds_patent", "국내 특허"), mcp_timeout_s, ("external", "mfds")),
            ("mfds_fda_orangebook", IngredientInput, partial(self._ingredient_call, "mfds_fda_orangebook", "미국 특허/독점권"), mcp_timeout_s, ("external", "orangebook")),
            ("hira_disease_name_code", DiseaseCodeInput, partial(self._disease_call, "hira_disease_name_code", "질병명/상병코드"), mcp_timeout_s, ("external", "hira", "grounding")),
            ("hira_disease_hospitalization_outpatient_stats", DiseaseCodeInput, partial(self._disease_call, "hira_disease_hospitalization_outpatient_stats", "질병 입원/외래 통계"), mcp_timeout_s, ("external", "hira")),
            ("hira_disease_gender_age_stats", DiseaseCodeInput, partial(self._disease_call, "hira_disease_gender_age_stats", "질병 성별/연령 통계"), mcp_timeout_s, ("external", "hira")),
            ("hira_disease_institution_class_stats", DiseaseCodeInput, partial(self._disease_call, "hira_disease_institution_class_stats", "질병 기관종별 통계"), mcp_timeout_s, ("external", "hira")),
            ("hira_disease_area_stats", DiseaseCodeInput, partial(self._disease_call, "hira_disease_area_stats", "질병 지역 통계"), mcp_timeout_s, ("external", "hira")),
            ("hira_reimbursement_criteria", BrandInput, self._hira_reimbursement, 8.0, ("external", "hira")),
            ("hira_procedure_gender_ipat_opat_stats", ProcedureCodeInput, partial(self._procedure_call, "hira_procedure_gender_ipat_opat_stats", "진료행위 입원/외래 통계"), mcp_timeout_s, ("external", "hira")),
            ("hira_procedure_gender_age_stats", ProcedureCodeInput, partial(self._procedure_call, "hira_procedure_gender_age_stats", "진료행위 성별/연령 통계"), mcp_timeout_s, ("external", "hira")),
            ("hira_procedure_institution_class_stats", ProcedureCodeInput, partial(self._procedure_call, "hira_procedure_institution_class_stats", "진료행위 기관종별 통계"), mcp_timeout_s, ("external", "hira")),
            ("hira_procedure_area_stats", ProcedureCodeInput, partial(self._procedure_call, "hira_procedure_area_stats", "진료행위 지역 통계"), mcp_timeout_s, ("external", "hira")),
        )
        exclude_web_search = _is_internal_metric_only_question(user_text)
        if exclude_web_search:
            LOGGER.info("external tool candidate excluded tool=web_search reason=internal_metric_only")
        return tuple(
            ToolSpec(name, _DESCRIPTIONS[name], input_model, execute, timeout_s, tags)
            for name, input_model, execute, timeout_s, tags in definitions
            if not (exclude_web_search and name == "web_search")
        )

    def _hira_reimbursement(self, payload: BaseModel) -> ToolEnvelope:
        request = BrandInput.model_validate(payload.model_dump())
        canonical = self._canonical_brand(request.brand)
        result = self._reimbursement.lookup(canonical)
        return reimbursement_envelope(
            result,
            subject=canonical,
            resolver=self._resolver,
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
        call = self._external.mfds_main_ingredient(canonical)
        item = _first_matching_mfds_item(call, canonical)
        ingredient = None if item is None else item.get("ITEM_INGR_NAME") or item.get("MAIN_INGR_ENG") or item.get("MTRAL_NM")
        if ingredient is None:
            payload = call.render_data.get("items", [])
            if isinstance(payload, list) and payload and isinstance(payload[0], dict):
                ingredient = payload[0].get("MTRAL_NM") or payload[0].get("MAIN_INGR_ENG") or payload[0].get("ITEM_INGR_NAME")
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
        if truncated := _truncated_result_envelope(call):
            return truncated
        items = _matching_mfds_items(call, canonical)
        if not items:
            return ToolEnvelope(
                ok=False,
                preview=call.summary_text,
                evidence=(),
                raw=asdict(call),
                error_code="NO_EVIDENCE",
                error_message="식약처 검색에서 canonical 제품군 근거를 찾지 못했습니다.",
            )
        detail_call = _permission_detail_call(self._external, items[0])
        facts = tuple(
            _row_fact(call, canonical, "허가 품목", item, index)
            for index, item in enumerate(items, start=1)
        )
        detail_facts = (
            _permission_detail_facts(detail_call, canonical)
            if detail_call is not None
            else ()
        )
        return ToolEnvelope(
            ok=True,
            preview=call.summary_text,
            evidence=(*facts, *detail_facts),
            raw=_permission_search_raw(call, detail_call),
            error_code=None,
            error_message=None,
        )

    def _mfds_composition(self, payload: BaseModel) -> ToolEnvelope:
        request = BrandInput.model_validate(payload.model_dump())
        canonical = self._canonical_brand(request.brand)
        call = self._external.mfds_composition(canonical)
        items = _matching_mfds_items(call, canonical)
        facts = tuple(
            EvidenceFact(
                fact_id=f"{call.tool}:{canonical}:{index}",
                subject=canonical,
                metric="성분 조성",
                value=None,
                unit=None,
                period=None,
                source_name="식약처 의약품 성분 정보",
                source_locator=_composition_locator(item),
                raw_ref=f"{call.tool}:{index}",
            )
            for index, item in enumerate(items, start=1)
            if _composition_locator(item)
        )
        if not facts:
            return _error("NO_EVIDENCE", "식약처 응답에 제품명과 일치하는 성분 조성 근거가 없습니다.")
        return ToolEnvelope(
            ok=True,
            preview=f"{canonical} 성분 조성 {len(facts)}건",
            evidence=facts,
            raw=asdict(call),
            error_code=None,
            error_message=None,
        )

    def _mfds_easy_drug(self, payload: BaseModel) -> ToolEnvelope:
        request = BrandInput.model_validate(payload.model_dump())
        canonical = self._canonical_brand(request.brand)
        call = self._external.mfds_easy_drug(canonical)
        items = _matching_mfds_items(call, canonical)
        facts = tuple(
            fact
            for index, item in enumerate(items, start=1)
            for fact in _easy_drug_facts(call, canonical, item, index)
        )
        if not facts:
            return _error("NO_EVIDENCE", "식약처 e약은요 응답에 제품명과 일치하는 공개 필드가 없습니다.")
        return ToolEnvelope(
            ok=True,
            preview=f"{canonical} e약은요 공개 필드 {len(facts)}건",
            evidence=facts,
            raw=asdict(call),
            error_code=None,
            error_message=None,
        )

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

    def _openfda_call(self, payload: BaseModel) -> ToolEnvelope:
        request = OpenFdaInput.model_validate(payload.model_dump())
        call = self._external.openfda_label_search(
            request.ingredient,
            evidence_type=request.evidence_type,
        )
        metric = "FAERS 자발보고 내 이상반응" if request.evidence_type == "adverse_event" else "FDA 라벨"
        return _external_call_envelope(call, request.ingredient, metric)

    def _permission_detail(self, payload: BaseModel) -> ToolEnvelope:
        request = ItemSequenceInput.model_validate(payload.model_dump())
        call = self._external.mfds_permission_detail(request.item_seq)
        return _external_call_envelope(call, request.item_seq, "허가 상세")

    def _clinical_kr(self, payload: BaseModel) -> ToolEnvelope:
        request = ClinicalQueryInput.model_validate(payload.model_dump())
        call = self._external.mfds_clinical_trial_kr(request.query, query_type=request.query_type)
        return _external_call_envelope(call, request.query, "국내 임상시험")

    def _clinical_global(self, payload: BaseModel) -> ToolEnvelope:
        request = ClinicalQueryInput.model_validate(payload.model_dump())
        call = self._external.clinicaltrials_v2_search(request.query, query_type=request.query_type)
        envelope = _external_call_envelope(call, request.query, "글로벌 임상시험")
        if request.query_type == "condition" and not envelope.ok:
            return _clinical_condition_absence_envelope(call, envelope)
        return envelope

    def _clinical_detail(
        self,
        payload: BaseModel,
        *,
        requested_fields: frozenset[str] | None,
    ) -> ToolEnvelope:
        request = NctIdInput.model_validate(payload.model_dump())
        call = self._external.clinicaltrials_study_details(request.nct_id)
        detail = call.render_data.get("detail")
        if call.status in _FAILED_STATUSES or not isinstance(detail, dict):
            return _external_call_envelope(call, request.nct_id, "임상시험 상세")
        url = str(call.safe_url or "")
        missing_reasons = {
            "enrollment": "ClinicalTrials 상세 응답에서 등록 인원을 확인할 수 없습니다.",
            "start_date": "ClinicalTrials 상세 응답에서 시험 시작일을 확인할 수 없습니다.",
            "primary_completion_date": (
                "ClinicalTrials 상세 응답에서 일차 완료일을 확인할 수 없습니다."
            ),
            "outcomes": "ClinicalTrials 상세 응답에서 결과지표를 확인할 수 없습니다.",
            "allocation": "ClinicalTrials 상세 응답에서 배정 방식을 확인할 수 없습니다.",
            "masking": "ClinicalTrials 상세 응답에서 눈가림 방식을 확인할 수 없습니다.",
            "intervention_model": (
                "ClinicalTrials 상세 응답에서 중재 모형을 확인할 수 없습니다."
            ),
            "eligibility": "ClinicalTrials 상세 응답에서 선정·제외 기준을 확인할 수 없습니다.",
        }
        legacy_missing_fields = frozenset(
            {"start_date", "primary_completion_date", "outcomes"}
        )
        facts = tuple(
            EvidenceFact(
                fact_id=f"{call.tool}:{request.nct_id}:{index}",
                subject=request.nct_id,
                metric=label,
                value=None,
                unit=None,
                period=None,
                source_name="ClinicalTrials.gov 임상시험 상세",
                source_locator=(
                    f"{value} · {url}"
                    if _clinical_detail_value_present(value) and url
                    else str(value)
                    if _clinical_detail_value_present(value)
                    else missing_reasons[key]
                ),
                raw_ref=f"{call.tool}:{key}",
            )
            for index, (key, label, value) in enumerate(
                (
                    ("title", "연구 제목", detail.get("title")),
                    ("status", "연구 상태", detail.get("status")),
                    ("phase", "임상 단계", detail.get("phase")),
                    ("enrollment", "등록 인원", detail.get("enrollment")),
                    ("interventions", "중재", detail.get("interventions")),
                    ("outcomes", "결과지표", detail.get("outcomes")),
                    ("start_date", "시험 시작일", detail.get("start_date")),
                    (
                        "primary_completion_date",
                        "일차 완료일",
                        detail.get("primary_completion_date"),
                    ),
                    (
                        "eligibility",
                        "선정·제외 기준",
                        (
                            f"{detail.get('eligibility')} "
                            "(선정·제외기준은 현재 연결에서 앞부분 200자까지만 제공됩니다.)"
                            if detail.get("eligibility")
                            else None
                        ),
                    ),
                    ("allocation", "배정 방식", detail.get("allocation")),
                    ("masking", "눈가림", detail.get("masking")),
                    (
                        "intervention_model",
                        "중재 모형",
                        detail.get("intervention_model"),
                    ),
                ),
                start=1,
            )
            if (requested_fields is None or key in requested_fields)
            and (
                key not in _CLINICAL_DETAIL_EXPLICIT_ONLY_FIELDS
                or requested_fields is not None
            )
            and (
                _clinical_detail_value_present(value)
                or key
                in (
                    missing_reasons
                    if requested_fields is not None
                    else legacy_missing_fields
                )
            )
        )
        if not facts:
            return _error("NO_EVIDENCE", "ClinicalTrials 상세 응답에 검증 가능한 필드가 없습니다.")
        missing_requested_facets = tuple(
            key
            for key in missing_reasons
            if requested_fields is not None
            and key in requested_fields
            and not _clinical_detail_value_present(detail.get(key))
        )
        return ToolEnvelope(
            ok=True,
            preview=call.summary_text,
            evidence=facts,
            missing_requested_facets=missing_requested_facets,
            raw=asdict(call),
            error_code="PARTIAL_RESULT" if missing_requested_facets else None,
            error_message=(
                "요청한 임상시험 상세 항목 일부를 확인할 수 없습니다."
                if missing_requested_facets
                else None
            ),
        )

    def _web_search(self, payload: BaseModel) -> ToolEnvelope:
        request = QueryInput.model_validate(payload.model_dump())
        call = self._external.web_search(request.query, topic=request.topic)
        return _external_call_envelope(call, request.brand or request.query, "웹 검색")

    def _disease_call(self, method: str, metric: str, payload: BaseModel) -> ToolEnvelope:
        request = DiseaseCodeInput.model_validate(payload.model_dump())
        requested = request.sick_cd.strip()
        if method == "hira_disease_name_code":
            call = self._external.hira_disease_name_code(requested)
            return _external_call_envelope(call, requested, metric)
        if is_hira_disease_code(requested):
            sick_cd = requested.upper()
        else:
            resolution = resolve_hira_disease_code(requested, self._external)
            match resolution:
                case HiraDiseaseCodeResolved(candidate=candidate):
                    sick_cd = candidate.sick_cd
                case HiraDiseaseCodeAmbiguous(candidates=candidates):
                    return _error(
                        "AMBIGUOUS_DISEASE_CODE",
                        "HIRA search_disease_code 후보가 여러 건입니다: "
                        + ", ".join(f"{candidate.sick_cd} {candidate.disease_name}" for candidate in candidates),
                    )
                case HiraDiseaseCodeAbsent(query=query):
                    return _error("DISEASE_CODE_NOT_FOUND", f"HIRA search_disease_code에서 상병코드를 확인하지 못했습니다: {query}")
        function = getattr(self._external, method)
        call = function(sick_cd, year=request.year)
        return _external_call_envelope(call, sick_cd, metric)

    def _procedure_call(self, method: str, metric: str, payload: BaseModel) -> ToolEnvelope:
        request = ProcedureCodeInput.model_validate(payload.model_dump())
        call = getattr(self._external, method)(request.st5_cd, year=request.year, std_type=request.std_type)
        return _external_call_envelope(call, request.st5_cd, metric)


def _external_call_envelope(call: ExternalCall, subject: str, metric: str) -> ToolEnvelope:
    if truncated := _truncated_result_envelope(call):
        return truncated
    evidence = _facts_from_external_call(call, subject, metric)
    ok = call.status not in _FAILED_STATUSES and bool(evidence)
    if not ok:
        error_message = (
            "외부 도구 조회에 실패했습니다."
            if call.status == "error"
            else "도구 응답에서 검증 가능한 근거를 찾지 못했습니다."
        )
        return ToolEnvelope(
            ok=False,
            preview=call.summary_text,
            evidence=(),
            raw=asdict(call),
            error_code="NO_EVIDENCE" if call.status not in _FAILED_STATUSES else call.status.upper(),
            error_message=error_message,
        )
    return ToolEnvelope(ok=True, preview=call.summary_text, evidence=evidence, raw=asdict(call), error_code=None, error_message=None)


def _permission_detail_call(
    external: ExternalApiClient,
    item: dict[str, Any],
) -> ExternalCall | None:
    item_seq = str(item.get("ITEM_SEQ") or item.get("itemSeq") or "").strip()
    if not item_seq:
        return None
    return external.mfds_permission_detail(item_seq)


def _permission_search_raw(
    search_call: ExternalCall,
    detail_call: ExternalCall | None,
) -> dict[str, Any]:
    calls = [asdict(search_call)]
    if detail_call is not None:
        calls.append(asdict(detail_call))
    raw = asdict(search_call)
    render_data = dict(raw["render_data"])
    render_data["calls"] = calls
    raw["render_data"] = render_data
    return raw


def _permission_detail_facts(
    call: ExternalCall,
    subject: str,
) -> tuple[EvidenceFact, ...]:
    items = _matching_mfds_items(call, subject)
    return tuple(
        fact
        for row_index, item in enumerate(items[:1], start=1)
        for fact in _permission_detail_row_facts(call, subject, item, row_index)
    )


def _permission_detail_row_facts(
    call: ExternalCall,
    subject: str,
    item: dict[str, Any],
    row_index: int,
) -> tuple[EvidenceFact, ...]:
    fields = (
        ("NB_DOC_DATA", "급여 기준"),
        ("REIMBURSEMENT_CRITERIA", "급여 기준"),
        ("EE_DOC_DATA", "효능·효과"),
        ("EFFICACY_EFFECT", "효능·효과"),
        ("UD_DOC_DATA", "용법·용량"),
        ("DOSAGE_USAGE", "용법·용량"),
        ("WARNING_DOC_DATA", "사용상 주의사항"),
        ("WARNING", "사용상 주의사항"),
        ("MAIN_ITEM_INGR", "적응증"),
        ("INDICATION", "적응증"),
    )
    product = str(item.get("ITEM_NAME") or item.get("itemName") or "").strip()
    return tuple(
        EvidenceFact(
            fact_id=f"{call.tool}:{subject}:{row_index}:{key}",
            subject=subject,
            metric=label,
            value=None,
            unit=None,
            period=None,
            source_name="식약처 의약품 허가 상세",
            source_locator=f"{product} · {value}" if product else value,
            raw_ref=f"{call.tool}:{row_index}:{key}",
        )
        for key, label in fields
        if (value := str(item.get(key) or "").strip())
    )


def _clinical_condition_absence_envelope(call: ExternalCall, envelope: ToolEnvelope) -> ToolEnvelope:
    if call.status == "error":
        message = _clinical_list_scope_note("ClinicalTrials.gov 조회에 실패했습니다.")
    else:
        message = _clinical_list_scope_note("ClinicalTrials.gov 질환 조건 검색 결과가 0건입니다.")
    return envelope.model_copy(update={"error_message": message})


def _truncated_result_envelope(call: ExternalCall) -> ToolEnvelope | None:
    if call.render_data.get("truncated") is not True:
        return None
    return ToolEnvelope(
        ok=False,
        preview=call.summary_text,
        evidence=(),
        raw=asdict(call),
        error_code="TRUNCATED_RESULT",
        error_message="외부 도구 결과가 절단되어 완전한 근거로 사용할 수 없습니다.",
    )


def _facts_from_external_call(call: ExternalCall, subject: str, metric: str) -> tuple[EvidenceFact, ...]:
    data = call.render_data
    rows = _external_rows(data)
    if call.tool == "clinicaltrials_v2_search":
        return _clinical_trial_facts(call, subject, metric, rows)
    if _is_adverse_event_call(data):
        rows = tuple(row for row in rows if _adverse_report_matches_subject(row, subject))
    if rows:
        fallback_period = _request_period(data)
        return tuple(
            _row_fact(call, subject, metric, item, index, fallback_period=fallback_period)
            for index, item in enumerate(rows[:5], start=1)
        )
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


def _clinical_trial_facts(
    call: ExternalCall,
    subject: str,
    metric: str,
    rows: tuple[dict[str, Any], ...],
) -> tuple[EvidenceFact, ...]:
    nct_ids = call.render_data.get("nct_ids")
    retrieved_count = len(rows) if rows else len(nct_ids) if isinstance(nct_ids, list) else 0
    if retrieved_count == 0:
        return ()
    displayed_count = min(retrieved_count, 5)
    facts: list[EvidenceFact] = []
    if rows:
        facts.extend(
            _row_fact(call, subject, metric, item, index)
            for index, item in enumerate(rows[:5], start=1)
        )
    elif isinstance(nct_ids, list):
        title = str(call.render_data.get("briefTitle") or "").strip()
        facts.extend(
            _text_fact(
                call,
                subject,
                metric,
                " · ".join(part for part in (str(nct_id), title) if part),
                index,
            )
            for index, nct_id in enumerate(nct_ids[:5], start=1)
            if nct_id
        )
    facts.extend(
        (
            _count_fact(call, subject, "현재 연결 조회 건수", retrieved_count, "응답에 수신된 레코드"),
            _count_fact(call, subject, "표시 건수", displayed_count, "답변에 표시된 레코드"),
        )
    )
    total_available = _clinical_total_available(call.render_data)
    if total_available is not None:
        facts.append(
            _count_fact(call, subject, "원천 제공 총 건수", total_available, "upstream totalCount")
        )
    return tuple(facts)


def _clinical_list_scope_note(prefix: str) -> str:
    return (
        f"상태: 확인 불가\n사유: {prefix}\n"
        "범위: 등록 목록만 표시합니다. "
        "의약품별 집계, 순위, 경쟁 분석/서사는 제공하지 않습니다."
    )


def _clinical_total_available(data: dict[str, Any]) -> int | None:
    payload = data.get("payload")
    candidate = data.get("totalCount")
    if candidate is None and isinstance(payload, dict):
        candidate = payload.get("totalCount")
    try:
        total = int(candidate)
    except (TypeError, ValueError):
        return None
    return total if total >= 0 else None


def _count_fact(
    call: ExternalCall,
    subject: str,
    metric: str,
    count: int,
    locator: str,
) -> EvidenceFact:
    slug = re.sub(r"[^0-9a-z가-힣]+", "_", metric.casefold()).strip("_")
    return EvidenceFact(
        fact_id=f"{call.tool}:{slug}",
        subject=subject,
        metric=metric,
        value=Decimal(count),
        unit="건",
        period=None,
        source_name=_source_name(call),
        source_locator=locator,
        raw_ref=f"{call.tool}:{slug}",
    )


def _external_rows(data: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    candidates: Any = data.get("items")
    payload = data.get("payload")
    if not isinstance(candidates, list) and isinstance(payload, dict):
        candidates = payload.get("results") or payload.get("studies")
    if not isinstance(candidates, list):
        return ()
    return tuple(item for item in candidates if isinstance(item, dict))


def _is_adverse_event_call(data: dict[str, Any]) -> bool:
    mcp = data.get("mcp")
    return isinstance(mcp, dict) and mcp.get("tool") == "search_drug_adverse_events"


def _adverse_report_matches_subject(item: dict[str, Any], subject: str) -> bool:
    target = _normalized_drug_term(subject)
    if not target:
        return False
    return any(
        term == target or term.startswith(f"{target} ") or target.startswith(f"{term} ")
        for term in (_normalized_drug_term(value) for value in _adverse_drug_terms(item))
        if term
    )


def _adverse_drug_terms(item: dict[str, Any]) -> tuple[str, ...]:
    terms: list[str] = []
    drug_names = item.get("drug_names")
    if isinstance(drug_names, list):
        terms.extend(str(value) for value in drug_names if value)

    patient = item.get("patient")
    drugs = patient.get("drug") if isinstance(patient, dict) else None
    if not isinstance(drugs, list):
        return tuple(terms)
    for drug in drugs:
        if not isinstance(drug, dict):
            continue
        medicinal_product = drug.get("medicinalproduct")
        if medicinal_product:
            terms.append(str(medicinal_product))
        openfda = drug.get("openfda")
        if not isinstance(openfda, dict):
            continue
        for key in ("generic_name", "substance_name", "brand_name"):
            value = openfda.get(key)
            if isinstance(value, list):
                terms.extend(str(entry) for entry in value if entry)
            elif value:
                terms.append(str(value))
    return tuple(terms)


def _normalized_drug_term(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", " ", value.casefold()).strip()


def _row_fact(
    call: ExternalCall,
    subject: str,
    metric: str,
    item: dict[str, Any],
    index: int,
    *,
    fallback_period: str | None = None,
) -> EvidenceFact:
    value = next((_decimal_or_none(item.get(key)) for key in ("value", "rank", "ptntCnt") if item.get(key) not in (None, "")), None)
    period = next(
        (
            str(item[key])
            for key in ("published_date", "date", "period", "year", "ITEM_PERMIT_DATE", "APPROVAL_TIME")
            if item.get(key)
        ),
        fallback_period,
    )
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


def _request_period(data: dict[str, Any]) -> str | None:
    request = data.get("request")
    if not isinstance(request, dict):
        return None
    year = request.get("year")
    return str(year) if year not in (None, "") else None


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
    report_id = str(item.get("safety_report_id") or "").strip()
    if report_id:
        parts = [f"FAERS 보고 {report_id}"]
        date = str(item.get("date") or "").strip()
        if date:
            parts.append(date)
        reactions = item.get("reaction_terms")
        if isinstance(reactions, list) and reactions:
            parts.append(f"보고 반응: {', '.join(str(value) for value in reactions)}")
        return " · ".join(parts)
    nct_id = str(item.get("NCTId") or item.get("nctId") or "").strip()
    if nct_id:
        parts = [nct_id]
        trial_title = str(item.get("briefTitle") or item.get("title") or "").strip()
        if trial_title:
            parts.append(trial_title)
        trial_url = str(item.get("url") or item.get("clinicaltrials_url") or "").strip()
        if trial_url:
            parts.append(trial_url)
        return " · ".join(parts)
    title = str(item.get("title") or "").strip()
    url = str(item.get("url") or "").strip()
    if title and url:
        safe_title = title.replace("[", "\\[").replace("]", "\\]")
        return f"[{safe_title}]({url})"
    patent_number = str(item.get("DOMESTIC_PATENT_NO") or "").strip()
    if patent_number:
        parts = []
        product = str(item.get("GOODS_NAME") or item.get("ITEM_NAME") or "").strip()
        if product:
            parts.append(product)
        parts.append(f"특허번호 {patent_number}")
        for key, label in (
            ("DOMESTIC_END_DATE", "만료일"),
            ("DOMESTIC_PATENT_STATUS", "상태"),
            ("PATENTEE", "권리자"),
        ):
            value = str(item.get(key) or "").strip()
            if value:
                parts.append(f"{label} {value}")
        return " · ".join(parts)
    product = str(item.get("ITEM_NAME") or item.get("GOODS_NAME") or "").strip()
    company = str(item.get("ENTP_NAME") or "").strip()
    permit_date = str(item.get("ITEM_PERMIT_DATE") or "").strip()
    if product or company or permit_date:
        parts = []
        if product:
            parts.append(product)
        if permit_date:
            parts.append(f"허가일 {permit_date}")
        if company:
            parts.append(company)
        ingredient = str(item.get("ITEM_INGR_NAME") or "").strip()
        if ingredient:
            parts.append(f"성분 {ingredient}")
        return " · ".join(parts)
    keys = (
        "title", "ITEM_NAME", "itemName", "briefTitle", "sickNm", "st5Nm",
        "GOODS_NAME", "INGR_ENG_NAME", "DOMESTIC_PATENT_NO", "PRT_NAME",
        "inpatOpat", "sex", "areaNm", "orgType",
    )
    values = tuple(dict.fromkeys(str(item[key]).strip() for key in keys if item.get(key)))
    return " · ".join(values[:3]) or None


def _source_name(call: ExternalCall) -> str:
    if call.tool.startswith("openfda"):
        mcp = call.render_data.get("mcp")
        if isinstance(mcp, dict) and mcp.get("tool") == "search_drug_adverse_events":
            return "FDA 이상반응 보고 정보"
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


def _composition_locator(item: dict[str, Any]) -> str | None:
    product = str(item.get("PRDUCT") or item.get("ITEM_NAME") or "").strip()
    ingredient = str(
        item.get("MTRAL_NM")
        or item.get("MAIN_INGR_ENG")
        or item.get("ITEM_INGR_NAME")
        or ""
    ).strip()
    if not product or not ingredient:
        return None
    quantity = str(item.get("QNT") or "").strip()
    unit = str(item.get("INGD_UNIT_CD") or "").strip()
    amount = f" {quantity}{unit}" if quantity else ""
    return f"{product} · {ingredient}{amount}"


def _easy_drug_facts(
    call: ExternalCall,
    subject: str,
    item: dict[str, Any],
    row_index: int,
) -> tuple[EvidenceFact, ...]:
    product = str(item.get("itemName") or item.get("ITEM_NAME") or "").strip()
    fields = (
        ("efcyQesitm", "효능·효과"),
        ("useMethodQesitm", "용법·용량"),
        ("atpnWarnQesitm", "주의사항 경고"),
        ("atpnQesitm", "주의사항"),
        ("intrcQesitm", "상호작용"),
        ("seQesitm", "부작용"),
        ("depositMethodQesitm", "보관법"),
    )
    return tuple(
        EvidenceFact(
            fact_id=f"{call.tool}:{subject}:{row_index}:{key}",
            subject=subject,
            metric=label,
            value=None,
            unit=None,
            period=None,
            source_name="식약처 e약은요 정보",
            source_locator=f"{product} · {value}" if product else value,
            raw_ref=f"{call.tool}:{row_index}:{key}",
        )
        for key, label in fields
        if (value := str(item.get(key) or "").strip())
    )


def _success(preview: str, evidence: tuple[EvidenceFact, ...]) -> ToolEnvelope:
    return ToolEnvelope(ok=True, preview=preview, evidence=evidence, raw=None, error_code=None, error_message=None)


def _error(code: str, message: str) -> ToolEnvelope:
    return ToolEnvelope(ok=False, preview=message, evidence=(), raw=None, error_code=code, error_message=message)
