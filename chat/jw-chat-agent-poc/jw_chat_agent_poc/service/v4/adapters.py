from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from datetime import UTC, datetime
from typing import Any

from jw_chat_agent_poc.common.periods import requested_period
from jw_chat_agent_poc.service.v4.contracts import (
    ClinicalTrialConcept,
    Citation,
    EvidenceEnvelope,
    SourceName,
    SourceResult,
)
from jw_chat_agent_poc.service.v4.clinical import (
    compile_clinical_query,
    concept_from_query,
)
from jw_chat_agent_poc.service.v4.patent import build_patent_lane_payload
from jw_chat_agent_poc.service.v4.time_context import current_kst_date


LOGGER = logging.getLogger(__name__)
_CANONICAL_DEEP_ANALYSIS_SQL = """
SELECT brand, market_id,
       ai_analysis_json, ai_analysis_short_json, ai_analysis_long_json,
       updated_at, short_generated_at, long_generated_at,
       short_generation_status, long_generation_status
FROM cache_deep_analysis_ai_analysis
WHERE brand = %s
  AND market_id IN ({market_placeholders})
ORDER BY GREATEST(
    COALESCE(long_generated_at, '1000-01-01 00:00:00'),
    COALESCE(short_generated_at, '1000-01-01 00:00:00'),
    COALESCE(updated_at, '1000-01-01 00:00:00')
) DESC, market_id ASC
LIMIT 1
"""
_NCT_RE = re.compile(r"\bNCT\d{8}\b", re.IGNORECASE)
_HIRA_CODE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]\d{2}(?:\.?\d)?)(?![A-Za-z0-9])")
_RECENT_YEAR_RE = re.compile(r"최근\s*(\d{1,2})\s*년")
_YEAR_RE = re.compile(r"20\d{2}")
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
    "rvdInsupBrdnAmt": "원",
    "rvdRpeTamtAmt": "원",
    "specCnt": "건",
    "vstDdcnt": "일",
}
_HIRA_THOUSAND_WON_FIELDS = frozenset({"rvdInsupBrdnAmt", "rvdRpeTamtAmt"})
_SOURCE_SCOPE = {
    "mart": "KR",
    "nedrug": "KR",
    "hira": "KR",
    "openfda": "US",
    "clinicaltrials": "GLOBAL",
    "web": "GLOBAL",
    "patent": "GLOBAL",
}
_V4_GATE_WEB_CACHE: dict[tuple[str, str, str], Any] = {}
_V4_GATE_WEB_CACHE_LOCK = threading.Lock()
_TRANSPORT_ERROR_RE = re.compile(
    r"timed out|read timeout|connect timeout|connection (?:aborted|refused|reset)|"
    r"max retries exceeded|temporary failure in name resolution|name or service not known|"
    r"network is unreachable|remote end closed connection|proxyerror|"
    r"\b(?:502|503|504) server error\b",
    re.IGNORECASE,
)
_PRODUCT_STRENGTH_RE = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:마이크로그램|밀리그램|그램|mg|mcg|g|mL|ml).*$",
    re.IGNORECASE,
)
_PRODUCT_FORM_RE = re.compile(
    r"(?:연질캡슐|경질캡슐|캡슐|시럽|크림|연고|패치|과립|정|주|액|산)$"
)


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


def _hira_additive_value(field: str, value: Any) -> str | None:
    number = _decimal_value(value)
    if number is None:
        return None
    if field in _HIRA_THOUSAND_WON_FIELDS:
        number *= Decimal(1000)
    return _decimal_text(number)


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
                output[field] = _hira_additive_value(
                    field,
                    sum(numeric, Decimal(0)),
                )
        output["units"] = dict(_HIRA_ADDITIVE_UNITS)
        output["sexBreakdown"] = [
            {
                key: (
                    _hira_additive_value(key, value)
                    if key in _HIRA_ADDITIVE_UNITS
                    else value
                )
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


def _payload_keys(value: Any) -> frozenset[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(str(key).casefold())
            keys.update(_payload_keys(item))
    if isinstance(value, list):
        for item in value:
            keys.update(_payload_keys(item))
    return frozenset(keys)


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
        **_trace_only_envelope_fields(source, query, payload),
    }
    if source == "hira":
        keys = _payload_keys(payload)
        markers = tuple(
            value.casefold()
            for value in _walk_named_values(payload, {"tool", "source"})
        )
        eligible_claims: list[str] = []
        if "ptntcnt" in keys:
            eligible_claims.append("patient_count")
        if keys & {"rvdinsupbrdnamt", "rvdrpetamtamt"}:
            eligible_claims.append("cost")
        if any("reimbursement" in marker for marker in markers):
            eligible_claims.append("reimbursement")
        reimbursement = "reimbursement" in eligible_claims
        return EvidenceEnvelope(
            kind="hira",
            **common,
            metric_type="reimbursement_criteria" if reimbursement else "patient_count",
            period=years or payload_years,
            unit={} if reimbursement else dict(_HIRA_ADDITIVE_UNITS),
            eligible_claims=tuple(eligible_claims),
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
        keys = _payload_keys(payload)
        markers = tuple(
            value.casefold()
            for value in _walk_named_values(payload, {"tool", "source"})
        )
        eligible_claims: list[str] = []
        if keys & {
            "item_name",
            "product_name",
            "item_permit_date",
            "permit_date",
            "approval_date",
            "entp_name",
            "manufacturer",
            "company",
        }:
            eligible_claims.append("approval")
        if keys & {
            "ee_doc_data",
            "ud_doc_data",
            "nb_doc_data",
            "efcy_qesitm",
            "use_method_qesitm",
            "atpn_warn_qesitm",
            "atpn_qesitm",
            "intrc_qesitm",
            "se_qesitm",
            "efficacy",
            "indication",
            "dosage",
            "warnings",
        }:
            eligible_claims.append("label")
        if (
            any("patent" in key or "reexam" in key for key in keys)
            or any(
                "patent" in marker or "orangebook" in marker
                for marker in markers
            )
        ):
            eligible_claims.append("patent")
        return EvidenceEnvelope(
            kind="nedrug",
            **common,
            product=_walk_named_values(payload, {"item_name", "product_name"}),
            ingredient=_walk_named_values(payload, {"item_ingr_name", "ingredient"}),
            company=_walk_named_values(payload, {"entp_name", "manufacturer", "company"}),
            approval_date=_walk_named_values(payload, {"item_permit_date", "permit_date", "approval_date"}),
            eligible_claims=tuple(eligible_claims),
            causal=False,
        )
    if source == "patent":
        patent_text = " ".join(
            _walk_named_values(
                payload,
                {
                    "title",
                    "snippet",
                    "summary_text",
                    "patent_expiry",
                    "expiration_date",
                    "reexam_date",
                },
            )
        ).casefold()
        has_patent_evidence = bool(
            re.search(r"patent|특허|reexam|재심사|expir(?:e|es|ed|ation)", patent_text)
        )
        return EvidenceEnvelope(
            kind="patent",
            **common,
            eligible_claims=("patent",) if has_patent_evidence else (),
            causal=False,
        )
    kind = "clinical" if source == "clinicaltrials" else source
    return EvidenceEnvelope(
        kind=kind,
        **common,
        eligible_claims=("observed_fact",),
        causal=False if source in {"openfda", "web"} else None,
    )


def _trace_only_envelope_fields(
    source: SourceName,
    query: str,
    payload: Any,
) -> dict[str, Any]:
    periods = sorted(
        {
            str(value).strip()[:7]
            for value in _walk_named_values(
                payload,
                {"period", "from_period", "to_period", "yyyymm"},
            )
            if re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])(?:-\d{2})?", str(value).strip())
        }
    )
    lowered = query.casefold()
    keys = _payload_keys(payload)
    if "specialty" in keys or "진료과" in lowered:
        grain = "specialty"
    elif "channel" in keys or "채널" in lowered:
        grain = "channel"
    elif keys & {"company", "manufacturer", "entp_name", "company_ranking_series"}:
        grain = "company"
    elif keys & {"ingredient", "item_ingr_name", "ingredient_trend"}:
        grain = "ingredient"
    elif keys & {"brand", "anchor_brand", "brand_value_series_10pt"}:
        grain = "brand"
    elif source == "mart" or "시장" in lowered:
        grain = "market"
    else:
        grain = "unknown"

    parent = next(
        iter(_walk_named_values(payload, {"market_id", "parent_entity", "market"})),
        None,
    )
    attributions = (
        ("observed_association",)
        if source == "mart" and "cause_card_data" in json.dumps(payload, ensure_ascii=False, default=str)
        else ()
    )
    return {
        "subject_grain": grain,
        "period_start": periods[0] if periods else None,
        "period_end": periods[-1] if periods else None,
        "parent_entity": parent,
        "eligible_attributions": attributions,
    }


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


def _hira_query_kind(query: str) -> str:
    code = _hira_code(query)
    if code and any(
        token in query
        for token in ("환자", "통계", "추이", "시계열", "연도별", "연도 별")
    ):
        return "patient"
    if "급여" in query:
        return "reimbursement"
    return "patient" if code else "lookup"


def _requested_hira_years(
    query: str,
    *,
    current_year: int | None = None,
) -> tuple[str, ...] | None:
    """Return the complete calendar range requested by the V4 HIRA query."""
    year_now = current_year or current_kst_date().year
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


def _ingredient_search_term(query: str) -> str | None:
    lowered = query.casefold()
    aliases = [alias for alias in _INGREDIENT_ALIASES if alias.casefold() in lowered]
    if not aliases:
        return None
    alias = max(aliases, key=len)
    return alias if alias == "피타바스타틴" else _INGREDIENT_ALIASES[alias]


def _nedrug_product_brand_hints(product_name: str) -> tuple[str, ...]:
    base = re.sub(r"\([^)]*\)", "", product_name).strip()
    without_strength = _PRODUCT_STRENGTH_RE.sub("", base).strip()
    without_form = _PRODUCT_FORM_RE.sub("", without_strength).strip()
    return tuple(
        dict.fromkeys(
            value
            for value in (base, without_strength, without_form)
            if value
        )
    )


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


def _reimbursement_lookup_metadata(result: Any, subject: str) -> dict[str, Any]:
    error_code = str(result.error_code or "") or None
    cache_lookup_status = getattr(result.cache_lookup_status, "value", result.cache_lookup_status)
    if result.ok and result.data is not None:
        outcome = "found"
    elif (
        error_code == "REALTIME_NO_EVIDENCE"
        and cache_lookup_status == "brand_unmatched"
    ):
        outcome = "doc_not_found"
    else:
        outcome = "coverage_unknown"
    return {
        "document": "reimbursement",
        "outcome": outcome,
        "subject": subject,
        "error_code": error_code,
    }


def _clinical_query(query: str) -> tuple[str, str]:
    normalized = query.replace(" ", "")
    for alias, (_code, condition) in _DISEASE_ALIASES.items():
        if alias.replace(" ", "") in normalized:
            return condition, "condition"
    return _ingredient_query(query), "intervention"


def _base_query(query: str) -> str:
    value = " ".join(query.split())
    suffixes = (
        "의약품 허가 효능 성분",
        "급여기준 환자 통계",
        "label safety",
        "clinical trials",
        "특허 만료 공식",
        "특허권 등재 현황",
        "재심사 종료일",
        "재심사 기간",
        "재심사 대상",
        "재심사 정보",
        "재심사",
        "허가 현황 및 최근 변경 사항",
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


def _needs_deep_analysis(query: str) -> bool:
    normalized = query.casefold()
    return any(
        marker in normalized
        for marker in ("원인", "왜 ", "이유", "요즘", "시장 어때", "동향", "분석", "전망")
    )


def _load_canonical_deep_analysis(
    brand: str,
    market_ids: tuple[str, ...],
) -> dict[str, Any] | None:
    import pymysql

    if not market_ids:
        return None
    config = {
        "host": os.environ.get("CHAT_QUERY_DB_HOST")
        or os.environ.get("CHAT_CACHE_DB_HOST", ""),
        "port": int(
            os.environ.get("CHAT_QUERY_DB_PORT")
            or os.environ.get("CHAT_CACHE_DB_PORT", "3306")
        ),
        "database": os.environ.get("CHAT_QUERY_DB_NAME")
        or os.environ.get("CHAT_CACHE_DB_NAME", ""),
        "user": os.environ.get("CHAT_QUERY_DB_USER")
        or os.environ.get("CHAT_CACHE_DB_USER", ""),
        "password": os.environ.get("CHAT_QUERY_DB_PASSWORD")
        or os.environ.get("CHAT_CACHE_DB_PASSWORD", ""),
    }
    if not all(config.values()):
        return None
    try:
        with pymysql.connect(
            **config,
            connect_timeout=3,
            read_timeout=5,
            write_timeout=5,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        ) as connection:
            with connection.cursor() as cursor:
                market_placeholders = ", ".join("%s" for _ in market_ids)
                query = _CANONICAL_DEEP_ANALYSIS_SQL.format(
                    market_placeholders=market_placeholders
                )
                cursor.execute(query, (brand, *market_ids))
                row = cursor.fetchone()
    except Exception as exc:  # noqa: BLE001 - fan-out continues without this optional source
        LOGGER.warning(
            "v4 canonical deep analysis read failed error_type=%s",
            type(exc).__name__,
        )
        return None
    return _deep_analysis_call_from_row(row, allowed_market_ids=market_ids)


def _deep_analysis_call_from_row(
    row: Mapping[str, Any] | None,
    *,
    allowed_market_ids: tuple[str, ...],
) -> dict[str, Any] | None:
    if not row:
        return None
    market_id = str(row.get("market_id") or "").strip()
    if allowed_market_ids and market_id not in allowed_market_ids:
        return None
    candidates: list[tuple[datetime, int, str, dict[str, Any]]] = []
    variants = (
        ("long", "ai_analysis_long_json", "long_generated_at", "long_generation_status", 2),
        ("short", "ai_analysis_short_json", "short_generated_at", "short_generation_status", 1),
        ("legacy", "ai_analysis_json", "updated_at", "", 0),
    )
    for variant, payload_key, generated_key, status_key, priority in variants:
        status = str(row.get(status_key) or "").casefold() if status_key else "complete"
        if status and not status.startswith("complete"):
            continue
        payload = _json_mapping(row.get(payload_key))
        generated_at = _analysis_timestamp(row.get(generated_key) or row.get("updated_at"))
        if payload is None or generated_at is None:
            continue
        candidates.append((generated_at, priority, variant, payload))
    if not candidates:
        return None
    generated_at, _, variant, payload = max(candidates, key=lambda item: (item[0], item[1]))
    iso_year, iso_week, _ = generated_at.isocalendar()
    return {
        "source": "내부 심층분석",
        "tool": "agent2_deep_analysis",
        "canonical_table": "cache_deep_analysis_ai_analysis",
        "brand": str(row.get("brand") or "").strip(),
        "market_id": market_id or None,
        "subject_grain": "brand",
        "analysis_variant": variant,
        "analysis": payload,
        "generated_at": generated_at.isoformat(),
        "freshness_label": f"내부 심층분석 · {iso_year}-W{iso_week:02d} 생성분",
    }


def _json_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return dict(payload) if isinstance(payload, Mapping) else None


def _analysis_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _clinical_lossless_external_call(
    query: str,
    concept: ClinicalTrialConcept,
    *,
    timeout_s: float,
):
    from jw_chat_agent_poc.tools.external.client import ExternalCall
    from jw_chat_agent_poc.tools.external.clinicaltrials_v2 import (
        CLINICALTRIALS_V2_STUDIES_URL,
        ClinicalTrialsV2Client,
    )

    compiled = compile_clinical_query(concept)
    try:
        result = ClinicalTrialsV2Client(timeout_s=timeout_s).search(compiled)
    except Exception as exc:  # noqa: BLE001 - external failures are typed evidence
        return ExternalCall(
            tool="clinicaltrials_v2_lossless_search",
            source="clinicaltrials_api_v2",
            status="error",
            summary_text="ClinicalTrials.gov API v2 전건 조회에 실패했습니다.",
            render_data={
                "request": dict(compiled.parameters),
                "payload": {"studies": []},
                "query_manifest": {
                    "query_id": compiled.query_id,
                    "compiled_expression": compiled.expression,
                    "source_queries": list(concept.source_queries or (query,)),
                    "pagination_complete": False,
                },
                "coverage": {
                    "total_reported": None,
                    "records_received": 0,
                    "records_unique": 0,
                    "page_count": 0,
                    "pagination_complete": False,
                    "partial_reason": "ClinicalTrials.gov API v2 조회 실패",
                },
                "error_type": type(exc).__name__,
                "external_claim_policy": "fail_closed_error",
            },
            safe_url=CLINICALTRIALS_V2_STUDIES_URL,
        )

    coverage = {
        "total_reported": result.total_reported,
        "records_received": result.records_received,
        "records_unique": result.records_unique,
        "page_count": result.page_count,
        "pagination_complete": result.pagination_complete,
        "partial_reason": result.partial_reason,
    }
    return ExternalCall(
        tool="clinicaltrials_v2_lossless_search",
        source="clinicaltrials_api_v2",
        status="live" if result.records else "no_data",
        summary_text=(
            f"ClinicalTrials.gov API v2에서 {result.records_unique}건을 전건 수집했습니다."
            if result.records
            else "ClinicalTrials.gov API v2 조회 결과가 없습니다."
        ),
        render_data={
            "request": dict(compiled.parameters),
            "payload": {
                "studies": list(result.records),
                "totalCount": result.total_reported,
            },
            "query_manifest": result.query_manifest,
            "coverage": coverage,
            "external_claim_policy": "source_relay_only",
        },
        safe_url=CLINICALTRIALS_V2_STUDIES_URL,
        elapsed_ms=result.elapsed_ms,
    )


def build_source_adapters() -> dict[SourceName, Any]:
    from jw_chat_agent_poc.agent_loop.factory import build_chat_agent_dependencies
    from jw_chat_agent_poc.service.general_view_routing import GeneralRoute, GeneralViewService
    from jw_chat_agent_poc.tools.external.client import ExternalCall
    from jw_chat_agent_poc.tools.external.hira_reimbursement import (
        HiraReimbursementHttpClient,
        ReimbursementLookupService,
        configured_reimbursement_store,
    )

    dependencies = build_chat_agent_dependencies(external_mode="live")
    external = dependencies.external
    general_view = GeneralViewService.from_env(dependencies.resolver)
    reimbursement = ReimbursementLookupService(
        store=configured_reimbursement_store(),
        realtime=HiraReimbursementHttpClient(timeout_s=7),
    )

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

    def ingredient_brand_resolutions(query: str) -> tuple[Any, ...]:
        search_term = _ingredient_search_term(query)
        if search_term is None:
            return ()
        search = external.mfds_permission_search(search_term)
        names = _walk_named_values(search.render_data, {"item_name", "product_name"})
        found: list[Any] = []
        seen: set[str] = set()
        for name in names:
            for hint in _nedrug_product_brand_hints(name):
                try:
                    resolution = dependencies.resolver.resolve(hint, allow_default=False)
                except LookupError:
                    continue
                canonical = str(resolution.canonical_brand)
                if canonical not in seen:
                    seen.add(canonical)
                    found.append(resolution)
                break
        return tuple(found)

    def general_mart(brand: str) -> dict[str, Any] | None:
        result = general_view.answer(
            f"{brand} 일반뷰 매출 점유율 순위 추이",
            compact=False,
            dual=False,
        )
        return result if _mart_payload_ok(result) else None

    def mart(query: str) -> SourceResult:
        started_at = datetime.now(UTC)
        payloads: list[dict[str, Any]] = []
        has_general_payload = False
        route = general_view.route(query)
        if route in {GeneralRoute.GENERAL_ONLY, GeneralRoute.DUAL}:
            result = general_view.answer(
                query,
                compact=False,
                dual=route is GeneralRoute.DUAL,
            )
            if _mart_payload_ok(result):
                payloads.append(result)
                has_general_payload = True

        resolution = resolved(query)
        resolutions = (
            (resolution,)
            if resolution is not None
            else ingredient_brand_resolutions(query)
        )
        layer = dependencies.query_layer
        for item in resolutions:
            market_ids = getattr(item, "market_ids", None)
            if market_ids == ():
                if not has_general_payload:
                    general = general_mart(str(item.canonical_brand))
                    if general is not None:
                        payloads.append(general)
                        has_general_payload = True
                continue
            strategic = (
                _strategic_mart_calls(layer, str(item.canonical_brand), query)
                if layer is not None
                else []
            )
            if _needs_deep_analysis(query):
                deep_analysis = _load_canonical_deep_analysis(
                    str(item.canonical_brand),
                    tuple(str(value) for value in (market_ids or ()) if str(value)),
                )
                if deep_analysis is not None:
                    strategic.append(deep_analysis)
            payloads.extend(strategic)
            if not strategic and not has_general_payload:
                general = general_mart(str(item.canonical_brand))
                if general is not None:
                    payloads.append(general)
                    has_general_payload = True

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
        query_kind = _hira_query_kind(base)
        code = _hira_code(base)
        if query_kind == "patient" and code:
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

        if query_kind == "reimbursement":
            subject = _reimbursement_subject(base)
            subject_resolution = resolved(subject)
            brand = (
                subject_resolution.canonical_brand
                if subject_resolution is not None
                else subject
            )
            lookup = reimbursement.lookup(brand)
            document_lookup = _reimbursement_lookup_metadata(lookup, brand)
            criterion = lookup.data
            if not lookup.ok or criterion is None:
                result = external_calls("hira", query, [])
                return result.model_copy(
                    update={
                        "payload": {
                            **result.payload,
                            "document_lookup": document_lookup,
                        }
                    }
                )
            call = ExternalCall(
                tool="hira_reimbursement_detail",
                source="hira_reimbursement",
                status="ok",
                summary_text=criterion.raw_text,
                render_data=asdict(criterion),
                safe_url=criterion.source_url,
            )
            result = external_calls("hira", query, [call])
            return result.model_copy(
                update={
                    "payload": {
                        **result.payload,
                        "document_lookup": document_lookup,
                    }
                }
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

    def clinicaltrials(
        query: str,
        *,
        concept: ClinicalTrialConcept | None = None,
    ) -> SourceResult:
        nct_id = _nct_id(query)
        if nct_id:
            return external_calls(
                "clinicaltrials", query, [external.clinicaltrials_study_details(nct_id)]
            )
        resolution = resolved(query)
        if concept is not None:
            concepts = (concept,)
        elif resolution is not None and resolution.molecule_en:
            molecules = tuple(dict.fromkeys(str(value) for value in resolution.molecule_en))
            combined = len(molecules) > 1 and bool(
                re.search(
                    r"(?:복합|조합|combination|\band\b|\+|(?:^|\s)(?:및|와|과)(?:\s|$))",
                    query,
                    re.IGNORECASE,
                )
            )
            concepts = (
                (
                    ClinicalTrialConcept(
                        ingredients=molecules,
                        brands=(str(resolution.canonical_brand),),
                        search_area="intervention",
                        match="both",
                        source_queries=(query,),
                    ),
                )
                if combined
                else tuple(
                    ClinicalTrialConcept(
                        ingredients=(molecule,),
                        brands=(str(resolution.canonical_brand),),
                        search_area="intervention",
                        match="any",
                        source_queries=(query,),
                    )
                    for molecule in molecules
                )
            )
        else:
            term, query_type = _clinical_query(query)
            concepts = (
                concept_from_query(
                    query,
                    search_area=query_type,
                    matched_terms=(term,),
                ),
            )
        calls = [
            _clinical_lossless_external_call(
                query,
                concept,
                timeout_s=float(external.timeout_s),
            )
            for concept in concepts
        ]
        return external_calls("clinicaltrials", query, calls)

    def patent(query: str) -> SourceResult:
        base = _base_query(query)
        resolution = resolved(query)
        molecules = (
            tuple(dict.fromkeys(str(value) for value in resolution.molecule_en))
            if resolution is not None and resolution.molecule_en
            else (_ingredient_query(base),)
        )
        kr_calls = [external.mfds_patent(molecule) for molecule in molecules]
        us_calls = [external.mfds_fda_orangebook(molecule) for molecule in molecules]
        news_calls = [
            _v4_web_search(
                external,
                f"{base} 특허 만료 최근 뉴스",
                search_depth="basic",
                topic="news",
            )
        ]
        result = external_calls(
            "patent",
            query,
            [*kr_calls, *us_calls, *news_calls],
        )
        lanes = build_patent_lane_payload(
            kr_calls=tuple(asdict(call) for call in kr_calls),
            us_calls=tuple(asdict(call) for call in us_calls),
            news_calls=tuple(asdict(call) for call in news_calls),
        )
        return result.model_copy(
            update={"payload": {**result.payload, "patent_lanes": lanes}}
        )

    return {
        "mart": mart,
        "nedrug": nedrug,
        "hira": hira,
        "openfda": openfda,
        "clinicaltrials": clinicaltrials,
        "web": lambda query: external_calls(
            "web",
            query,
            [_v4_web_search(external, _base_query(query), search_depth="advanced")],
        ),
        "patent": patent,
    }


def _strategic_mart_calls(layer, brand: str, query: str) -> list[dict[str, Any]]:
    lowered = query.casefold()
    recent_year_match = _RECENT_YEAR_RE.search(query)
    recent_years = int(recent_year_match.group(1)) if recent_year_match else None
    metric_period = "latest" if recent_years else requested_period(query) or "latest"
    relative_history_points = min(60, recent_years * 12 + 1) if recent_years else None
    calls: list[dict[str, Any]] = []
    try:
        scope = layer.market_scope(brand)
    except (LookupError, ValueError):
        return calls
    if _mart_payload_ok(scope):
        calls.append(scope)
    market = _mart_market_id(scope)

    metric_package = (
        ("sales", 60),
        ("share", 10),
        ("rank", 10),
        ("prescription_volume", 60),
    )
    for metric, history_points in metric_package:
        try:
            call = layer.brand_metric(
                brand,
                metric,
                metric_period,
                market=market,
                history_points=relative_history_points or history_points,
            )
            calls.append(
                _clip_recent_year_call(call, recent_years)
                if recent_years
                else call
            )
        except (LookupError, ValueError):
            continue

    top_brands: dict[str, Any] | None = None
    try:
        top_brands = layer.top_brands(brand, limit=8, metric="sales", market=market)
        calls.append(top_brands)
    except (LookupError, ValueError):
        pass

    bundle_period = f"최근 {recent_years}년" if recent_years else metric_period
    bundle = _entity_bundle_call(layer, brand, market, top_brands, bundle_period)
    if bundle is not None:
        calls.append(bundle)

    try:
        cause = layer.cause_card_data(brand, market)
    except (LookupError, ValueError):
        cause = {}
    if cause:
        cause, period_anchor = align_cause_periods(cause)
        calls.append(
            {
                "source": str(scope.get("source") or "내부 데이터마트"),
                "tool": "cause_card_data",
                "summary_text": f"{brand} 시장의 원인분석 분해 데이터를 직접 조회했습니다.",
                "render_data": cause,
                "cause_period_anchor": period_anchor,
            }
        )

    dimensions: list[str] = []
    if "진료과" in lowered:
        dimensions.append("specialty")
    if any(token in lowered for token in ("유통채널", "채널별", "채널")):
        dimensions.append("channel")
    breakdown_metric = (
        "prescription_volume"
        if any(token in lowered for token in ("처방", "판매량", "수량"))
        else "sales"
    )
    trend_requested = any(
        token in lowered
        for token in ("추이", "시계열", "최근 5년", "최근5년", "변해", "변화", "변동")
    )
    for dimension in dimensions:
        try:
            calls.append(
                layer.dimension_breakdown(
                    brand,
                    dimension,
                    market=market,
                    metric=breakdown_metric,
                )
            )
        except (LookupError, ValueError):
            pass
        if not trend_requested:
            continue
        try:
            calls.append(
                layer.query(
                    {
                        "market": market,
                        "metrics": [breakdown_metric],
                        "group_by": [dimension, "period"],
                        "derive": ["trend"],
                        "filters": {"brand": brand},
                        "sort": "period_asc",
                        "limit": 10,
                    },
                    fallback_brand=brand,
                )
            )
        except (LookupError, ValueError):
            pass
    return calls


def _entity_bundle_call(
    layer: Any,
    anchor_brand: str,
    market: str | None,
    top_brands: dict[str, Any] | None,
    period: str = "latest",
) -> dict[str, Any] | None:
    if top_brands is None:
        return None
    render_data = top_brands.get("render_data")
    if not isinstance(render_data, dict):
        return None
    raw_rows = render_data.get("level_top5_trend_series")
    if not isinstance(raw_rows, list):
        return None
    rows = [dict(row) for row in raw_rows if isinstance(row, dict) and row.get("brand")]
    anchor_row = next(
        (row for row in rows if str(row.get("brand")) == anchor_brand),
        {"brand": anchor_brand, "company": "", "rank": None},
    )
    anchor_company = str(anchor_row.get("company") or "").strip()
    family = [
        row
        for row in rows
        if str(row.get("brand")) != anchor_brand
        and anchor_company
        and str(row.get("company") or "").strip() == anchor_company
    ]
    competitors = [
        row
        for row in rows
        if str(row.get("brand")) != anchor_brand and row not in family
    ]
    selected = [anchor_row, *family[:2], *competitors[:5]]
    selected = list({str(row["brand"]): row for row in selected}.values())
    recent_year_match = _RECENT_YEAR_RE.search(period)
    recent_years = int(recent_year_match.group(1)) if recent_year_match else None

    def load(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
        try:
            if period == "latest":
                call = layer.market_member_metric(
                    anchor_brand,
                    str(row["brand"]),
                    market=market,
                    metric="series",
                )
            elif recent_years:
                call = layer.brand_metric(
                    str(row["brand"]),
                    "sales",
                    "latest",
                    market=market,
                    history_points=min(60, recent_years * 12 + 1),
                )
            else:
                call = layer.brand_metric(
                    str(row["brand"]),
                    "sales",
                    period,
                    market=market,
                    history_points=60,
                )
        except (LookupError, ValueError):
            return None
        return row, call

    with ThreadPoolExecutor(max_workers=max(1, len(selected))) as pool:
        loaded = tuple(item for item in pool.map(load, selected) if item is not None)
    if not loaded:
        return None

    period_sets = [
        _member_periods(call)
        for _row, call in loaded
    ]
    period_sets = [periods for periods in period_sets if periods]
    if not period_sets:
        return None
    common_periods = set(period_sets[0])
    for periods in period_sets[1:]:
        common_periods.intersection_update(periods)
    common_periods = _comparison_periods(common_periods, period)
    if not common_periods:
        return None

    members: list[dict[str, Any]] = []
    for row, call in loaded:
        member_brand = str(row["brand"])
        role = (
            "target"
            if member_brand == anchor_brand
            else "family"
            if anchor_company and str(row.get("company") or "").strip() == anchor_company
            else "competitor"
        )
        members.append(
            {
                "brand": member_brand,
                "company": str(row.get("company") or ""),
                "rank": row.get("rank"),
                "role": role,
                "from_period": row.get("from_period"),
                "from_ms_pct": row.get("from_ms_pct"),
                "to_period": row.get("to_period"),
                "to_ms_pct": row.get("to_ms_pct"),
                "share_delta_pctp": row.get("share_delta_pctp"),
                "render_data": _clip_bundle_periods(call.get("render_data"), common_periods),
            }
        )
    ordered_periods = sorted(common_periods)
    return {
        "source": str(top_brands.get("source") or "내부 데이터마트"),
        "tool": "entity_bundle",
        "summary_text": f"{anchor_brand}와 같은 시장의 패밀리·경쟁 브랜드 시계열을 병렬 조회했습니다.",
        "entity_bundle": {
            "anchor": anchor_brand,
            "market_id": market,
            "requested_period": period,
            "period_start": ordered_periods[0],
            "period_end": ordered_periods[-1],
            "same_period_and_denominator": True,
            "members": members,
        },
    }


def _comparison_periods(periods: set[str], requested: str) -> set[str]:
    recent_year_match = _RECENT_YEAR_RE.search(requested)
    if recent_year_match:
        current_year = current_kst_date().year
        start_year = current_year - int(recent_year_match.group(1))
        return {
            period
            for period in periods
            if start_year <= int(period[:4]) <= current_year
        }
    if requested == "latest":
        return periods

    end_period: str | None = None
    if re.fullmatch(r"20\d{2}", requested):
        matches = sorted(period for period in periods if period.startswith(f"{requested}-"))
        end_period = matches[-1] if matches else None
    else:
        month_match = re.fullmatch(r"(20\d{2})-(0[1-9]|1[0-2])", requested)
        quarter_match = re.fullmatch(r"(20\d{2})-Q([1-4])", requested)
        if month_match is not None:
            end_period = requested
        elif quarter_match is not None:
            year, quarter = map(int, quarter_match.groups())
            end_period = f"{year:04d}-{quarter * 3:02d}"
    if end_period is None or end_period not in periods:
        return set()
    year, month = map(int, end_period.split("-"))
    start_period = f"{year - 1:04d}-{month:02d}"
    if start_period not in periods:
        return set()
    return {start_period, end_period}


def _member_periods(call: dict[str, Any]) -> set[str]:
    render_data = call.get("render_data")
    if not isinstance(render_data, dict):
        return set()
    for key in ("brand_value_series_10pt", "series"):
        series = render_data.get(key)
        if isinstance(series, list):
            return {
                str(item["period"])
                for item in series
                if isinstance(item, dict) and _is_month_period(item.get("period"))
            }
    return set()


def _clip_bundle_periods(value: Any, periods: set[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: _clip_bundle_periods(item, periods)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _clip_bundle_periods(item, periods)
            for item in value
            if not isinstance(item, dict)
            or not _is_month_period(item.get("period"))
            or str(item["period"]) in periods
        ]
    return value


def _clip_recent_year_call(call: dict[str, Any], years: int) -> dict[str, Any]:
    render_data = call.get("render_data")
    if not isinstance(render_data, dict):
        return call
    today = current_kst_date()
    start_year = today.year - years
    periods = {
        period
        for period in _nested_month_periods(render_data)
        if start_year <= int(period[:4]) <= today.year
    }
    if not periods:
        return call
    return {
        **call,
        "render_data": _clip_bundle_periods(render_data, periods),
    }


def _nested_month_periods(value: Any) -> set[str]:
    if isinstance(value, dict):
        period = value.get("period")
        periods = {str(period)} if _is_month_period(period) else set()
        for item in value.values():
            periods.update(_nested_month_periods(item))
        return periods
    if isinstance(value, list):
        periods: set[str] = set()
        for item in value:
            periods.update(_nested_month_periods(item))
        return periods
    return set()


def align_cause_periods(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str] | None]:
    """Annotate the shared comparison range without shrinking source tables."""

    aligned = copy.deepcopy(payload)
    period_sets: list[set[str]] = []

    def collect(value: Any, parent_key: str = "") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                collect(item, str(key))
            return
        if not isinstance(value, list):
            return
        if _is_cause_series_key(parent_key):
            periods = {
                str(item.get("period") or "").strip()
                for item in value
                if isinstance(item, dict)
                and _is_month_period(item.get("period"))
            }
            if len(periods) >= 2:
                period_sets.append(periods)
        for item in value:
            collect(item, parent_key)

    collect(aligned)
    if not period_sets:
        return aligned, None
    common_periods = set(period_sets[0])
    for periods in period_sets[1:]:
        common_periods.intersection_update(periods)
    if len(common_periods) < 2:
        return aligned, None
    period_start = min(common_periods)
    period_end = max(common_periods)
    anchor = {"period_start": period_start, "period_end": period_end}
    aligned["cause_period_anchor"] = anchor
    return aligned, anchor


def _is_cause_series_key(key: str) -> bool:
    lowered = key.casefold()
    return lowered == "series" or lowered.endswith("_series") or "series_" in lowered


def _is_month_period(value: Any) -> bool:
    return bool(re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2])", str(value or "").strip()))
    if last.get("value_억원") is not None:
        row["value_recent_억원"] = last["value_억원"]
        if first.get("value_억원") is not None:
            row["value_delta_억원"] = _decimal_delta(
                first["value_억원"],
                last["value_억원"],
                Decimal("0.01"),
            )


def _decimal_delta(start: Any, end: Any, quantum: Decimal) -> float:
    return float(
        (Decimal(str(end)) - Decimal(str(start))).quantize(
            quantum,
            rounding=ROUND_HALF_UP,
        )
    )


def _mart_market_id(scope: dict[str, Any]) -> str | None:
    render_data = scope.get("render_data")
    if not isinstance(render_data, dict):
        return None
    value = render_data.get("market_id") or render_data.get("market")
    return str(value).strip() if value not in (None, "") else None


def _clear_v4_gate_web_cache() -> None:
    with _V4_GATE_WEB_CACHE_LOCK:
        _V4_GATE_WEB_CACHE.clear()


def _v4_web_search(
    external: Any,
    query: str,
    *,
    search_depth: str,
    topic: str = "general",
):
    provider = os.environ.get("WEB_SEARCH_PROVIDER", "tavily").strip().casefold()
    if provider != "tavily_mcp":
        return external.web_search(query, topic=topic)

    cache_enabled = all(
        os.environ.get(name, "").strip().casefold() in {"1", "true", "yes", "on"}
        for name in ("CHAT_V4_GATE_WEB_CACHE_ENABLED", "CHAT_V4_GATE_RUNNER")
    )
    cache_key = (" ".join(query.split()), search_depth, topic)
    if cache_enabled:
        with _V4_GATE_WEB_CACHE_LOCK:
            cached = _V4_GATE_WEB_CACHE.get(cache_key)
        if cached is not None:
            return replace(
                cached,
                render_data={
                    **cached.render_data,
                    "v4_tavily_policy": {
                        **cached.render_data.get("v4_tavily_policy", {}),
                        "gate_cache_hit": True,
                    },
                },
            )

    attempts = 1
    call = _v4_tavily_mcp_request(
        external,
        query,
        search_depth=search_depth,
        topic=topic,
    )
    if _v4_transport_retryable(call):
        attempts = 2
        call = _v4_tavily_mcp_request(
            external,
            query,
            search_depth=search_depth,
            topic=topic,
        )
    call = replace(
        call,
        render_data={
            **call.render_data,
            "v4_tavily_policy": {
                "attempts": attempts,
                "search_depth": search_depth,
                "gate_cache_hit": False,
                "retry_scope": "transport_only",
            },
        },
    )
    if cache_enabled and call.status in {"live", "no_data"}:
        with _V4_GATE_WEB_CACHE_LOCK:
            _V4_GATE_WEB_CACHE[cache_key] = call
    return call


def _v4_transport_retryable(call: Any) -> bool:
    if str(getattr(call, "status", "")).casefold() != "error":
        return False
    render_data = getattr(call, "render_data", {})
    if not isinstance(render_data, dict):
        return False
    if str(render_data.get("error_type") or "").casefold() == "parse_failure":
        return False
    detail = " ".join(
        str(value)
        for value in (
            render_data.get("error"),
            render_data.get("message"),
            getattr(call, "summary_text", ""),
        )
        if value
    )
    return bool(_TRANSPORT_ERROR_RE.search(detail))


def _v4_tavily_mcp_request(
    external: Any,
    query: str,
    *,
    search_depth: str,
    topic: str,
):
    from jw_chat_agent_poc.tools.external.client import (
        TAVILY_MCP_SOURCE,
        _mcp_tool_spec,
        _tavily_mcp_web_call,
        _web_error,
    )
    from jw_chat_agent_poc.tools.external.mcp_client import (
        McpClientError,
        McpJsonClient,
        mcp_attempt_limit,
    )

    params = {"query": query, "max_results": "5", "topic": topic}
    spec = _mcp_tool_spec("tavily_mcp_search", params)
    spec["arguments"]["search_depth"] = search_depth
    url = external._mcp_url(spec["resource_id"], spec["source"])
    started = time.monotonic()
    try:
        with mcp_attempt_limit(1):
            result = McpJsonClient(
                url,
                timeout_s=int(getattr(external, "timeout_s", 12)),
            ).call_tool(spec["mcp_tool"], spec["arguments"])
    except McpClientError as exc:
        elapsed = round((time.monotonic() - started) * 1000, 1)
        return _web_error(TAVILY_MCP_SOURCE, query, exc, elapsed)
    elapsed = round((time.monotonic() - started) * 1000, 1)
    return _tavily_mcp_web_call(params, result, elapsed)


def _mart_payload_ok(payload: dict[str, Any]) -> bool:
    status = str(payload.get("status") or "ok").casefold()
    render_data = payload.get("render_data")
    if status in {"error", "no_data", "unsupported"}:
        return False
    if isinstance(render_data, dict) and bool(render_data):
        return True
    tool_calls = payload.get("tool_calls")
    return isinstance(tool_calls, list) and any(
        isinstance(call, dict) and _mart_payload_ok(call)
        for call in tool_calls
    )


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
