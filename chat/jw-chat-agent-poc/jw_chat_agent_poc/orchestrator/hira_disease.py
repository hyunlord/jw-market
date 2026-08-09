from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Final, Protocol, TypeAlias, TypedDict, assert_never

from jw_chat_agent_poc.common.periods import requested_period
from jw_chat_agent_poc.orchestrator.markdown_formatting import hira_disease_display_name
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
HIRA_TREND_YEARS = tuple(str(year) for year in range(2020, 2025))
DISEASE_CODE_RE = re.compile(r"(?<![A-Za-z0-9])(?P<code>[A-Za-z]\d{2}(?:\.?\d)?)(?![A-Za-z0-9])")


@dataclass(frozen=True, slots=True)
class HiraDiseaseCandidate:
    sick_cd: str
    disease_name: str


@dataclass(frozen=True, slots=True)
class HiraDiseaseCodeResolved:
    search_call: ExternalCall
    candidate: HiraDiseaseCandidate


@dataclass(frozen=True, slots=True)
class HiraDiseaseCodeAmbiguous:
    search_call: ExternalCall
    candidates: tuple[HiraDiseaseCandidate, ...]


@dataclass(frozen=True, slots=True)
class HiraDiseaseCodeAbsent:
    search_call: ExternalCall
    query: str


@dataclass(frozen=True, slots=True)
class HiraDiseaseCodeUnavailable:
    search_call: ExternalCall
    query: str


HiraDiseaseCodeResolution: TypeAlias = (
    HiraDiseaseCodeResolved
    | HiraDiseaseCodeAmbiguous
    | HiraDiseaseCodeAbsent
    | HiraDiseaseCodeUnavailable
)
MAX_HIRA_DISEASE_CANDIDATES = 5
HIRA_DISEASE_STATISTICS_URL: Final = (
    "https://opendata.hira.or.kr/op/opc/olapHthInsRvStatInfoTab1.do"
)


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

HIRA_DISEASE_TEXT_BRANDS: dict[str, str] = {
    "이상지질": "리바로",
    "고지혈": "리바로",
    "지질단백질": "리바로",
    "당뇨": "가드메트",
    "혈우": "헴리브라",
    "빈혈": "페린젝트",
    "류마티스": "악템라",
}

HIRA_DISEASE_TEXT_EVIDENCE_LABELS: dict[str, tuple[str, ...]] = {
    "이상지질": ("이상지질", "이상지질혈증"),
    "고지혈": ("고지혈증",),
    "지질단백질": ("지질단백질", "지질단백질대사장애"),
    "당뇨": ("당뇨", "당뇨병"),
    "혈우": ("혈우", "혈우병"),
    "류마티스": ("류마티스", "류마티스관절염"),
}

_HIRA_EXACT_DISEASE_ALIASES: dict[str, str] = {
    "고지혈증": "고지혈",
    "이상지질혈증": "이상지질",
    "지질단백질대사장애": "지질단백질",
    "제2형 당뇨병": "당뇨",
    "2형 당뇨병": "당뇨",
    "철결핍성 빈혈": "빈혈",
    "철결핍빈혈": "빈혈",
    "A형 혈우병": "혈우",
    "혈청검사양성 류마티스관절염": "류마티스",
}
_HIRA_SIMILAR_DISEASE_QUERIES: dict[str, tuple[str, str]] = {
    "당뇨망막병증": ("당뇨병성 망막병증", "2"),
}
_HIRA_ALIAS_POSTPOSITION = "의|은|는|이|가|을|를|에서|에|으로|로|와|과|도|만"


@dataclass(frozen=True, slots=True)
class HiraStatRequest:
    """One HIRA statistic a question asks for.

    ``periods`` empty means a single call at the API's own default year; a
    populated tuple means one call per period. ``label`` is the human name the
    verification contract publishes for this requirement, carried here so the
    reason a tool was invoked is answerable from the request itself.
    """

    tool: str
    periods: tuple[str, ...] = ()
    label: str = ""


_HIRA_STATISTICS = "hira_disease_hospitalization_outpatient_stats"
_HIRA_DISTRIBUTION_TOKENS: tuple[str, ...] = (
    "환자분포",
    "환자 분포",
    "환자통계",
    "환자 통계",
    "질병통계",
    "질병 통계",
    "질환통계",
    "질환 통계",
)
#: A span written as a quantity and a unit — "5년간", "18개월간".
_HIRA_SPAN_PATTERN: Final = re.compile(r"(\d{1,2})\s*(년|개월|달)\s*간")
#: The normalized relative range common.periods already produces — "최근 5년".
_HIRA_RELATIVE_PATTERN: Final = re.compile(r"최근\s*(\d{1,2})\s*(년|개월|달)")
#: A time grain — "연도별", "년도별", "해마다".
_HIRA_GRAIN_PATTERN: Final = re.compile(r"(?:연도|년도|해)\s*(?:별|마다)")
_HIRA_YEAR_PATTERN: Final = re.compile(r"20\d{2}")
#: Bare nouns that ask for a span without naming one. No pattern can reach these,
#: so they are enumerated — and this axis is the one that keeps leaking. Anything
#: carrying a quantity, a unit or a grain is handled by the patterns above and
#: does not need to be listed.
_HIRA_SPAN_WORDS: tuple[str, ...] = ("추이", "시계열")


def hira_requested_years(question: str) -> tuple[str, ...] | None:
    """The year span a question asks for, or None when it asks for one point.

    Asking "does this demand more than one period" rather than "does this contain
    a trend word": "최근 5년" names no trend and demands five. The quantity, span,
    explicit-year and grain forms are shapes, so an expression nobody has seen
    still resolves if it has the shape.
    """
    lowered = question.casefold()
    named = tuple(dict.fromkeys(_HIRA_YEAR_PATTERN.findall(question)))
    within_window = [year for year in named if year in HIRA_TREND_YEARS]
    if len(within_window) >= 2:
        first, last = min(within_window), max(within_window)
        return tuple(year for year in HIRA_TREND_YEARS if first <= year <= last)
    months = _hira_span_months(question)
    if months is not None:
        years = max(1, min(len(HIRA_TREND_YEARS), -(-months // 12)))
        return HIRA_TREND_YEARS[-years:] if years >= 2 else None
    if _HIRA_GRAIN_PATTERN.search(lowered):
        return HIRA_TREND_YEARS
    if any(word in lowered for word in _HIRA_SPAN_WORDS):
        return HIRA_TREND_YEARS
    return None


def hira_binding_question(question: str) -> str:
    """Expose every year in an explicit HIRA range to the binding boundary."""
    if not is_hira_disease_question(question):
        return question
    named = tuple(dict.fromkeys(_HIRA_YEAR_PATTERN.findall(question)))
    if len(named) < 2:
        return question
    requested = hira_requested_years(question)
    if not requested:
        return question
    missing = tuple(year for year in requested if year not in named)
    if not missing:
        return question
    return f"{question} (요청 범위 포함 연도: {', '.join(missing)})"


def _hira_span_months(question: str) -> int | None:
    """Months requested, read off the period parser the codebase already has.

    common.periods.requested_period normalizes "최근 5년" for us. It tries its
    year branch before its relative-range branch, though, so a two-digit count is
    swallowed: "최근 20년" comes back as the year '2020', "최근 10년" as '2010'.
    That is a defect in the shared parser and fixing it there would reach every
    metric route, so the raw question is matched here as well rather than
    changing behaviour outside HIRA.
    """
    requested = requested_period(question)
    if requested:
        match = _HIRA_RELATIVE_PATTERN.fullmatch(requested.strip())
        if match is not None:
            return _hira_months(match)
    match = _HIRA_RELATIVE_PATTERN.search(question) or _HIRA_SPAN_PATTERN.search(question)
    return _hira_months(match) if match is not None else None


def _hira_months(match: re.Match[str]) -> int:
    return int(match.group(1)) * (12 if match.group(2) == "년" else 1)


def hira_stat_requests(question: str) -> tuple[HiraStatRequest, ...]:
    """Which HIRA statistics this question asks for.

    The single source for both the executor and the verification contract. They
    used to decide separately — the executor on one keyword, the contract on this
    vocabulary — and disagreed on the default, so three banded tools were called
    that nothing had asked for. Anything not recognised here falls to the stated
    default below, never to "call everything".
    """
    lowered = question.casefold()
    normalized = lowered.strip().rstrip(".?!。？！").strip()
    if normalized.endswith(("질환", "질병")):
        return ()
    years = hira_requested_years(question)
    if years:
        return (
            HiraStatRequest(_HIRA_STATISTICS, years, f"HIRA {years[0]}~{years[-1]} 환자 추이"),
        )
    if any(token in lowered for token in _HIRA_DISTRIBUTION_TOKENS):
        return (
            HiraStatRequest(_HIRA_STATISTICS, (), "HIRA 입원/외래"),
            HiraStatRequest("hira_disease_gender_age_stats", (), "HIRA 성별/연령"),
            HiraStatRequest("hira_disease_institution_class_stats", (), "HIRA 기관종별"),
            HiraStatRequest("hira_disease_area_stats", (), "HIRA 지역"),
        )
    if any(token in lowered for token in ("성별", "연령", "나이")):
        return (HiraStatRequest("hira_disease_gender_age_stats", (), "HIRA 성별/연령"),)
    if any(token in lowered for token in ("기관", "종별")):
        return (HiraStatRequest("hira_disease_institution_class_stats", (), "HIRA 기관종별"),)
    if any(token in lowered for token in ("지역", "시도")):
        return (HiraStatRequest("hira_disease_area_stats", (), "HIRA 지역"),)
    return (HiraStatRequest(_HIRA_STATISTICS, (), "HIRA 입원/외래"),)


def hira_disease_anchor_brand(question: str) -> str | None:
    """Resolve a disease-only question through the explicit HIRA mapping."""

    return next((brand for token, brand in HIRA_DISEASE_TEXT_BRANDS.items() if token in question), None)


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
        external_calls = _hira_external_calls(question, external, sick_cd)
        for call in external_calls:
            calls.append(_with_hira_mapping_context(call, resolution.canonical_brand, mapping, index, total))
    return calls


def hira_disease_code_calls(question: str, sick_cd: str, external: ExternalApiClient) -> list[ExternalCall]:
    code = normalize_hira_disease_code(sick_cd)
    requested_years = hira_requested_years(question) or ()
    return [
        _with_hira_direct_code_context(call, code, requested_years)
        for call in _hira_stat_external_calls(question, external, code)
    ]


def hira_direct_disease_calls(question: str, disease_query: str, external: ExternalApiClient) -> list[ExternalCall]:
    """Resolve a user disease name/code through HIRA search_disease_code before stats."""

    resolution = resolve_hira_disease_code(disease_query, external)
    similar_query: str | None = None
    if isinstance(resolution, HiraDiseaseCodeAbsent):
        retry = _similar_hira_disease_query(disease_query)
        if retry is not None:
            similar_query, sick_type = retry
            resolution = resolve_hira_disease_code(similar_query, external, sick_type=sick_type)
    match resolution:
        case HiraDiseaseCodeResolved(search_call=search_call, candidate=candidate):
            basis = f"HIRA search_disease_code 단일 후보({disease_query})"
            if similar_query is not None:
                basis = f"HIRA 유사 질환명 재조회({disease_query} → {similar_query})"
            mapping = _hira_mapping(
                candidate.sick_cd,
                candidate.disease_name,
                basis,
            )
            calls = [
                _with_hira_direct_search_context(
                    search_call,
                    candidate,
                    original_query=disease_query,
                    similar_query=similar_query,
                )
            ]
            calls.extend(
                _with_hira_mapping_context(call, disease_query, mapping, 1, 1)
                for call in _hira_stat_external_calls(question, external, candidate.sick_cd)
            )
            return calls
        case HiraDiseaseCodeAmbiguous(search_call=search_call, candidates=candidates):
            return [_hira_code_ambiguous_call(similar_query or disease_query, search_call, candidates)]
        case HiraDiseaseCodeAbsent(search_call=search_call, query=query):
            return [_hira_code_absent_call(disease_query if similar_query else query, search_call)]
        case HiraDiseaseCodeUnavailable(search_call=search_call):
            return [search_call]
        case unreachable:
            assert_never(unreachable)


def resolve_hira_disease_code(
    disease_query: str,
    external: ExternalApiClient,
    *,
    sick_type: str | None = None,
) -> HiraDiseaseCodeResolution:
    search_call = (
        external.hira_disease_name_code(disease_query)
        if sick_type is None
        else external.hira_disease_name_code(disease_query, sick_type=sick_type)
    )
    candidates = _hira_candidates(search_call)
    if _hira_code_lookup_unavailable(search_call):
        return HiraDiseaseCodeUnavailable(search_call=search_call, query=disease_query)
    if not candidates:
        return HiraDiseaseCodeAbsent(search_call=search_call, query=disease_query)
    if len(candidates) == 1:
        return HiraDiseaseCodeResolved(search_call=search_call, candidate=candidates[0])
    return HiraDiseaseCodeAmbiguous(search_call=search_call, candidates=candidates)


def _similar_hira_disease_query(disease_query: str) -> tuple[str, str] | None:
    normalized = re.sub(r"\s+", "", disease_query).casefold()
    return _HIRA_SIMILAR_DISEASE_QUERIES.get(normalized)


def _hira_external_calls(question: str, external: ExternalApiClient, sick_cd: str) -> tuple[ExternalCall, ...]:
    return (
        external.hira_disease_name_code(sick_cd),
        *_hira_stat_external_calls(question, external, sick_cd),
    )


def _hira_stat_external_calls(question: str, external: ExternalApiClient, sick_cd: str) -> tuple[ExternalCall, ...]:
    """Call exactly the statistics hira_stat_requests says the question asks for.

    This used to decide for itself on one keyword and otherwise call all four,
    including three banded tables the verification contract never required.
    """
    return tuple(
        _hira_stat_call(request, external, sick_cd, period)
        for request in hira_stat_requests(question)
        for period in (request.periods or (None,))
    )


def _hira_stat_call(
    request: HiraStatRequest,
    external: ExternalApiClient,
    sick_cd: str,
    period: str | None,
) -> ExternalCall:
    caller = getattr(external, request.tool)
    call = caller(sick_cd) if period is None else caller(sick_cd, period)
    if not _hira_call_unavailable(call) and not _hira_call_has_no_data(call):
        return _with_hira_stat_outcome(call, sick_cd, period, attempt_count=1)
    retried = caller(sick_cd) if period is None else caller(sick_cd, period)
    return _with_hira_stat_outcome(
        retried,
        sick_cd,
        period,
        attempt_count=2,
        first_attempt_status=call.status,
    )


def _with_hira_stat_outcome(
    call: ExternalCall,
    sick_cd: str,
    period: str | None,
    *,
    attempt_count: int,
    first_attempt_status: str | None = None,
) -> ExternalCall:
    summary = call.summary_text
    outcome = "success"
    request = call.render_data.get("request")
    observed_period = (
        period
        or (str(request.get("year") or "").strip() if isinstance(request, dict) else "")
        or "기본 연도"
    )
    if _hira_call_unavailable(call):
        outcome = "query_failed_after_retry" if attempt_count > 1 else "query_failed"
        suffix = "(재시도함)" if attempt_count > 1 else ""
        summary = f"HIRA KCD {sick_cd}의 {observed_period} 환자수 조회 실패{suffix}."
    elif _hira_call_has_no_data(call):
        outcome = "data_absent_after_retry" if attempt_count > 1 else "data_absent"
        summary = f"HIRA KCD {sick_cd}의 {observed_period} 환자수 데이터 없음."
    elif attempt_count > 1:
        summary = f"HIRA KCD {sick_cd}의 {observed_period} 환자수를 재시도 후 조회했습니다."
    return ExternalCall(
        tool=call.tool,
        source=call.source,
        status=call.status,
        summary_text=summary,
        render_data={
            **call.render_data,
            "lookup_outcome": outcome,
            "attempt_count": attempt_count,
            "retry_attempted": attempt_count > 1,
            "first_attempt_status": first_attempt_status,
        },
        safe_url=call.safe_url,
        elapsed_ms=call.elapsed_ms,
    )


def _with_hira_direct_code_context(
    call: ExternalCall,
    sick_cd: str,
    requested_years: tuple[str, ...],
) -> ExternalCall:
    request = call.render_data.get("request")
    period = str(request.get("year") or "") if isinstance(request, dict) else ""
    summary = call.summary_text
    if call.tool == _HIRA_STATISTICS:
        if _hira_call_unavailable(call):
            suffix = "(재시도함)" if call.render_data.get("retry_attempted") is True else ""
            summary = f"HIRA KCD {sick_cd}의 {period or '기본 연도'} 환자수 조회 실패{suffix}."
        elif _hira_call_has_no_data(call):
            summary = f"HIRA KCD {sick_cd}의 {period or '기본 연도'} 환자수 데이터 없음."
        else:
            summary = f"HIRA KCD {sick_cd}의 {period or '기본 연도'} 입원·외래 환자수를 조회했습니다."
    return ExternalCall(
        tool=call.tool,
        source=call.source,
        status=call.status,
        summary_text=summary,
        render_data={
            **call.render_data,
            "direct_sickCd": sick_cd,
            "direct_code_lookup": True,
            "requested_period": period,
            "requested_periods": list(requested_years),
        },
        safe_url=call.safe_url,
        elapsed_ms=call.elapsed_ms,
    )


def _hira_disease_mappings(question: str, canonical_brand: str) -> tuple[HiraMapping, ...] | None:
    mapping = HIRA_DISEASE_MAPPINGS.get(canonical_brand)
    if mapping is not None:
        return _normalize_hira_mappings(mapping)
    for token, mapping in HIRA_DISEASE_TEXT_MAPPINGS.items():
        if _has_exact_text_evidence_binding(question, token):
            return _normalize_hira_mappings(mapping)
    return None


def hira_disease_code_for_text(text: str) -> str | None:
    """Return one authoritative KCD code when the existing mapping is unambiguous."""

    direct_code = explicit_hira_disease_code(text)
    if direct_code is not None:
        return direct_code
    mappings = _hira_disease_mappings(text, text.strip())
    if mappings is None:
        return None
    codes = {mapping["sick_cd"] for mapping in mappings}
    return next(iter(codes)) if len(codes) == 1 else None


def explicit_hira_disease_code(text: str) -> str | None:
    match = DISEASE_CODE_RE.search(text)
    if match is None:
        return None
    return normalize_hira_disease_code(match.group("code"))


def normalize_hira_disease_code(code: str) -> str:
    compact = code.upper().replace(".", "")
    if len(compact) == 4:
        return f"{compact[:3]}.{compact[3]}"
    return compact


def hira_disease_code_for_exact_name(text: str) -> str | None:
    """Resolve only complete, unambiguous disease names for evidence binding."""

    normalized = re.sub(r"\s+", " ", text.strip())
    codes: set[str] = set()
    for alias, mapping_token in _HIRA_EXACT_DISEASE_ALIASES.items():
        pattern = (
            rf"(?<![0-9A-Za-z가-힣]){re.escape(alias)}"
            rf"(?=(?:(?:{_HIRA_ALIAS_POSTPOSITION}))?(?:\s|[?!.:,]|$))"
        )
        if not re.search(pattern, normalized, flags=re.IGNORECASE):
            continue
        mappings = _normalize_hira_mappings(HIRA_DISEASE_TEXT_MAPPINGS[mapping_token])
        codes.update(mapping["sick_cd"] for mapping in mappings)
    return next(iter(codes)) if len(codes) == 1 else None


def _normalize_hira_mappings(mapping: HiraMappingEntry) -> tuple[HiraMapping, ...]:
    if isinstance(mapping, dict):
        return (mapping,)
    return tuple(mapping)


def _has_exact_text_evidence_binding(question: str, token: str) -> bool:
    from jw_chat_agent_poc.tool_use.routing_v4_capabilities import verify_claim_evidence

    subject = _hira_disease_subject(question)
    if not subject:
        return False
    bound = (_normalize_hira_text_evidence(subject),)
    expected_labels = HIRA_DISEASE_TEXT_EVIDENCE_LABELS.get(token, (token,))
    return any(
        verify_claim_evidence(
            expected_evidence_ids=(_normalize_hira_text_evidence(label),),
            bound_evidence_ids=bound,
        )
        for label in expected_labels
    )


def _hira_disease_subject(question: str) -> str:
    body = re.sub(r"^\s*(?:HIRA|hira)\s*:\s*", "", question, count=1).strip()
    subject = re.split(
        r"(?:의\s*)?(?:환자\s*수|환자수|환자\s*통계|환자통계|환자\s*분포|환자분포|질병\s*통계|질병통계|질환\s*통계|질환통계|관련\s*질병|관련\s*질환)",
        body,
        maxsplit=1,
    )[0]
    return subject.strip(" \t\r\n.?!。？！")


def _normalize_hira_text_evidence(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().casefold())


def _hira_candidates(search_call: ExternalCall) -> tuple[HiraDiseaseCandidate, ...]:
    raw_items = search_call.render_data.get("items")
    if not isinstance(raw_items, list):
        return ()
    candidates: list[HiraDiseaseCandidate] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        raw_code = item.get("sickCd")
        raw_name = item.get("sickNm")
        if not isinstance(raw_code, str) or not isinstance(raw_name, str):
            continue
        sick_cd = raw_code.strip().upper()
        disease_name = raw_name.strip()
        if not sick_cd or not disease_name:
            continue
        candidate = HiraDiseaseCandidate(sick_cd=sick_cd, disease_name=disease_name)
        if candidate not in candidates:
            candidates.append(candidate)
    return tuple(candidates)


def _hira_call_unavailable(call: ExternalCall) -> bool:
    status = call.status.strip().casefold()
    error_code = str(call.render_data.get("error_code") or "").strip()
    return status in {
        "error",
        "failed",
        "query_failed",
        "timeout",
        "tool_timeout",
        "deadline_exceeded",
    } or bool(error_code)


def _hira_call_has_no_data(call: ExternalCall) -> bool:
    return call.status.strip().casefold() == "no_data" and not _hira_call_unavailable(call)


def _hira_code_lookup_unavailable(search_call: ExternalCall) -> bool:
    return _hira_call_unavailable(search_call)


def _candidate_dict(candidate: HiraDiseaseCandidate) -> dict[str, str]:
    return {"sickCd": candidate.sick_cd, "sickNm": candidate.disease_name}


def _hira_code_ambiguous_call(
    query: str,
    search_call: ExternalCall,
    candidates: tuple[HiraDiseaseCandidate, ...],
) -> ExternalCall:
    visible_candidates = candidates[:MAX_HIRA_DISEASE_CANDIDATES]
    return ExternalCall(
        tool="hira_disease_code_ambiguous",
        source="hira_disease",
        status="ambiguous",
        summary_text=f"HIRA search_disease_code에서 {query} 후보가 여러 건이라 통계를 조회하지 않았습니다.",
        render_data={
            "query": query,
            "reason": "multiple_hira_disease_code_candidates",
            "candidates": [_candidate_dict(candidate) for candidate in visible_candidates],
            "candidate_total": len(candidates),
            "candidate_limit": MAX_HIRA_DISEASE_CANDIDATES,
            "candidates_truncated": len(candidates) > len(visible_candidates),
            "search": search_call.render_data,
        },
        safe_url=search_call.safe_url,
        elapsed_ms=search_call.elapsed_ms,
    )


def _hira_code_absent_call(query: str, search_call: ExternalCall) -> ExternalCall:
    user_message = (
        f"해당 질병명에 대응하는 HIRA 상병코드를 찾지 못했습니다: {query}\n\n"
        f"공식 확인 경로: [HIRA 국민관심질병통계]({HIRA_DISEASE_STATISTICS_URL})\n"
        f"검색어: {query}\n"
        "확인 필드: 상병코드(KCD), 질병명\n\n"
        "정확한 질병명으로 다시 입력하거나 상병코드를 직접 알려주시면 재조회할 수 있습니다."
    )
    return ExternalCall(
        tool="hira_disease_code_absent",
        source="hira_disease",
        status="no_data",
        summary_text=user_message,
        render_data={
            "query": query,
            "reason": "hira_disease_code_search_no_data",
            "reason_code": "DISEASE_CODE_ABSENT",
            "user_message": user_message,
            "recovery_action": "정확한 질병명을 다시 입력하거나 HIRA 상병코드를 직접 제공해 주세요.",
            "source": "HIRA 국민관심질병통계",
            "evidence_summary": (
                f"HIRA 질병명·상병코드 조회에서 검색어 '{query}'에 대응하는 후보가 0건이었습니다.",
            ),
            "candidates": [],
            "search": search_call.render_data,
        },
        safe_url=HIRA_DISEASE_STATISTICS_URL,
        elapsed_ms=search_call.elapsed_ms,
    )


def _with_hira_direct_search_context(
    call: ExternalCall,
    candidate: HiraDiseaseCandidate,
    *,
    original_query: str,
    similar_query: str | None,
) -> ExternalCall:
    summary = f"HIRA search_disease_code에서 {candidate.sick_cd}({candidate.disease_name}) 단일 후보를 확인했습니다."
    if similar_query is not None:
        summary = (
            f"HIRA search_disease_code에서 {original_query}의 유사 질환명 "
            f"{similar_query}을 재조회해 {candidate.sick_cd}({candidate.disease_name})를 확인했습니다."
        )
    return ExternalCall(
        tool=call.tool,
        source=call.source,
        status=call.status,
        summary_text=summary,
        render_data={
            **call.render_data,
            "resolved_sickCd": candidate.sick_cd,
            "resolved_disease_name": candidate.disease_name,
            "original_query": original_query,
            "similar_query": similar_query,
            "similar_query_retry": similar_query is not None,
        },
        safe_url=call.safe_url,
        elapsed_ms=call.elapsed_ms,
    )


def _with_hira_mapping_context(
    call: ExternalCall,
    brand: str,
    mapping: HiraMapping,
    index: int,
    total: int,
) -> ExternalCall:
    summary = (
        call.summary_text
        if _hira_call_unavailable(call) or _hira_call_has_no_data(call)
        else _hira_call_summary(call.tool, mapping)
    )
    return ExternalCall(
        tool=call.tool,
        source=call.source,
        status=call.status,
        summary_text=summary,
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
    disease_name = hira_disease_display_name(mapping["disease_name"])
    summaries = {
        "hira_disease_name_code": f"HIRA 질병명칭/코드조회에서 {sick_cd}({disease_name}) 코드를 확인했습니다.",
        "hira_disease_hospitalization_outpatient_stats": f"HIRA 질병입원외래별통계에서 {sick_cd}({disease_name}) 연간 입원/외래 환자수 분포를 확인했습니다.",
        "hira_disease_gender_age_stats": f"HIRA 질병성별연령별통계 API를 KCD {sick_cd} 기준으로 조회했습니다.",
        "hira_disease_institution_class_stats": f"HIRA 질병요양기관종별통계 API를 KCD {sick_cd} 기준으로 조회했습니다.",
        "hira_disease_area_stats": f"HIRA 질병지역별통계 API를 KCD {sick_cd} 기준으로 조회했습니다.",
    }
    return summaries.get(tool, f"HIRA 질병정보서비스 API를 KCD {sick_cd} 기준으로 조회했습니다.")
