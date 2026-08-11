from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, InvalidOperation
from datetime import UTC, datetime
from typing import Any

from jw_chat_agent_poc.service.v4.contracts import (
    Citation,
    EvidenceEnvelope,
    SourceName,
    SourceResult,
)


_NCT_RE = re.compile(r"\bNCT\d{8}\b", re.IGNORECASE)
_HIRA_CODE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]\d{2}(?:\.?\d)?)(?![A-Za-z0-9])")
_RECENT_YEAR_RE = re.compile(r"최근\s*(\d{1,2})\s*년")
_YEAR_RE = re.compile(r"20\d{2}")
_MART_TERMS = (
    "매출",
    "점유율",
    "순위",
    "hhi",
    "집중도",
    "성장",
    "증감",
    "yoy",
    "cagr",
    "경쟁",
    "시장 규모",
    "원인분석",
    "요즘",
)
_DISEASE_ALIASES = {
    "당뇨망막병증": ("H360", "diabetic retinopathy"),
    "당뇨병성 망막병증": ("H360", "diabetic retinopathy"),
    "뇌경색": ("I63", "cerebral infarction"),
}
_INGREDIENT_ALIASES = {
    "스타틴": "Pitavastatin",
    "피타바스타틴": "Pitavastatin",
    "리바로": "Pitavastatin",
}
_REIMBURSEMENT_ALIASES = {
    "aflibercept": "아일리아",
    "애플리버셉트": "아일리아",
}
_REIMBURSEMENT_TERMS = re.compile(
    r"(?:요양)?급여\s*(?:적용)?기준(?:\s*및\s*방법)?|건강보험|약제|고시",
    re.IGNORECASE,
)
_HIRA_ADDITIVE_UNITS = {
    "ptntCnt": "명",
    "rvdInsupBrdnAmt": "천원",
    "rvdRpeTamtAmt": "천원",
    "specCnt": "건",
    "vstDdcnt": "일",
}
_SOURCE_SCOPE = {
    "mart": "KR",
    "nedrug": "KR",
    "hira": "KR",
    "openfda": "US",
    "clinicaltrials": "GLOBAL",
    "web": "GLOBAL",
    "patent": "GLOBAL",
}


@dataclass(frozen=True)
class _FailedHiraYearCall:
    tool: str
    source: str
    status: str
    summary_text: str
    render_data: dict[str, Any]
    safe_url: str | None = None
    elapsed_ms: float | None = None


def _decimal_value(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", ""))
    except InvalidOperation:
        return None


def _decimal_text(value: Decimal) -> str:
    if value == value.to_integral_value():
        return str(int(value))
    return format(value.normalize(), "f")


def _aggregate_hira_items(items: Sequence[Any]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        key = (
            str(item.get("inpatOpat") or ""),
            str(item.get("sickCd") or ""),
            str(item.get("sickNm") or ""),
        )
        groups.setdefault(key, []).append(item)

    aggregated: list[dict[str, Any]] = []
    for (patient_type, code, name), rows in groups.items():
        output: dict[str, Any] = {
            "inpatOpat": patient_type,
            "sex": rows[0].get("sex") if len(rows) == 1 else None,
        }
        if code:
            output["sickCd"] = code
        if name:
            output["sickNm"] = name
        for field in _HIRA_ADDITIVE_UNITS:
            values = [_decimal_value(row.get(field)) for row in rows]
            numeric = [value for value in values if value is not None]
            if numeric:
                output[field] = _decimal_text(sum(numeric, Decimal(0)))
        output["units"] = dict(_HIRA_ADDITIVE_UNITS)
        output["sexBreakdown"] = [
            {
                key: value
                for key, value in row.items()
                if key in {"sex", *_HIRA_ADDITIVE_UNITS}
            }
            for row in rows
        ]
        aggregated.append(output)
    return aggregated


def _normalize_hira_render_data(render_data: Any) -> Any:
    if not isinstance(render_data, dict):
        return render_data
    raw_items: Any = None
    mcp = render_data.get("mcp")
    if isinstance(mcp, dict) and isinstance(mcp.get("content_text"), str):
        try:
            decoded = json.loads(mcp["content_text"])
        except (json.JSONDecodeError, TypeError):
            decoded = None
        if isinstance(decoded, list):
            raw_items = decoded
        elif isinstance(decoded, dict) and isinstance(decoded.get("items"), list):
            raw_items = decoded["items"]
    if raw_items is None:
        raw_items = render_data.get("items")
    if not isinstance(raw_items, list):
        return render_data
    return {**render_data, "items": _aggregate_hira_items(raw_items)}


def _walk_named_values(value: Any, names: set[str]) -> tuple[str, ...]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key.casefold() in names:
                candidates = item if isinstance(item, list) else [item]
                for candidate in candidates:
                    if isinstance(candidate, (str, int, float)) and str(candidate).strip():
                        found.append(str(candidate).strip())
            found.extend(_walk_named_values(item, names))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk_named_values(item, names))
    return tuple(dict.fromkeys(found))


def _evidence_envelope(
    source: SourceName,
    query: str,
    payload: Any,
) -> EvidenceEnvelope:
    years = tuple(dict.fromkeys(re.findall(r"(?:19|20)\d{2}", query)))
    payload_years = _walk_named_values(payload, {"year", "period", "yyyymm"})
    time_match = (
        "NOT_REQUESTED"
        if not years
        else "MATCH"
        if any(any(year in value for value in payload_years) for year in years)
        else "MISMATCH"
    )
    matches = tuple(value.casefold() for value in _walk_named_values(payload, {"match_scope", "entity_match"}))
    entity_match = (
        "MISMATCH"
        if any("mismatch" in value for value in matches)
        else "PARTIAL"
        if any("partial" in value or "component" in value for value in matches)
        else "EXACT"
    )
    common = {
        "entity_match": entity_match,
        "source_scope": _SOURCE_SCOPE[source],
        "time_match": time_match,
    }
    if source == "hira":
        reimbursement = "급여" in query
        return EvidenceEnvelope(
            kind="hira",
            **common,
            metric_type="reimbursement_criteria" if reimbursement else "patient_count",
            period=years or payload_years,
            unit={} if reimbursement else dict(_HIRA_ADDITIVE_UNITS),
            eligible_claims=("reimbursement_criteria",) if reimbursement else ("patient_count", "association"),
            causal=False,
        )
    if source == "clinicaltrials":
        return EvidenceEnvelope(
            kind="clinical",
            **common,
            study_type=next(iter(_walk_named_values(payload, {"studytype"})), None),
            intervention_type=_walk_named_values(payload, {"type", "interventiontype"}),
            phase=_walk_named_values(payload, {"phases", "phase"}),
            recruitment_status=next(iter(_walk_named_values(payload, {"overallstatus", "status"})), None),
            country=_walk_named_values(payload, {"country"}),
            disease=_walk_named_values(payload, {"conditions", "condition"}),
            eligible_claims=("study_design", "phase", "recruitment_status", "enrollment", "eligibility"),
            causal=False,
        )
    if source == "nedrug":
        return EvidenceEnvelope(
            kind="nedrug",
            **common,
            product=_walk_named_values(payload, {"item_name", "product_name"}),
            ingredient=_walk_named_values(payload, {"item_ingr_name", "ingredient"}),
            company=_walk_named_values(payload, {"entp_name", "manufacturer", "company"}),
            approval_date=_walk_named_values(payload, {"item_permit_date", "permit_date", "approval_date"}),
            eligible_claims=("approval", "efficacy", "ingredient", "company"),
            causal=False,
        )
    kind = "clinical" if source == "clinicaltrials" else source
    return EvidenceEnvelope(
        kind=kind,
        **common,
        eligible_claims=("observed_fact",),
        causal=False if source in {"openfda", "web"} else None,
    )


def _parallel_hira_year_calls(
    fetch: Callable[[str, str], Any],
    code: str,
    years: Sequence[str],
) -> list[Any]:
    """Fetch independent HIRA years concurrently and retry only failed years."""

    async def gather() -> list[Any]:
        tasks = [asyncio.create_task(asyncio.to_thread(fetch, code, year)) for year in years]
        try:
            first = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            raise
        failed = [
            index
            for index, result in enumerate(first)
            if isinstance(result, Exception)
            or str(getattr(result, "status", "")).casefold() == "error"
        ]
        if failed:
            retries = await asyncio.gather(
                *(asyncio.to_thread(fetch, code, years[index]) for index in failed),
                return_exceptions=True,
            )
            for index, result in zip(failed, retries, strict=True):
                first[index] = result
        output: list[Any] = []
        for year, result in zip(years, first, strict=True):
            if isinstance(result, Exception):
                output.append(
                    _FailedHiraYearCall(
                        tool="hira_year_query",
                        source="hira",
                        status="error",
                        summary_text=f"{year}년 HIRA 조회 실패",
                        render_data={"request": {"sickCd": code, "year": year}, "items": []},
                    )
                )
            else:
                output.append(result)
        return output

    return asyncio.run(gather())


def _nct_id(query: str) -> str | None:
    match = _NCT_RE.search(query)
    return match.group(0).upper() if match else None


def _hira_code(query: str) -> str | None:
    match = _HIRA_CODE_RE.search(query)
    if match:
        return match.group(1).upper().replace(".", "")
    normalized = query.replace(" ", "")
    for alias, (code, _condition) in _DISEASE_ALIASES.items():
        if alias.replace(" ", "") in normalized:
            return code
    return None


def _requested_hira_years(
    query: str,
    *,
    current_year: int | None = None,
) -> tuple[str, ...] | None:
    """Return the complete calendar range requested by the V4 HIRA query."""
    year_now = current_year or datetime.now(UTC).year
    named = tuple(dict.fromkeys(_YEAR_RE.findall(query)))
    if len(named) >= 2:
        first, last = sorted((int(min(named)), int(max(named))))
        return tuple(str(year) for year in range(first, last + 1))
    if named:
        return named
    recent = _RECENT_YEAR_RE.search(query)
    if recent:
        count = max(1, min(10, int(recent.group(1))))
        return tuple(str(year) for year in range(year_now - count + 1, year_now + 1))
    if "추이" in query or "시계열" in query or re.search(r"(?:연도|년도|해)\s*(?:별|마다)", query):
        return tuple(str(year) for year in range(year_now - 4, year_now + 1))
    return None


def _ingredient_query(query: str) -> str:
    lowered = query.casefold()
    for alias, ingredient in _INGREDIENT_ALIASES.items():
        if alias.casefold() in lowered:
            return ingredient
    return _base_query(query)


def _reimbursement_subject(query: str) -> str:
    value = _REIMBURSEMENT_TERMS.sub(" ", _base_query(query))
    value = re.sub(r"\([^)]*\)", " ", value)
    value = " ".join(value.split()).strip()
    lowered = value.casefold()
    for alias, brand in _REIMBURSEMENT_ALIASES.items():
        if alias in lowered:
            return brand
    if value.endswith("주"):
        value = value[:-1]
    return value or _base_query(query)


def _clinical_query(query: str) -> tuple[str, str]:
    normalized = query.replace(" ", "")
    for alias, (_code, condition) in _DISEASE_ALIASES.items():
        if alias.replace(" ", "") in normalized:
            return condition, "condition"
    return _ingredient_query(query), "intervention"


def _mart_relevant(query: str) -> bool:
    lowered = query.casefold()
    return any(term in lowered for term in _MART_TERMS)


def _base_query(query: str) -> str:
    value = " ".join(query.split())
    suffixes = (
        "의약품 허가 효능 성분",
        "급여기준 환자 통계",
        "label safety",
        "clinical trials",
        "특허 만료 공식",
    )
    changed = True
    while changed:
        changed = False
        lowered = value.casefold()
        for suffix in suffixes:
            if lowered.endswith(suffix.casefold()):
                value = value[: -len(suffix)].strip()
                changed = True
                break
    return value or query


def build_source_adapters() -> dict[SourceName, Any]:
    from jw_chat_agent_poc.agent_loop.factory import build_chat_agent_dependencies
    from jw_chat_agent_poc.service.general_view_routing import GeneralRoute, GeneralViewService
    from jw_chat_agent_poc.tools.external.client import ExternalCall
    from jw_chat_agent_poc.tools.external.hira_reimbursement import HiraReimbursementHttpClient

    dependencies = build_chat_agent_dependencies(external_mode="live")
    external = dependencies.external
    general_view = GeneralViewService.from_env(dependencies.resolver)

    def external_calls(source: SourceName, query: str, calls: list[ExternalCall]) -> SourceResult:
        started_at = datetime.now(UTC)
        payloads = [asdict(call) for call in calls]
        usable = [
            call
            for call, payload in zip(calls, payloads, strict=True)
            if call.status not in {"error", "no_data", "unsupported"} and _has_payload(payload)
        ]
        status = "ok" if usable else "empty"
        return SourceResult(
            source=source,
            query=query,
            status=status,
            payload={"calls": payloads},
            evidence=_evidence_envelope(source, query, {"calls": payloads}),
            citations=tuple(
                Citation(
                    source=call.source or source,
                    query=query,
                    url=call.safe_url,
                    retrieved_at=started_at,
                    used=False,
                )
                for call in usable
            ),
            notice=None if status == "ok" else _first_notice(calls),
        )

    def resolved(query: str):
        try:
            return dependencies.resolver.resolve(_base_query(query), allow_default=False)
        except LookupError:
            return None

    def mart(query: str) -> SourceResult:
        started_at = datetime.now(UTC)
        payloads: list[dict[str, Any]] = []
        if not _mart_relevant(query):
            return SourceResult(
                source="mart",
                query=query,
                status="empty",
                payload={"calls": []},
                notice="question does not request a mart-backed metric",
            )
        route = general_view.route(query)
        if route in {GeneralRoute.GENERAL_ONLY, GeneralRoute.DUAL}:
            result = general_view.answer(
                query,
                compact=False,
                dual=route is GeneralRoute.DUAL,
            )
            if _mart_payload_ok(result):
                payloads.append(result)

        layer = dependencies.query_layer
        if route is not GeneralRoute.GENERAL_ONLY and layer is not None:
            try:
                resolution = dependencies.resolver.resolve(query, allow_default=False)
            except LookupError:
                resolution = None
            if resolution is not None:
                payloads.extend(_strategic_mart_calls(layer, resolution.canonical_brand, query))

        source_labels = tuple(
            dict.fromkeys(
                str(payload.get("source") or "mart")
                for payload in payloads
            )
        )
        return SourceResult(
            source="mart",
            query=query,
            status="ok" if payloads else "empty",
            payload={"calls": payloads},
            evidence=_evidence_envelope("mart", query, {"calls": payloads}),
            citations=tuple(
                Citation(
                    source=label,
                    query=query,
                    url="mart://read-only/query-layer",
                    retrieved_at=started_at,
                    used=False,
                )
                for label in source_labels
            ),
            notice=None if payloads else "mart read-only adapters returned no rows",
        )

    def nedrug(query: str) -> SourceResult:
        base = _base_query(query)
        resolution = resolved(query)
        brand = resolution.canonical_brand if resolution is not None else base
        search = external.mfds_permission_search(brand)
        calls = [search]
        item_seq = _find_value(search.render_data, "ITEM_SEQ", "item_seq")
        if item_seq:
            calls.append(external.mfds_permission_detail(str(item_seq)))
        if "특허" in query and resolution is not None:
            for molecule in resolution.molecule_en:
                calls.extend((external.mfds_patent(molecule), external.mfds_fda_orangebook(molecule)))
        result = external_calls("nedrug", query, calls)
        requested_periods = _requested_hira_years(base)
        if requested_periods:
            result = result.model_copy(
                update={
                    "payload": {
                        **result.payload,
                        "period_coverage": _period_coverage_from_calls(
                            requested_periods,
                            calls,
                        ),
                    }
                }
            )
        return result

    def hira(query: str) -> SourceResult:
        base = _base_query(query)
        resolution = resolved(query)
        if "급여" in base:
            subject = _reimbursement_subject(base)
            subject_resolution = resolved(subject)
            brand = (
                subject_resolution.canonical_brand
                if subject_resolution is not None
                else subject
            )
            criterion = HiraReimbursementHttpClient(timeout_s=7).fetch(
                brand
            )
            if criterion is None:
                return external_calls("hira", query, [])
            call = ExternalCall(
                tool="hira_reimbursement_detail",
                source="hira_reimbursement",
                status="ok",
                summary_text=criterion.raw_text,
                render_data=asdict(criterion),
                safe_url=criterion.source_url,
            )
            return external_calls("hira", query, [call])

        code = _hira_code(base)
        if code:
            calls = [external.hira_disease_name_code(code)]
            years = _requested_hira_years(base) or ("2024",)

            def fetch_hira_year(disease_code: str, year: str) -> ExternalCall:
                call = external.hira_disease_hospitalization_outpatient_stats(
                    disease_code,
                    year,
                )
                return replace(
                    call,
                    render_data=_normalize_hira_render_data(call.render_data),
                )

            calls.extend(
                _parallel_hira_year_calls(
                    fetch_hira_year,
                    code,
                    years,
                )
            )
            result = external_calls("hira", query, calls)
            coverage = {
                "requested_periods": list(years),
                "periods": [
                    {"period": year, "status": _hira_period_status(call)}
                    for year, call in zip(years, calls[1:], strict=True)
                ],
            }
            return result.model_copy(
                update={"payload": {**result.payload, "period_coverage": coverage}}
            )
        return external_calls("hira", query, [external.hira_disease_name_code(base)])

    def openfda(query: str) -> SourceResult:
        resolution = resolved(query)
        ingredients = resolution.molecule_en if resolution and resolution.molecule_en else (_ingredient_query(query),)
        return external_calls(
            "openfda",
            query,
            [external.openfda_label_search(ingredient) for ingredient in ingredients],
        )

    def clinicaltrials(query: str) -> SourceResult:
        nct_id = _nct_id(query)
        if nct_id:
            return external_calls(
                "clinicaltrials", query, [external.clinicaltrials_study_details(nct_id)]
            )
        resolution = resolved(query)
        if resolution is not None and resolution.molecule_en:
            calls = [
                external.clinicaltrials_v2_search(molecule, query_type="intervention")
                for molecule in resolution.molecule_en
            ]
        else:
            term, query_type = _clinical_query(query)
            calls = [external.clinicaltrials_v2_search(term, query_type=query_type)]
        return external_calls("clinicaltrials", query, calls)

    return {
        "mart": mart,
        "nedrug": nedrug,
        "hira": hira,
        "openfda": openfda,
        "clinicaltrials": clinicaltrials,
        "web": lambda query: external_calls("web", query, [external.web_search(_base_query(query))]),
        "patent": lambda query: external_calls(
            "patent", query, [external.web_search(f"{_base_query(query)} 특허 만료 공식")]
        ),
    }


def _strategic_mart_calls(layer, brand: str, query: str) -> list[dict[str, Any]]:
    lowered = query.casefold()
    metrics: list[str] = []
    if "점유율" in lowered:
        metrics.append("share")
    if "순위" in lowered:
        metrics.append("rank")
    if "hhi" in lowered or "집중도" in lowered:
        metrics.append("hhi")
    if any(token in lowered for token in ("성장", "증감", "yoy", "cagr")):
        metrics.append("growth")
    if "매출" in lowered or not metrics:
        metrics.append("sales")

    calls: list[dict[str, Any]] = []
    history_points = 60 if any(
        token in lowered
        for token in ("추이", "시계열", "최근 5년", "최근5년", "변해", "변화", "변동")
    ) else 10
    for metric in dict.fromkeys(metrics):
        try:
            calls.append(
                layer.brand_metric(
                    brand,
                    metric,
                    "latest",
                    history_points=history_points,
                )
            )
        except LookupError:
            continue
    if any(token in lowered for token in ("경쟁", "경쟁사", "시장", "요즘")):
        try:
            calls.append(layer.top_brands(brand, limit=5, metric="sales"))
        except LookupError:
            pass
    return calls


def _mart_payload_ok(payload: dict[str, Any]) -> bool:
    status = str(payload.get("status") or "ok").casefold()
    render_data = payload.get("render_data")
    return status not in {"error", "no_data", "unsupported"} and isinstance(render_data, dict) and bool(render_data)


def _has_payload(payload: dict[str, Any]) -> bool:
    render_data = payload.get("render_data")
    if isinstance(render_data, dict):
        return bool(render_data)
    return bool(payload.get("summary_text"))


def _hira_period_status(call: Any) -> str:
    status = str(getattr(call, "status", "") or "").casefold()
    if status in {"error", "timeout", "missing_key", "unsupported"}:
        return "error"
    render_data = getattr(call, "render_data", None)
    items = render_data.get("items") if isinstance(render_data, dict) else None
    if status == "no_data" or items == []:
        return "no_data"
    return "ok"


def _period_coverage_from_calls(
    requested_periods: Sequence[str],
    calls: Sequence[Any],
) -> dict[str, Any]:
    serialized = " ".join(str(asdict(call)) for call in calls)
    call_statuses = {
        str(getattr(call, "status", "") or "").casefold()
        for call in calls
    }
    failed = bool(call_statuses) and call_statuses <= {
        "error",
        "timeout",
        "missing_key",
        "unsupported",
    }
    return {
        "requested_periods": list(requested_periods),
        "periods": [
            {
                "period": period,
                "status": "ok" if period in serialized else "error" if failed else "no_data",
            }
            for period in requested_periods
        ],
    }


def _first_notice(calls: list[Any]) -> str:
    for call in calls:
        summary = str(getattr(call, "summary_text", "") or "").strip()
        if summary:
            return summary
    return "source adapter returned no usable rows"


def _find_value(value: Any, *keys: str) -> Any | None:
    if isinstance(value, dict):
        for key in keys:
            if value.get(key) not in (None, ""):
                return value[key]
        for nested in value.values():
            found = _find_value(nested, *keys)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_value(nested, *keys)
            if found not in (None, ""):
                return found
    return None
