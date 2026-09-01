from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import requests

from jw_chat_agent_poc.tools.external.mcp_client import (
    MCP_FIRST_ATTEMPT_TIMEOUT_S,
    McpClientError,
    McpJsonClient,
    McpToolResult,
)
from jw_chat_agent_poc.tools.external.telemetry import (
    emit_external_call_telemetry,
    failure_class_from_exception,
)

GENOS_MCP_GATEWAY_BASE_ENV = "GENOS_MCP_GATEWAY_BASE"
OPENFDA_MCP_RESOURCE_ENV = "OPENFDA_MCP_RESOURCE_ID"
NEDRUG_MCP_RESOURCE_ENV = "NEDRUG_MCP_RESOURCE_ID"
HIRA_MCP_RESOURCE_ENV = "HIRA_MCP_RESOURCE_ID"
CLINICAL_TRIALS_MCP_RESOURCE_ENV = "CLINICAL_TRIALS_MCP_RESOURCE_ID"
TAVILY_MCP_RESOURCE_ENV = "TAVILY_MCP_RESOURCE_ID"
OPENFDA_MCP_URL_ENV = "OPENFDA_MCP_URL"
NEDRUG_MCP_URL_ENV = "NEDRUG_MCP_URL"
HIRA_MCP_URL_ENV = "HIRA_MCP_URL"
CLINICAL_TRIALS_MCP_URL_ENV = "CLINICAL_TRIALS_MCP_URL"
TAVILY_MCP_URL_ENV = "TAVILY_MCP_URL"
WEB_SEARCH_PROVIDER_ENV = "WEB_SEARCH_PROVIDER"
TAVILY_API_KEY_ENV = "TAVILY_API_KEY"
SERPER_API_KEY_ENV = "SERPER_API_KEY"
BRAVE_SEARCH_API_KEY_ENV = "BRAVE_SEARCH_API_KEY"
MFDS_PATENT_QUERY_ALIASES = {
    "pitavastatin": "리바로",
    "ezetimibe": "리바로젯",
}
MFDS_PATENT_INGREDIENT_ALIASES = {
    "emicizumab": "에미시주맙",
    "pitavastatin": "Pitavastatin",
    "pitavastatin calcium": "Pitavastatin",
    "피타바스타틴": "Pitavastatin",
    "피타바스타틴칼슘": "Pitavastatin",
}
HIRA_DISEASE_SOURCE = "hira_disease"
HIRA_PROCEDURE_SOURCE = "hira_procedure"
WEB_SEARCH_SOURCE = "web_search"
WEB_SEARCH_MAX_RESULTS = 5
TAVILY_TIMEOUT_CAP_S = 5
NEDRUG_READ_TIMEOUT_ENV = "NEDRUG_READ_TIMEOUT_S"
HIRA_READ_TIMEOUT_ENV = "HIRA_READ_TIMEOUT_S"
NEDRUG_DEFAULT_READ_TIMEOUT_S = 19.4
HIRA_DEFAULT_READ_TIMEOUT_S = 24.0
HIRA_RESOURCE_POOL_BACKOFF_ENV = "HIRA_RESOURCE_POOL_BACKOFF_S"
HIRA_RESOURCE_POOL_MAX_BACKOFF_ENV = "HIRA_RESOURCE_POOL_MAX_BACKOFF_S"
HIRA_RESOURCE_POOL_DEFAULT_BACKOFF_S = 15.0
HIRA_RESOURCE_POOL_DEFAULT_MAX_BACKOFF_S = 120.0
_HIRA_RESOURCE_POOL_PUBLIC_MESSAGE = "HIRA 원천 일시 장애 (호출량/서버 부하)"
_HIRA_RESOURCE_POOL_RE = re.compile(
    r"(?:ResourceLimitException|No resources currently available in pool\s+dsOpenapi)",
    re.IGNORECASE,
)
MFDS_PATENT_MAX_RESULTS_ENV = "MFDS_PATENT_MAX_RESULTS"
MFDS_PATENT_DEFAULT_MAX_RESULTS = 500
MFDS_PATENT_PROVIDER_MAX_RESULTS = 500
DEFAULT_MCP_GATEWAY_BASE = "http://llmops-gateway-api-service:8080"
OPENFDA_MCP_DEFAULT_RESOURCE = "184"
NEDRUG_MCP_DEFAULT_RESOURCE = "250"
HIRA_MCP_DEFAULT_RESOURCE = "253"
CLINICAL_TRIALS_MCP_DEFAULT_RESOURCE = "169"
TAVILY_MCP_DEFAULT_RESOURCE = "214"
OPENFDA_MCP_SOURCE = "openfda_mcp"
NEDRUG_MCP_SOURCE = "nedrug_mcp"
HIRA_MCP_SOURCE = "hira_mcp"
CLINICAL_TRIALS_MCP_SOURCE = "clinicaltrials_mcp"
TAVILY_MCP_SOURCE = "tavily_mcp"
_PERSONAL_BLOG_HOSTS = frozenset(
    {"blog.naver.com", "m.blog.naver.com", "tistory.com", "brunch.co.kr", "medium.com"}
)
_PUBLICATION_DATE_QUERY_RE = re.compile(
    r"(?:최근|최신|개정|변경|뉴스|이슈)",
    re.IGNORECASE,
)
_PUBLICATION_DATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?is)(?:article:published_time|datePublished|datePublishedUTC|pubdate)"
        r"[^>\n]{0,240}?(20\d{2}-\d{2}-\d{2}(?:[T ][^\"'<\s]+)?)"
    ),
    re.compile(
        r"(?is)(20\d{2}-\d{2}-\d{2}(?:[T ][^\"'<\s]+)?)"
        r"[^>\n]{0,240}?(?:article:published_time|datePublished|datePublishedUTC|pubdate)"
    ),
    re.compile(r"(?is)<time\b[^>]*\bdatetime=[\"'](20\d{2}-\d{2}-\d{2}[^\"']*)[\"']"),
    re.compile(
        r"(?is)(?:게시일|등록일|작성일|발행일|입력일|최종\s*수정일)"
        r"[^0-9]{0,24}(20\d{2}[./-]\d{1,2}[./-]\d{1,2})"
    ),
)
_PUBLICATION_METADATA_MAX_BYTES = 256 * 1024
MCP_DIRECT_URL_ENV_BY_SOURCE = {
    OPENFDA_MCP_SOURCE: OPENFDA_MCP_URL_ENV,
    NEDRUG_MCP_SOURCE: NEDRUG_MCP_URL_ENV,
    HIRA_MCP_SOURCE: HIRA_MCP_URL_ENV,
    CLINICAL_TRIALS_MCP_SOURCE: CLINICAL_TRIALS_MCP_URL_ENV,
    TAVILY_MCP_SOURCE: TAVILY_MCP_URL_ENV,
}
MCP_SCHEMA_GUARDED_TOOLS = frozenset(
    {
        "search_studies",
        "search_drug_labels",
        "search_drug_adverse_events",
    }
)


def resolve_patent_ingredient_query(text: str) -> str | None:
    normalized = " ".join(str(text or "").casefold().replace("-", " ").split())
    for alias, ingredient in MFDS_PATENT_INGREDIENT_ALIASES.items():
        alias_norm = " ".join(alias.casefold().replace("-", " ").split())
        pattern = rf"(?<![0-9a-z가-힣]){re.escape(alias_norm)}(?![0-9a-z가-힣])"
        if re.search(pattern, normalized):
            return ingredient
    return None


@dataclass(frozen=True)
class ExternalCall:
    tool: str
    source: str
    status: str
    summary_text: str
    render_data: dict[str, Any]
    safe_url: str | None = None
    elapsed_ms: float | None = None


class _HiraResourcePoolCircuit:
    def __init__(
        self,
        *,
        base_backoff_s: float,
        max_backoff_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base_backoff_s = max(0.1, base_backoff_s)
        self._max_backoff_s = max(self._base_backoff_s, max_backoff_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._opened_until = 0.0
        self._half_open_probe = False

    def before_call(self) -> tuple[bool, float]:
        with self._lock:
            now = self._clock()
            if now < self._opened_until:
                return False, round(self._opened_until - now, 3)
            if self._consecutive_failures:
                if self._half_open_probe:
                    return False, 0.0
                self._half_open_probe = True
            return True, 0.0

    def observe(self, call: ExternalCall) -> None:
        with self._lock:
            if _is_hira_resource_pool_failure(call):
                self._consecutive_failures += 1
                delay = min(
                    self._max_backoff_s,
                    self._base_backoff_s * (2 ** (self._consecutive_failures - 1)),
                )
                self._opened_until = self._clock() + delay
                self._half_open_probe = False
                return
            if call.status in {"live", "no_data"}:
                self._consecutive_failures = 0
                self._opened_until = 0.0
                self._half_open_probe = False
                return
            if self._half_open_probe:
                self._opened_until = self._clock() + self._base_backoff_s
                self._half_open_probe = False


def _is_hira_resource_pool_failure(call: ExternalCall) -> bool:
    render_data = call.render_data if isinstance(call.render_data, dict) else {}
    if str(render_data.get("error_type") or "").casefold() == "provider_resource_limit":
        return True
    return bool(
        _HIRA_RESOURCE_POOL_RE.search(
            " ".join(
                str(value)
                for value in (
                    render_data.get("error"),
                    render_data.get("message"),
                    call.summary_text,
                )
                if value
            )
        )
    )


class ExternalApiClient:
    def __init__(self, mode: str = "fixture", fixture_path: Path | None = None, timeout_s: int = 12) -> None:
        self.mode = mode
        self.timeout_s = timeout_s
        path = fixture_path or Path(__file__).resolve().parents[2] / "fixtures" / "external_api_fixtures.json"
        self.fixtures = json.loads(path.read_text(encoding="utf-8"))
        self.mcp_gateway_base = os.environ.get(GENOS_MCP_GATEWAY_BASE_ENV, DEFAULT_MCP_GATEWAY_BASE).rstrip("/")

    @staticmethod
    def redact_url(url: str) -> str:
        if "serviceKey=" not in url:
            return url
        prefix, rest = url.split("serviceKey=", 1)
        if "&" in rest:
            return f"{prefix}serviceKey=<redacted>&{rest.split('&', 1)[1]}"
        return f"{prefix}serviceKey=<redacted>"

    def mfds_permission_search(self, brand: str) -> ExternalCall:
        return self._fixture_or_live("mfds_permission_search", {"brand": brand})

    def mfds_permission_detail(self, item_seq: str) -> ExternalCall:
        return self._fixture_or_live("mfds_permission_detail", {"item_seq": item_seq})

    def mfds_composition(self, brand: str) -> ExternalCall:
        return self._fixture_or_live("mfds_composition", {"brand": brand})

    def mfds_main_ingredient(self, brand: str) -> ExternalCall:
        if self.mode != "live":
            return self.mfds_permission_search(brand)
        return self._live_mcp_call("mfds_main_ingredient", {"brand": brand})

    def mfds_easy_drug(self, brand: str) -> ExternalCall:
        return self._fixture_or_live("mfds_easy_drug", {"brand": brand})

    def mfds_clinical_trial_kr(
        self,
        keyword: str,
        *,
        query_type: Literal["intervention", "condition"] = "intervention",
    ) -> ExternalCall:
        params = {"query.condition": keyword} if query_type == "condition" else {"keyword": keyword}
        return self._fixture_or_live("mfds_clinical_trial_kr", params)

    def clinicaltrials_v2_search(
        self,
        query_intr: str,
        *,
        query_type: Literal["intervention", "condition"] = "intervention",
    ) -> ExternalCall:
        key = "query.condition" if query_type == "condition" else "query.intr"
        return self._fixture_or_live("clinicaltrials_v2_search", {key: query_intr})

    def clinicaltrials_study_details(self, nct_id: str) -> ExternalCall:
        normalized = nct_id.upper()
        call = self._fixture_or_live(
            "clinicaltrials_study_details",
            {"nct_id": normalized},
        )
        if self.mode != "live":
            return call
        return _enrich_clinicaltrials_detail_from_official_api(
            call,
            normalized,
            timeout_s=min(self.timeout_s, 8),
        )

    def openfda_label_search(
        self,
        substance_name: str,
        *,
        evidence_type: str = "label",
    ) -> ExternalCall:
        query = f'openfda.substance_name:"{substance_name.upper()}"'
        return self._fixture_or_live(
            "openfda_label_search",
            {"search": query, "evidence_type": evidence_type},
        )

    def openfda_combo_label_search(self, substance_names: tuple[str, ...]) -> ExternalCall:
        query = " AND ".join(f'openfda.substance_name:"{name.upper()}"' for name in substance_names)
        call = self._fixture_or_live("openfda_label_search", {"search": query})
        return ExternalCall(
            tool="openfda_combo_label_search",
            source=call.source,
            status=call.status,
            summary_text=(
                f"openFDA label에서 {', '.join(substance_names)} 두 성분이 모두 포함된 복합제 라벨을 우선 확인했습니다. "
                "없으면 성분별 라벨은 참고용입니다."
            ),
            render_data={**call.render_data, "match_scope": "combo_substance_and"},
            safe_url=call.safe_url,
            elapsed_ms=call.elapsed_ms,
        )


    def mfds_patent(
        self,
        ingredient_en: str,
        *,
        item_name: str | None = None,
    ) -> ExternalCall:
        resolved_item_name = item_name or MFDS_PATENT_QUERY_ALIASES.get(ingredient_en.lower())
        resolved_ingredient = MFDS_PATENT_INGREDIENT_ALIASES.get(
            ingredient_en.casefold(),
            ingredient_en,
        )
        params: dict[str, str] = {}
        if resolved_ingredient:
            params["ingr_name"] = resolved_ingredient
        if resolved_item_name:
            params["item_name"] = resolved_item_name
        params["limit"] = str(_mfds_patent_result_limit())
        return self._fixture_or_live("mfds_patent", params, xml=True)

    def mfds_fda_orangebook(self, ingredient_en: str) -> ExternalCall:
        ingredients = re.split(r"\s+AND\s+", ingredient_en, flags=re.IGNORECASE)
        query = " AND ".join(ingredient.strip().title() for ingredient in ingredients)
        return self._fixture_or_live("mfds_fda_orangebook", {"ingr_name": query}, xml=True)

    def hira_disease_name_code(self, sick_cd: str, *, sick_type: str | None = None) -> ExternalCall:
        disease_type = "SICK_CD" if is_hira_disease_code(sick_cd) else "SICK_NM"
        if self.mode != "live":
            return _fixture_hira_disease_name_code(sick_cd, disease_type, self.fixtures["hira_disease_name_code"])
        params = {"sickCd": sick_cd, "searchText": sick_cd, "diseaseType": disease_type}
        if sick_type is not None:
            params["sickType"] = sick_type
        call = self._fixture_or_live(
            "hira_disease_name_code",
            params,
            xml=True,
        )
        return self._with_source(call, HIRA_DISEASE_SOURCE)

    def hira_disease_hospitalization_outpatient_stats(self, sick_cd: str, year: str = "2024") -> ExternalCall:
        call = self._fixture_or_live(
            "hira_disease_hospitalization_outpatient_stats",
            {"sickCd": sick_cd, "year": year},
            xml=True,
        )
        return _aggregate_hira_patient_type_sexes(self._with_source(call, HIRA_DISEASE_SOURCE))

    def hira_disease_gender_age_stats(self, sick_cd: str, year: str = "2024") -> ExternalCall:
        call = self._fixture_or_live(
            "hira_disease_gender_age_stats",
            {"sickCd": sick_cd, "year": year},
            xml=True,
        )
        return self._with_source(call, HIRA_DISEASE_SOURCE)

    def hira_disease_institution_class_stats(self, sick_cd: str, year: str = "2024") -> ExternalCall:
        call = self._fixture_or_live(
            "hira_disease_institution_class_stats",
            {"sickCd": sick_cd, "year": year},
            xml=True,
        )
        return self._with_source(call, HIRA_DISEASE_SOURCE)

    def hira_disease_area_stats(self, sick_cd: str, year: str = "2024") -> ExternalCall:
        call = self._fixture_or_live("hira_disease_area_stats", {"sickCd": sick_cd, "year": year}, xml=True)
        return self._with_source(call, HIRA_DISEASE_SOURCE)

    def hira_procedure_gender_ipat_opat_stats(self, st5_cd: str, year: str = "2024", std_type: str = "1") -> ExternalCall:
        call = self._fixture_or_live(
            "hira_procedure_gender_ipat_opat_stats",
            {"st5Cd": st5_cd, "year": year, "stdType": std_type},
            xml=True,
        )
        return self._with_source(call, HIRA_PROCEDURE_SOURCE)

    def hira_procedure_gender_age_stats(self, st5_cd: str, year: str = "2024", std_type: str = "1") -> ExternalCall:
        call = self._fixture_or_live(
            "hira_procedure_gender_age_stats",
            {"st5Cd": st5_cd, "year": year, "stdType": std_type},
            xml=True,
        )
        return self._with_source(call, HIRA_PROCEDURE_SOURCE)

    def hira_procedure_institution_class_stats(self, st5_cd: str, year: str = "2024", std_type: str = "1") -> ExternalCall:
        call = self._fixture_or_live(
            "hira_procedure_institution_class_stats",
            {"st5Cd": st5_cd, "year": year, "stdType": std_type},
            xml=True,
        )
        return self._with_source(call, HIRA_PROCEDURE_SOURCE)

    def hira_procedure_area_stats(self, st5_cd: str, year: str = "2024", std_type: str = "1") -> ExternalCall:
        call = self._fixture_or_live(
            "hira_procedure_area_stats",
            {"st5Cd": st5_cd, "year": year, "stdType": std_type},
            xml=True,
        )
        return self._with_source(call, HIRA_PROCEDURE_SOURCE)

    def web_search(
        self,
        query: str,
        max_results: int = 5,
        *,
        topic: Literal["general", "news"] = "general",
    ) -> ExternalCall:
        max_results = _bounded_web_results(max_results)
        if self.mode != "live":
            call = self._fixture_or_live(
                "web_search",
                {"query": query, "max_results": str(max_results), "topic": topic},
            )
            return self._with_source(call, WEB_SEARCH_SOURCE)
        return self._live_web_search(query, max_results=max_results, topic=topic)

    @staticmethod
    def _with_source(call: ExternalCall, source: str) -> ExternalCall:
        return ExternalCall(
            tool=call.tool,
            source=source,
            status=call.status,
            summary_text=call.summary_text,
            render_data=call.render_data,
            safe_url=call.safe_url,
            elapsed_ms=call.elapsed_ms,
        )

    def _fixture_or_live(self, tool: str, params: dict[str, str], xml: bool = False) -> ExternalCall:
        if self.mode != "live":
            data = self.fixtures[tool]
            return ExternalCall(
                tool=tool,
                source="external_api",
                status="fixture",
                summary_text=data["summary_text"],
                render_data={**data["render_data"], "request": params},
                safe_url=data.get("safe_url"),
                elapsed_ms=0.0,
            )
        return self._live_mcp_call(tool, params)

    def _live_mcp_call(self, tool: str, params: dict[str, str]) -> ExternalCall:
        spec = _mcp_tool_spec(tool, params)
        url = self._mcp_url(spec["resource_id"], spec["source"])
        safe_url = self.redact_url(url)
        start = time.monotonic()
        if spec["source"] == HIRA_MCP_SOURCE:
            allowed, retry_after_s = _HIRA_RESOURCE_POOL_CIRCUIT.before_call()
            if not allowed:
                call = _mcp_failed_call(
                    tool,
                    spec["source"],
                    params,
                    spec["mcp_tool"],
                    "HIRA provider resource-pool circuit is open",
                    safe_url,
                    0.0,
                    error_type="provider_resource_limit",
                    public_message=_HIRA_RESOURCE_POOL_PUBLIC_MESSAGE,
                    extra_render_data={
                        "circuit_state": "open",
                        "retry_after_ms": round(retry_after_s * 1000),
                    },
                )
                emit_external_call_telemetry(
                    primary_provider=spec["source"],
                    question=json.dumps(
                        params,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    domain_source="MCP",
                    cache_status="not_applicable",
                    call=call,
                )
                return call
        try:
            read_timeout_s = mcp_read_timeout_s(spec["source"], fallback=self.timeout_s)
            if spec["source"] in {NEDRUG_MCP_SOURCE, HIRA_MCP_SOURCE}:
                client = McpJsonClient(
                    url,
                    timeout_s=read_timeout_s,
                    connect_timeout_s=float(MCP_FIRST_ATTEMPT_TIMEOUT_S),
                    first_attempt_timeout_s=read_timeout_s,
                )
            else:
                client = McpJsonClient(url, timeout_s=read_timeout_s)
            if spec["mcp_tool"] in MCP_SCHEMA_GUARDED_TOOLS:
                result = client.call_tool_checked(spec["mcp_tool"], spec["arguments"])
            else:
                result = client.call_tool(spec["mcp_tool"], spec["arguments"])
        except McpClientError as exc:
            elapsed = round((time.monotonic() - start) * 1000, 1)
            safe_reason = self.redact_url(exc.message)
            if tool == "tavily_mcp_search":
                return _web_error(
                    TAVILY_MCP_SOURCE,
                    params.get("query", ""),
                    McpClientError(safe_reason),
                    elapsed,
                )
            if tool in {"clinicaltrials_v2_search", "clinicaltrials_study_details"}:
                call = _clinicaltrials_failed_call(params, spec["mcp_tool"], safe_reason, safe_url, elapsed)
            else:
                call = _mcp_failed_call(tool, spec["source"], params, spec["mcp_tool"], safe_reason, safe_url, elapsed)
        else:
            elapsed = round((time.monotonic() - start) * 1000, 1)
            call = _mcp_external_call(tool, spec["source"], params, spec["mcp_tool"], result, safe_url, elapsed)
        if spec["source"] == HIRA_MCP_SOURCE:
            _HIRA_RESOURCE_POOL_CIRCUIT.observe(call)
        if tool != "tavily_mcp_search":
            emit_external_call_telemetry(
                primary_provider=spec["source"],
                question=json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                domain_source="MCP",
                cache_status="not_applicable",
                call=call,
            )
        return call

    def _mcp_url(self, resource_id: str, source: str | None = None) -> str:
        direct_env = MCP_DIRECT_URL_ENV_BY_SOURCE.get(source or "")
        if direct_env:
            direct_url = os.environ.get(direct_env, "").strip()
            if direct_url:
                return direct_url.rstrip("/")
        return f"{self.mcp_gateway_base}/mcp/{resource_id}/mcp"

    def _live_web_search(
        self,
        query: str,
        max_results: int = 5,
        *,
        topic: Literal["general", "news"] = "general",
    ) -> ExternalCall:
        provider = os.environ.get(WEB_SEARCH_PROVIDER_ENV, "tavily").strip().lower()
        if provider == "tavily":
            call = self._live_tavily_search(query, max_results, topic=topic)
        elif provider == TAVILY_MCP_SOURCE:
            call = self._live_mcp_call(
                "tavily_mcp_search",
                {
                    "query": query,
                    "max_results": str(max_results),
                    "topic": topic,
                },
            )
        elif provider == "serper":
            call = self._live_serper_search(query, max_results)
        elif provider == "brave":
            call = self._live_brave_search(query, max_results)
        else:
            call = ExternalCall(
                tool="web_search",
                source=WEB_SEARCH_SOURCE,
                status="unsupported",
                summary_text=f"지원하지 않는 web search provider: {provider}",
                render_data={"query": query, "provider": provider, "items": [], "external_claim_policy": "web_results_unverified"},
                elapsed_ms=0.0,
            )
        emit_external_call_telemetry(
            primary_provider=provider,
            question=query,
            domain_source="web",
            cache_status="not_applicable",
            call=call,
            fallback_blocked=call.status in {"unsupported", "missing_key"},
        )
        return call

    def _live_tavily_search(
        self,
        query: str,
        max_results: int,
        *,
        topic: Literal["general", "news"],
    ) -> ExternalCall:
        key = os.environ.get(TAVILY_API_KEY_ENV)
        if not key:
            return _missing_web_key("tavily", TAVILY_API_KEY_ENV, query)
        max_results = _bounded_web_results(max_results)
        start = time.monotonic()
        try:
            response = requests.post(
                "https://api.tavily.com/search",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "query": query,
                    "max_results": max_results,
                    "search_depth": "basic",
                    "include_answer": False,
                    "topic": topic,
                },
                timeout=min(self.timeout_s, TAVILY_TIMEOUT_CAP_S),
            )
            elapsed = round((time.monotonic() - start) * 1000, 1)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            elapsed = round((time.monotonic() - start) * 1000, 1)
            return _web_error("tavily", query, exc, elapsed)
        items = [
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "snippet": item.get("content") or item.get("snippet"),
                "published_at": item.get("published_date") or item.get("date"),
                "published_date": item.get("published_date") or item.get("date"),
            }
            for item in payload.get("results", [])[:max_results]
            if isinstance(item, dict)
        ]
        return _web_call("tavily", query, items, elapsed)

    def _live_serper_search(self, query: str, max_results: int) -> ExternalCall:
        key = os.environ.get(SERPER_API_KEY_ENV)
        if not key:
            return _missing_web_key("serper", SERPER_API_KEY_ENV, query)
        start = time.monotonic()
        try:
            response = requests.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": key, "Content-Type": "application/json"},
                json={"q": query, "num": max(1, min(max_results, 10)), "gl": "kr", "hl": "ko"},
                timeout=min(self.timeout_s, 10),
            )
            elapsed = round((time.monotonic() - start) * 1000, 1)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            elapsed = round((time.monotonic() - start) * 1000, 1)
            return _web_error("serper", query, exc, elapsed)
        items = [
            {
                "title": item.get("title"),
                "url": item.get("link"),
                "snippet": item.get("snippet"),
                "published_at": item.get("date"),
                "published_date": item.get("date"),
            }
            for item in payload.get("organic", [])[:max_results]
            if isinstance(item, dict)
        ]
        return _web_call("serper", query, items, elapsed)

    def _live_brave_search(self, query: str, max_results: int) -> ExternalCall:
        key = os.environ.get(BRAVE_SEARCH_API_KEY_ENV)
        if not key:
            return _missing_web_key("brave", BRAVE_SEARCH_API_KEY_ENV, query)
        start = time.monotonic()
        try:
            response = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={"X-Subscription-Token": key, "Accept": "application/json"},
                params={"q": query, "count": max(1, min(max_results, 5)), "country": "KR", "search_lang": "ko"},
                timeout=min(self.timeout_s, 10),
            )
            elapsed = round((time.monotonic() - start) * 1000, 1)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            elapsed = round((time.monotonic() - start) * 1000, 1)
            return _web_error("brave", query, exc, elapsed)
        web = payload.get("web") if isinstance(payload.get("web"), dict) else {}
        items = [
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "snippet": item.get("description"),
                "published_at": item.get("page_age") or item.get("age"),
                "published_date": item.get("page_age") or item.get("age"),
            }
            for item in web.get("results", [])[:max_results]
            if isinstance(item, dict)
        ]
        return _web_call("brave", query, items, elapsed)


def _mcp_tool_spec(tool: str, params: dict[str, str]) -> dict[str, Any]:
    match tool:
        case "tavily_mcp_search":
            max_results = _int_or_none(params.get("max_results", ""))
            return {
                "resource_id": os.environ.get(
                    TAVILY_MCP_RESOURCE_ENV,
                    TAVILY_MCP_DEFAULT_RESOURCE,
                ),
                "source": TAVILY_MCP_SOURCE,
                "mcp_tool": "tavily_search",
                "arguments": {
                    "query": params.get("query", ""),
                    "max_results": _bounded_web_results(
                        max_results
                        if max_results is not None
                        else WEB_SEARCH_MAX_RESULTS
                    ),
                    "search_depth": "advanced",
                    "topic": params.get("topic", "general"),
                },
            }
        case "clinicaltrials_v2_search":
            condition = params.get("query.condition")
            return {
                "resource_id": os.environ.get(CLINICAL_TRIALS_MCP_RESOURCE_ENV, CLINICAL_TRIALS_MCP_DEFAULT_RESOURCE),
                "source": CLINICAL_TRIALS_MCP_SOURCE,
                "mcp_tool": "search_studies",
                "arguments": (
                    {"condition": condition, "pageSize": 5}
                    if condition
                    else {"intervention": params.get("query.intr", ""), "pageSize": 5}
                ),
            }
        case "clinicaltrials_study_details":
            return {
                "resource_id": os.environ.get(CLINICAL_TRIALS_MCP_RESOURCE_ENV, CLINICAL_TRIALS_MCP_DEFAULT_RESOURCE),
                "source": CLINICAL_TRIALS_MCP_SOURCE,
                "mcp_tool": "get_study_details",
                "arguments": {"nctId": params.get("nct_id", "")},
            }
        case "openfda_label_search":
            ingredient = _openfda_active_ingredient(params)
            if params.get("evidence_type") == "adverse_event":
                return {
                    "resource_id": os.environ.get(OPENFDA_MCP_RESOURCE_ENV, OPENFDA_MCP_DEFAULT_RESOURCE),
                    "source": OPENFDA_MCP_SOURCE,
                    "mcp_tool": "search_drug_adverse_events",
                    "arguments": {
                        "generic_name": ingredient,
                        "limit": 5,
                        "sort": "receivedate:desc",
                    },
                }
            return {
                "resource_id": os.environ.get(OPENFDA_MCP_RESOURCE_ENV, OPENFDA_MCP_DEFAULT_RESOURCE),
                "source": OPENFDA_MCP_SOURCE,
                "mcp_tool": "search_drug_labels",
                "arguments": {"generic_name": ingredient, "limit": 5},
            }
        case "mfds_permission_search":
            return _nedrug_spec(tool, "search_drug_permission_list", {"item_name": params.get("brand"), "limit": 10})
        case "mfds_permission_detail":
            return _nedrug_spec(tool, "get_drug_permission_detail", {"item_seq": params.get("item_seq"), "limit": 5})
        case "mfds_composition":
            return _nedrug_spec(tool, "get_drug_main_ingredient", {"prduct": params.get("brand"), "limit": 5})
        case "mfds_main_ingredient":
            return _nedrug_spec(tool, "get_drug_main_ingredient", {"prduct": params.get("brand"), "limit": 10})
        case "mfds_easy_drug":
            return _nedrug_spec(tool, "search_easy_drug_info", {"item_name": params.get("brand"), "limit": 5})
        case "mfds_clinical_trial_kr":
            return _nedrug_spec(
                tool,
                "search_clinical_test_info",
                {
                    "clinic_exam_title": params.get("query.condition"),
                    "goods_name": params.get("keyword"),
                    "limit": 5,
                },
            )
        case "mfds_patent":
            return _nedrug_spec(
                tool,
                "search_korea_drug_patent",
                {
                    "item_name": params.get("item_name"),
                    "ingr_name": params.get("ingr_name"),
                    "limit": _mfds_patent_result_limit(params.get("limit")),
                },
            )
        case "mfds_fda_orangebook":
            return _nedrug_spec(tool, "search_fda_orangebook_patent", {"prt_name": params.get("prt_name"), "ingr_name": params.get("ingr_name"), "limit": 5})
        case "hira_disease_name_code":
            search_text = params.get("searchText") or params.get("sickCd", "")
            disease_type = params.get("diseaseType") or ("SICK_CD" if is_hira_disease_code(search_text) else "SICK_NM")
            request_code = _hira_request_code(search_text)
            return _hira_spec(
                tool,
                "search_disease_code",
                {
                    "search_text": request_code or search_text,
                    "disease_type": disease_type,
                    "sick_type": params.get("sickType") or _hira_sick_type(request_code) or "1",
                    "med_tp": "1",
                    "num_of_rows": 10,
                },
            )
        case "hira_disease_hospitalization_outpatient_stats":
            return _hira_spec(tool, "get_disease_stats_by_patient_type", _hira_disease_args(params))
        case "hira_disease_gender_age_stats":
            return _hira_spec(tool, "get_disease_stats_by_age_gender", _hira_disease_args(params))
        case "hira_disease_institution_class_stats":
            return _hira_spec(tool, "get_disease_stats_by_institution_class", _hira_disease_args(params))
        case "hira_disease_area_stats":
            return _hira_spec(tool, "get_disease_stats_by_region", _hira_disease_args(params))
        case "hira_procedure_gender_ipat_opat_stats":
            return _hira_spec(tool, "get_treatment_stats_by_patient_type", _hira_procedure_args(params))
        case "hira_procedure_gender_age_stats":
            return _hira_spec(tool, "get_treatment_stats_by_age_gender", _hira_procedure_args(params))
        case "hira_procedure_institution_class_stats":
            return _hira_spec(tool, "get_treatment_stats_by_institution_class", _hira_procedure_args(params))
        case "hira_procedure_area_stats":
            return _hira_spec(tool, "get_treatment_stats_by_region", _hira_procedure_args(params))
        case unreachable:
            raise McpClientError(f"No MCP mapping for external tool: {unreachable}")


def mcp_read_timeout_s(source: str, *, fallback: float) -> float:
    if source == NEDRUG_MCP_SOURCE:
        return _positive_env_float(NEDRUG_READ_TIMEOUT_ENV, NEDRUG_DEFAULT_READ_TIMEOUT_S)
    if source == HIRA_MCP_SOURCE:
        return _positive_env_float(HIRA_READ_TIMEOUT_ENV, HIRA_DEFAULT_READ_TIMEOUT_S)
    return float(fallback)


def _positive_env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


_HIRA_RESOURCE_POOL_CIRCUIT = _HiraResourcePoolCircuit(
    base_backoff_s=_positive_env_float(
        HIRA_RESOURCE_POOL_BACKOFF_ENV,
        HIRA_RESOURCE_POOL_DEFAULT_BACKOFF_S,
    ),
    max_backoff_s=_positive_env_float(
        HIRA_RESOURCE_POOL_MAX_BACKOFF_ENV,
        HIRA_RESOURCE_POOL_DEFAULT_MAX_BACKOFF_S,
    ),
)


def _nedrug_spec(tool: str, mcp_tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "resource_id": os.environ.get(NEDRUG_MCP_RESOURCE_ENV, NEDRUG_MCP_DEFAULT_RESOURCE),
        "source": NEDRUG_MCP_SOURCE,
        "mcp_tool": mcp_tool,
        "arguments": _strip_none_arguments(arguments),
    }


def _mfds_patent_result_limit(value: object | None = None) -> int:
    raw = value if value not in (None, "") else os.environ.get(
        MFDS_PATENT_MAX_RESULTS_ENV,
        str(MFDS_PATENT_DEFAULT_MAX_RESULTS),
    )
    try:
        parsed = int(str(raw))
    except (TypeError, ValueError):
        parsed = MFDS_PATENT_DEFAULT_MAX_RESULTS
    return max(1, min(parsed, MFDS_PATENT_PROVIDER_MAX_RESULTS))


def _hira_spec(tool: str, mcp_tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "resource_id": os.environ.get(HIRA_MCP_RESOURCE_ENV, HIRA_MCP_DEFAULT_RESOURCE),
        "source": HIRA_MCP_SOURCE,
        "mcp_tool": mcp_tool,
        "arguments": _strip_none_arguments(arguments),
    }


def _hira_disease_args(params: dict[str, str]) -> dict[str, Any]:
    display_code = params.get("sickCd", "")
    request_code = _hira_request_code(display_code)
    return {
        "sick_cd": request_code or display_code,
        "year": params.get("year"),
        "sick_type": _hira_sick_type(request_code) or "1",
        "med_tp": "1",
        "num_of_rows": 160,
    }


def _hira_request_code(code: str) -> str | None:
    compact = code.strip().upper().replace(".", "").replace("_", "")
    return compact if re.fullmatch(r"[A-Z]\d{2,3}", compact) else None


def _hira_sick_type(request_code: str | None) -> str | None:
    if request_code is None:
        return None
    if len(request_code) == 3:
        return "1"
    if len(request_code) == 4:
        return "2"
    return None


def _hira_procedure_args(params: dict[str, str]) -> dict[str, Any]:
    return {"st5_cd": params.get("st5Cd", ""), "year": params.get("year"), "std_type": params.get("stdType"), "num_of_rows": 20}


def _strip_none_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in arguments.items() if value not in {None, ""}}


def is_hira_disease_code(text: str) -> bool:
    """Return whether text is exactly a KCD-like HIRA disease code, not a free-form query."""

    return re.fullmatch(r"\s*[A-Za-z]\d{2}(?:\.?\d{1,2})?\s*", text) is not None


_HIRA_SEX_KEYS = ("sex", "sexCdNm", "sexNm", "sexName", "gender", "genderName")


def _aggregate_hira_patient_type_sexes(call: ExternalCall) -> ExternalCall:
    """Replace sex-split admission rows with explicit totals and breakdowns."""

    raw_items = call.render_data.get("items")
    if not isinstance(raw_items, list):
        return call

    grouped: dict[tuple[str, str, str], list[tuple[dict[str, Any], str, int]]] = {}
    untouched: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        patient_type = str(item.get("inpatOpat") or "").strip()
        sex = _hira_sex_label(item)
        patient_count = _int_or_none(str(item.get("ptntCnt") or ""))
        if not patient_type or not sex or patient_count is None:
            untouched.append(item)
            continue
        key = (
            patient_type,
            str(item.get("sickCd") or "").strip(),
            str(item.get("sickNm") or "").strip(),
        )
        grouped.setdefault(key, []).append((item, sex, patient_count))

    aggregated: list[dict[str, Any]] = []
    applied = False
    sex_labels_exposed = False
    for group in grouped.values():
        distinct_sexes = {sex for _, sex, _ in group}
        if len(group) < 2 or len(distinct_sexes) < 2:
            aggregated.extend(_with_hira_explicit_sex(item, sex) for item, sex, _ in group)
            sex_labels_exposed = True
            continue
        first = dict(group[0][0])
        for key in _HIRA_SEX_KEYS:
            first.pop(key, None)
        breakdown = [
            {"sex": sex, "ptntCnt": count}
            for _, sex, count in sorted(group, key=lambda value: _hira_sex_sort_key(value[1]))
        ]
        total = sum(entry["ptntCnt"] for entry in breakdown)
        breakdown_text = " + ".join(f"{entry['sex']} {entry['ptntCnt']:,}" for entry in breakdown)
        first.update(
            {
                "ptntCnt": str(total),
                "patientCountDisplay": f"{total:,} ({breakdown_text})",
                "sexBreakdown": breakdown,
                "sexAggregation": "sum",
            }
        )
        aggregated.append(first)
        applied = True

    if not applied and not sex_labels_exposed:
        return call
    return ExternalCall(
        tool=call.tool,
        source=call.source,
        status=call.status,
        summary_text=call.summary_text,
        render_data={
            **call.render_data,
            "items": [*aggregated, *untouched],
            "totalCount": len(aggregated) + len(untouched),
            "sex_aggregation_applied": applied,
            "sex_labels_exposed": sex_labels_exposed,
        },
        safe_url=call.safe_url,
        elapsed_ms=call.elapsed_ms,
    )


def _with_hira_explicit_sex(item: dict[str, Any], sex: str) -> dict[str, Any]:
    return {**item, "patientTypeDisplay": f"{item.get('inpatOpat')}({sex})"}


def _hira_sex_label(item: dict[str, Any]) -> str:
    raw = next(
        (item.get(key) for key in _HIRA_SEX_KEYS if item.get(key) not in (None, "")),
        "",
    )
    normalized = str(raw).strip().casefold()
    if normalized in {"1", "m", "male", "남", "남성", "남자"}:
        return "남"
    if normalized in {"2", "f", "female", "여", "여성", "여자"}:
        return "여"
    return str(raw).strip()


def _hira_sex_sort_key(sex: str) -> tuple[int, str]:
    return ({"남": 0, "여": 1}.get(sex, 2), sex)


def _fixture_hira_disease_name_code(search_text: str, disease_type: str, data: dict[str, Any]) -> ExternalCall:
    normalized = search_text.strip().upper()
    items = data.get("render_data", {}).get("items", [])
    matched_code = next(
        (
            item
            for item in items
            if isinstance(item, dict)
            and is_hira_disease_code(search_text)
            and str(item.get("sickCd") or "").strip().upper() == normalized
        ),
        None,
    )
    if matched_code is not None:
        return ExternalCall(
            tool="hira_disease_name_code",
            source=HIRA_DISEASE_SOURCE,
            status="fixture",
            summary_text=data["summary_text"],
            render_data={
                **data["render_data"],
                "totalCount": "1",
                "items": [matched_code],
                "request": {"sickCd": search_text, "searchText": search_text, "diseaseType": disease_type},
            },
            safe_url=data.get("safe_url"),
            elapsed_ms=0.0,
        )
    return ExternalCall(
        tool="hira_disease_name_code",
        source=HIRA_DISEASE_SOURCE,
        status="no_data",
        summary_text=f"HIRA search_disease_code fixture has no candidate for {search_text}.",
        render_data={
            "totalCount": "0",
            "items": [],
            "request": {"sickCd": search_text, "searchText": search_text, "diseaseType": disease_type},
            "message": "fixture search result absent",
        },
        safe_url=data.get("safe_url"),
        elapsed_ms=0.0,
    )


def _openfda_active_ingredient(params: dict[str, str]) -> str:
    search = params.get("search", "")
    match = re.search(r'openfda\.substance_name:"([^"]+)"', search)
    return match.group(1) if match else search


def _mcp_external_call(
    tool: str,
    source: str,
    params: dict[str, str],
    mcp_tool: str,
    result: McpToolResult,
    url: str,
    elapsed: float,
) -> ExternalCall:
    if tool == "tavily_mcp_search":
        return _tavily_mcp_web_call(params, result, elapsed)
    payload = _mcp_payload(result)
    provider_error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(provider_error, str) and provider_error.strip():
        resource_pool_failure = (
            source == HIRA_MCP_SOURCE
            and _HIRA_RESOURCE_POOL_RE.search(provider_error) is not None
        )
        return _mcp_failed_call(
            tool,
            source,
            params,
            mcp_tool,
            provider_error.strip(),
            url,
            elapsed,
            error_type=("provider_resource_limit" if resource_pool_failure else None),
            public_message=(
                _HIRA_RESOURCE_POOL_PUBLIC_MESSAGE if resource_pool_failure else None
            ),
        )
    if source in {NEDRUG_MCP_SOURCE, HIRA_MCP_SOURCE} and _public_data_quota_exceeded(
        payload,
        result.content_text,
    ):
        return _mcp_quota_call(tool, source, params, mcp_tool, url, elapsed)
    if result.raw_result.get("isError") is True:
        return _mcp_failed_call(
            tool,
            source,
            params,
            mcp_tool,
            result.content_text,
            url,
            elapsed,
        )
    if tool == "clinicaltrials_study_details":
        return _clinicaltrials_detail_call_from_mcp(params, mcp_tool, result, url, elapsed)
    if tool == "clinicaltrials_v2_search":
        return _clinicaltrials_call_from_mcp(params, mcp_tool, result, url, elapsed)
    if mcp_tool == "search_drug_labels" and not (
        isinstance(payload, dict) and isinstance(payload.get("results"), list)
    ):
        payload = _openfda_label_mcp_payload(result.content_text)
    if mcp_tool == "search_drug_adverse_events" and not (
        isinstance(payload, dict) and isinstance(payload.get("results"), list)
    ):
        payload = _openfda_adverse_mcp_payload(result.content_text)
    render_data = _mcp_render_data(payload, params, mcp_tool, result.content_text)
    has_items = bool(render_data.get("items") or render_data.get("payload", {}).get("results"))
    status = "live" if has_items else "no_data"
    if not has_items:
        render_data["message"] = "MCP 조회 결과 없음"
    return ExternalCall(
        tool=tool,
        source=source,
        status=status,
        summary_text=_mcp_summary(tool, status, render_data),
        render_data=render_data,
        safe_url=url,
        elapsed_ms=elapsed,
    )


def _tavily_mcp_web_call(
    params: dict[str, str],
    result: McpToolResult,
    elapsed: float,
) -> ExternalCall:
    query = params.get("query", "")
    if result.raw_result.get("isError") is True:
        return _web_error(
            TAVILY_MCP_SOURCE,
            query,
            McpClientError(result.content_text or "MCP tool error"),
            elapsed,
        )
    payload = _mcp_payload(result)
    raw_items = payload.get("results") if isinstance(payload, dict) else None
    parser_outcome = "structured_results"
    if raw_items is None and isinstance(payload, dict) and isinstance(payload.get("text"), str):
        raw_items = _tavily_text_results(payload["text"])
        parser_outcome = "parsed_text_results" if raw_items else "parse_failure"
    if not isinstance(raw_items, list):
        parser_outcome = "parse_failure"
        raw_items = []
    if parser_outcome == "parse_failure":
        call = _web_error(
            TAVILY_MCP_SOURCE,
            query,
            McpClientError("Tavily MCP schema parse failure: unsupported response shape"),
            elapsed,
        )
        return ExternalCall(
            tool=call.tool,
            source=call.source,
            status=call.status,
            summary_text=call.summary_text,
            render_data={
                **call.render_data,
                "error_type": "parse_failure",
                "parser_outcome": "parse_failure",
            },
            safe_url=call.safe_url,
            elapsed_ms=call.elapsed_ms,
        )
    if not raw_items:
        parser_outcome = "empty_result"
    max_results = _int_or_none(params.get("max_results", ""))
    limit = _bounded_web_results(
        max_results if max_results is not None else WEB_SEARCH_MAX_RESULTS
    )
    items = [
        {
            "title": item.get("title"),
            "url": item.get("url"),
            "snippet": item.get("content") or item.get("snippet"),
            "published_at": item.get("published_date") or item.get("date"),
            "published_date": item.get("published_date") or item.get("date"),
        }
        for item in raw_items[:limit]
        if isinstance(item, dict)
    ]
    call = _web_call(TAVILY_MCP_SOURCE, query, items, elapsed)
    return ExternalCall(
        tool=call.tool,
        source=call.source,
        status=call.status,
        summary_text=call.summary_text,
        render_data={**call.render_data, "parser_outcome": parser_outcome},
        safe_url=call.safe_url,
        elapsed_ms=call.elapsed_ms,
    )


def _tavily_text_results(text: str) -> list[dict[str, Any]]:
    if "Detailed Results:" not in text:
        return []
    items: list[dict[str, Any]] = []
    blocks = re.split(r"(?m)(?=^Title:\s*)", text)
    for block in blocks:
        title_match = re.search(r"(?m)^Title:\s*(.+?)\s*$", block)
        url_match = re.search(r"(?m)^URL:\s*(https?://\S+)\s*$", block)
        if title_match is None or url_match is None:
            continue
        content_match = re.search(r"(?m)^Content:\s*(.+?)\s*$", block)
        published_match = re.search(
            r"(?m)^(?:Published Date|Published At|Date):\s*(.+?)\s*$",
            block,
        )
        raw_match = re.search(
            r"(?ms)^Raw Content:\s*(.+?)(?=\n(?:Score|Title):|\Z)",
            block,
        )
        content = content_match.group(1).strip() if content_match else ""
        if not content or content.casefold() == "undefined":
            content = raw_match.group(1).strip() if raw_match else ""
        items.append(
            {
                "title": title_match.group(1).strip(),
                "url": url_match.group(1).strip(),
                "content": content,
                "published_at": published_match.group(1).strip() if published_match else None,
                "published_date": published_match.group(1).strip() if published_match else None,
            }
        )
    return items


def _clinicaltrials_failed_call(params: dict[str, str], mcp_tool: str, reason: str, url: str, elapsed: float) -> ExternalCall:
    tool = "clinicaltrials_study_details" if "nct_id" in params else "clinicaltrials_v2_search"
    return ExternalCall(
        tool=tool,
        source=CLINICAL_TRIALS_MCP_SOURCE,
        status="error",
        summary_text=f"ClinicalTrials MCP 조회 실패: {reason}",
        render_data={
            "request": params,
            "payload": {"studies": []} if tool == "clinicaltrials_v2_search" else {},
            "mcp": {"tool": mcp_tool},
            "error": reason,
            "external_claim_policy": "fail_closed_error",
        },
        safe_url=url,
        elapsed_ms=elapsed,
    )


def _clinicaltrials_call_from_mcp(params: dict[str, str], mcp_tool: str, result: McpToolResult, url: str, elapsed: float) -> ExternalCall:
    structured_payload = _mcp_payload(result)
    payload = (
        structured_payload
        if isinstance(structured_payload, dict) and isinstance(structured_payload.get("studies"), list)
        else _clinicaltrials_mcp_payload(result.content_text)
    )
    studies = payload.get("studies", [])
    render_data = {
        "request": params,
        "payload": payload,
        "mcp": {"tool": mcp_tool, "content_text": result.content_text},
        "external_claim_policy": "source_relay_only",
    }
    if not isinstance(studies, list) or not studies:
        return ExternalCall(
            tool="clinicaltrials_v2_search",
            source=CLINICAL_TRIALS_MCP_SOURCE,
            status="no_data",
            summary_text="ClinicalTrials MCP 조회 결과가 없어 외부 임상 근거를 생성하지 않습니다.",
            render_data={**render_data, "payload": {"studies": []}, "message": "ClinicalTrials MCP 조회 결과 없음"},
            safe_url=url,
            elapsed_ms=elapsed,
        )
    nct_ids = _nct_ids_from_studies(studies)
    return ExternalCall(
        tool="clinicaltrials_v2_search",
        source=CLINICAL_TRIALS_MCP_SOURCE,
        status="live",
        summary_text=f"ClinicalTrials MCP에서 {params.get('query.intr') or params.get('query.condition', '')} 관련 NCT 원문 결과를 확인했습니다: {','.join(nct_ids[:3])}",
        render_data={**render_data, "nct_ids": nct_ids, "briefTitle": _first_study_value(studies, "briefTitle"), "overallStatus": _first_study_value(studies, "overallStatus")},
        safe_url=url,
        elapsed_ms=elapsed,
    )


def _clinicaltrials_detail_call_from_mcp(
    params: dict[str, str],
    mcp_tool: str,
    result: McpToolResult,
    url: str,
    elapsed: float,
) -> ExternalCall:
    detail = _clinicaltrials_detail_payload(result.content_text)
    nct_id = str(detail.get("nct_id") or params.get("nct_id") or "").upper()
    study_url = str(detail.get("url") or f"https://clinicaltrials.gov/study/{nct_id}")
    render_data = {
        "request": params,
        "detail": detail,
        "field_capabilities": {
            "outcomes": "SUPPORTED",
            "dates": "SUPPORTED",
            "phase": "SUPPORTED",
            "enrollment": "SUPPORTED",
            "interventions": "SUPPORTED",
            "eligibility": "PARTIAL",
        },
        "eligibility_disclosure": "선정·제외 기준은 원문 앞 200자까지만 제공됩니다.",
        "mcp": {"tool": mcp_tool},
        "external_claim_policy": "source_relay_only",
    }
    if not nct_id or not detail.get("title"):
        return ExternalCall(
            tool="clinicaltrials_study_details",
            source=CLINICAL_TRIALS_MCP_SOURCE,
            status="no_data",
            summary_text="ClinicalTrials 상세 응답에서 검증 가능한 연구 식별자와 제목을 찾지 못했습니다.",
            render_data=render_data,
            safe_url=study_url or url,
            elapsed_ms=elapsed,
        )
    return ExternalCall(
        tool="clinicaltrials_study_details",
        source=CLINICAL_TRIALS_MCP_SOURCE,
        status="live",
        summary_text=f"ClinicalTrials.gov에서 {nct_id} 상세 원문을 확인했습니다.",
        render_data=render_data,
        safe_url=study_url,
        elapsed_ms=elapsed,
    )


def _enrich_clinicaltrials_detail_from_official_api(
    call: ExternalCall,
    nct_id: str,
    *,
    timeout_s: int,
) -> ExternalCall:
    """Expose design fields omitted by the MCP adapter without inferring values."""

    started = time.monotonic()
    api_url = f"https://clinicaltrials.gov/api/v2/studies/{nct_id}"
    direct_payload: dict[str, Any] | None = None
    direct_errors: list[str] = []
    direct_attempts = 0
    for direct_attempts in range(1, 3):
        try:
            response = requests.get(api_url, timeout=min(timeout_s, 5))
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                direct_payload = payload
                break
            direct_errors.append("NonObjectPayload")
        except Exception as exc:  # noqa: BLE001 - retain the MCP result and expose adapter status
            direct_errors.append(type(exc).__name__)
        if direct_attempts == 1:
            time.sleep(0.15)
    direct_elapsed = round((time.monotonic() - started) * 1000, 1)

    render_data = dict(call.render_data)
    current_detail = render_data.get("detail")
    detail = dict(current_detail) if isinstance(current_detail, dict) else {}
    mapped: dict[str, Any] = {}
    if direct_payload is not None:
        protocol = direct_payload.get("protocolSection")
        protocol = protocol if isinstance(protocol, dict) else {}
        identification = protocol.get("identificationModule")
        design = protocol.get("designModule")
        arms_module = protocol.get("armsInterventionsModule")
        identification = identification if isinstance(identification, dict) else {}
        design = design if isinstance(design, dict) else {}
        arms_module = arms_module if isinstance(arms_module, dict) else {}
        design_info = design.get("designInfo")
        enrollment_info = design.get("enrollmentInfo")
        masking_info = design_info.get("maskingInfo") if isinstance(design_info, dict) else {}
        design_info = design_info if isinstance(design_info, dict) else {}
        enrollment_info = enrollment_info if isinstance(enrollment_info, dict) else {}
        masking_info = masking_info if isinstance(masking_info, dict) else {}
        arms = arms_module.get("armGroups")
        arms = [dict(item) for item in arms if isinstance(item, dict)] if isinstance(arms, list) else []
        phases = design.get("phases")
        phase_value: Any = phases if isinstance(phases, list) else None
        title_parts = tuple(
            str(identification.get(key) or "")
            for key in ("briefTitle", "officialTitle")
        )
        masking_wording = next(
            (
                match.group(0)
                for title in title_parts
                for match in (re.search(r"\b(?:Single|Double|Triple|Quadruple)\s+Mask(?:ed|ing)\b", title, re.IGNORECASE),)
                if match is not None
            ),
            None,
        )
        mapped = {
            "phase": phase_value,
            "allocation": design_info.get("allocation"),
            "intervention_model": design_info.get("interventionModel"),
            "masking": masking_info.get("masking"),
            "masking_roles": masking_info.get("whoMasked"),
            "masking_source_wording": masking_wording,
            "primary_purpose": design_info.get("primaryPurpose"),
            "enrollment": enrollment_info.get("count"),
            "arms": arms,
            "active_comparator_present": any(
                str(arm.get("type") or "").upper() == "ACTIVE_COMPARATOR"
                for arm in arms
            ),
        }
        for key, value in mapped.items():
            if value not in (None, "", [], {}):
                detail[key] = value

    requested_fields = (
        "phase",
        "allocation",
        "intervention_model",
        "masking",
        "primary_purpose",
        "enrollment",
        "arms",
    )
    field_status: dict[str, str] = {}
    for key in requested_fields:
        if detail.get(key) not in (None, "", [], {}):
            field_status[key] = "present"
        elif direct_payload is not None:
            field_status[key] = "field_missing"
        else:
            field_status[key] = "adapter_not_exposed"
    render_data.update(
        {
            "detail": detail,
            "field_status": field_status,
            "direct_official_api": {
                "status": "ok" if direct_payload is not None else "unavailable",
                "url": api_url,
                "elapsed_ms": direct_elapsed,
                "attempt_count": direct_attempts,
                "error_types": direct_errors,
                "error_type": direct_errors[-1] if direct_errors else None,
            },
            "field_capabilities": {
                **(
                    dict(render_data.get("field_capabilities"))
                    if isinstance(render_data.get("field_capabilities"), dict)
                    else {}
                ),
                **{key: field_status[key].upper() for key in requested_fields},
            },
        }
    )
    status = "live" if direct_payload is not None or call.status not in {"error", "no_data", "unsupported"} else call.status
    summary = (
        f"ClinicalTrials.gov 공식 API에서 {nct_id} 디자인 필드를 직접 매핑했습니다."
        if direct_payload is not None
        else call.summary_text
    )
    return ExternalCall(
        tool=call.tool,
        source=call.source,
        status=status,
        summary_text=summary,
        render_data=render_data,
        safe_url=f"https://clinicaltrials.gov/study/{nct_id}",
        elapsed_ms=round(float(call.elapsed_ms or 0.0) + direct_elapsed, 1),
    )


def _clinicaltrials_detail_payload(text: str) -> dict[str, Any]:
    aliases = {
        "nctId": "nct_id",
        "clinicaltrials_url": "url",
        "briefTitle": "title",
        "officialTitle": "official_title",
        "overallStatus": "status",
        "allocation": "allocation",
        "masking": "masking",
        "interventionModel": "intervention_model",
        "startDate": "start_date",
        "primaryCompletionDate": "primary_completion_date",
        "phases": "phase",
        "enrollmentCount": "enrollment",
        "interventions": "interventions",
        "primaryOutcomes": "outcomes",
        "secondaryOutcomes": "secondary_outcomes",
        "eligibilityCriteria": "eligibility",
    }
    detail: dict[str, Any] = {}
    row_mode: str | None = None
    for raw_line in text.splitlines():
        stripped = raw_line.strip().lstrip("- ")
        if stripped.startswith("primary["):
            row_mode = "outcomes"
            continue
        if stripped.startswith("secondary["):
            row_mode = "secondary_outcomes"
            continue
        if stripped.startswith("interventions["):
            row_mode = "interventions"
            continue
        if re.match(r"^[A-Za-z][A-Za-z]+(?:\[[^]]+\])?:", stripped):
            row_mode = None
        if row_mode is not None and "," in stripped and ":" not in stripped:
            values = next(csv.reader([stripped]))
            if row_mode == "interventions" and len(values) >= 2:
                detail.setdefault(row_mode, []).append(values[1].strip())
            elif values:
                detail.setdefault(row_mode, []).append(values[0].strip())
            continue
        if ":" not in stripped:
            continue
        raw_key, raw_value = stripped.split(":", 1)
        normalized_key = re.sub(r"\[[^]]+\](?:\{[^}]+\})?$", "", raw_key.strip())
        key = aliases.get(normalized_key)
        if key is None:
            continue
        value = raw_value.strip().strip('"')
        if key == "enrollment":
            detail[key] = _int_or_none(value)
        elif key in {"interventions", "outcomes", "secondary_outcomes"}:
            detail[key] = [part.strip() for part in value.split(",") if part.strip()]
        else:
            detail[key] = value
    if not detail.get("title") and detail.get("official_title"):
        detail["title"] = detail["official_title"]
    return detail


def _mcp_failed_call(
    tool: str,
    source: str,
    params: dict[str, str],
    mcp_tool: str,
    reason: str,
    url: str,
    elapsed: float,
    *,
    no_data: bool = False,
    error_type: str | None = None,
    public_message: str | None = None,
    extra_render_data: dict[str, Any] | None = None,
) -> ExternalCall:
    status = "no_data" if no_data else "error"
    message = public_message or (
        "MCP 조회 결과 없음" if no_data else "MCP 조회 실패"
    )
    render_data: dict[str, Any] = {
        "request": params,
        "mcp": {"tool": mcp_tool},
        "error": reason,
        "message": message,
    }
    if error_type:
        render_data["error_type"] = error_type
    if extra_render_data:
        render_data.update(extra_render_data)
    return ExternalCall(
        tool=tool,
        source=source,
        status=status,
        summary_text=(
            public_message
            or f"{mcp_tool} MCP 조회 {'결과 없음' if no_data else '실패'}: {reason}"
        ),
        render_data=render_data,
        safe_url=url,
        elapsed_ms=elapsed,
    )


def _mcp_quota_call(
    tool: str,
    source: str,
    params: dict[str, str],
    mcp_tool: str,
    url: str,
    elapsed: float,
) -> ExternalCall:
    return ExternalCall(
        tool=tool,
        source=source,
        status="error",
        summary_text=f"{mcp_tool} MCP 제공자 조회 한도 초과",
        render_data={
            "request": params,
            "mcp": {"tool": mcp_tool},
            "items": [],
            "error_type": "quota",
            "message": "제공자 조회 한도 초과",
        },
        safe_url=url,
        elapsed_ms=elapsed,
    )


def _public_data_quota_exceeded(payload: Any, content_text: str) -> bool:
    pending = [payload]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if key in {"resultCode", "result_code"} and str(value).strip() == "22":
                    return True
                if isinstance(value, (dict, list, tuple)):
                    pending.append(value)
        elif isinstance(current, (list, tuple)):
            pending.extend(current)
    try:
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        serialized = str(payload)
    return failure_class_from_exception(
        RuntimeError(f"{serialized} {content_text}")
    ) == "quota"


def _mcp_payload(result: McpToolResult) -> Any:
    structured = result.raw_result.get("structuredContent")
    if isinstance(structured, dict) and "result" in structured:
        payload = structured["result"]
        if isinstance(payload, str):
            return _json_or_text(payload)
        if isinstance(payload, (dict, list)):
            return payload
    return _json_or_text(result.content_text)


def _json_or_text(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return []
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return {"text": stripped}


def _mcp_render_data(payload: Any, params: dict[str, str], mcp_tool: str, content_text: str) -> dict[str, Any]:
    if mcp_tool == "search_korea_drug_patent":
        result_limit = _mfds_patent_result_limit(params.get("limit"))
    elif (
        mcp_tool == "search_disease_code"
        or mcp_tool.startswith("get_disease_stats_")
    ) and isinstance(payload, list):
        result_limit = len(payload)
    else:
        result_limit = 5
    if isinstance(payload, list):
        return {
            "request": params,
            "request_limit": result_limit,
            "source_limit_reached": (
                mcp_tool == "search_korea_drug_patent"
                and len(payload) >= result_limit
            ),
            "resultCode": "00",
            "totalCount": len(payload),
            "items": payload[:result_limit],
            "mcp": _mcp_trace_metadata(mcp_tool, content_text),
        }
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return {"request": params, "payload": {"meta": payload.get("meta", {}), "results": payload["results"][:5]}, "mcp": _mcp_trace_metadata(mcp_tool, content_text)}
    if isinstance(payload, dict):
        return {"request": params, "payload": payload, "mcp": _mcp_trace_metadata(mcp_tool, content_text)}
    return {"request": params, "payload": {"value": payload}, "mcp": _mcp_trace_metadata(mcp_tool, content_text)}


def _mcp_trace_metadata(mcp_tool: str, content_text: str) -> dict[str, Any]:
    if mcp_tool != "search_korea_drug_patent":
        return {"tool": mcp_tool, "content_text": content_text}
    encoded = content_text.encode("utf-8")
    return {
        "tool": mcp_tool,
        "content_length": len(encoded),
        "content_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _mcp_summary(tool: str, status: str, render_data: dict[str, Any]) -> str:
    if status == "no_data":
        return f"{tool} MCP returned no results"
    if "items" in render_data:
        return f"{tool} MCP returned totalCount={render_data.get('totalCount')}"
    payload = render_data.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return f"{tool} MCP returned results={len(payload['results'])}"
    return f"{tool} MCP returned data"


def _openfda_adverse_mcp_payload(text: str) -> dict[str, Any]:
    total = 0
    events: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_reactions = False

    def finish_event() -> None:
        nonlocal current
        if current is None:
            return
        reactions = current.get("reaction_terms")
        if isinstance(reactions, list) and reactions:
            date = str(current.get("date") or "기간 미상")
            report_id = str(current.get("safety_report_id") or "식별자 미상")
            current["title"] = (
                f"FAERS 보고 {report_id} ({date}) · "
                f"보고 반응: {', '.join(str(item) for item in reactions)}"
            )
        events.append(current)
        current = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("total_results:"):
            total = _int_or_none(stripped.split(":", 1)[1].strip()) or 0
            continue
        report_match = re.match(r'^  - safety_report_id:\s*"?([^"\s]+)"?$', line)
        if report_match:
            finish_event()
            current = {
                "safety_report_id": report_match.group(1),
                "drug_names": [],
                "reaction_terms": [],
            }
            in_reactions = False
            continue
        if current is None:
            continue
        if re.match(r"^    reactions\[", line):
            in_reactions = True
            continue
        if in_reactions and line.startswith("      ") and "," in stripped and ":" not in stripped:
            reaction, _outcome = stripped.split(",", 1)
            current["reaction_terms"].append(reaction.strip().strip('"'))
            continue
        if line.startswith("      - name:"):
            name = stripped.split(":", 1)[1].strip().strip('"')
            current["drug_names"].append(name)
            in_reactions = False
            continue
        if re.match(r"^(?:generic_name|substance_name|brand_name)(?:\[[^\]]+\])?:", stripped):
            name = stripped.split(":", 1)[1].strip().strip("[]\"'")
            if name:
                current["drug_names"].append(name)
            in_reactions = False
            continue
        if line.startswith("    report_date:"):
            current["date"] = stripped.split(":", 1)[1].strip().strip('"')
        elif line.startswith("    serious:"):
            current["serious"] = stripped.split(":", 1)[1].strip() == "Yes"
        elif line.startswith("    country:"):
            current["country"] = stripped.split(":", 1)[1].strip().strip('"')

    finish_event()
    return {
        "meta": {"results": {"total": total}},
        "results": events[:5],
    }


def _openfda_label_mcp_payload(text: str) -> dict[str, Any]:
    total_match = re.search(r"(?m)^total_results:\s*(\d+)\s*$", text)
    total = int(total_match.group(1)) if total_match else 0
    labels: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for line in text.splitlines():
        match = re.match(
            r"^\s+(?P<item>-\s+)?(?P<key>[a-z_]+)(?:\[\d+\])?:\s*(?P<value>.*)$",
            line,
        )
        if match is None:
            continue
        if match.group("item"):
            if current is not None:
                labels.append(current)
            current = {}
        if current is None:
            continue
        value: Any = match.group("value").strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        elif value == "[]":
            value = []
        current[match.group("key")] = value

    if current is not None:
        labels.append(current)
    return {
        "meta": {"results": {"total": total}},
        "results": labels[:5],
    }

def _clinicaltrials_mcp_payload(text: str) -> dict[str, Any]:
    studies: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    intervention_mode = False
    next_page_token = ""
    total_count: int | None = None
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("totalCount:"):
            total_count = _int_or_none(stripped.split(":", 1)[1].strip())
            continue
        if stripped.startswith("nextPageToken:"):
            next_page_token = stripped.split(":", 1)[1].strip()
            continue
        if stripped.startswith("- "):
            if current:
                studies.append(_clinical_study_from_flat(current))
            current = {}
            intervention_mode = False
            _apply_mcp_key_value(current, stripped[2:])
            continue
        if current is None:
            continue
        if stripped.startswith("interventions["):
            intervention_mode = True
            current.setdefault("interventions", [])
            continue
        if intervention_mode and "," in stripped and ":" not in stripped:
            kind, name = stripped.split(",", 1)
            interventions = current.setdefault("interventions", [])
            if isinstance(interventions, list):
                interventions.append({"type": kind.strip(), "name": name.strip().strip('"')})
            continue
        intervention_mode = False
        _apply_mcp_key_value(current, stripped)
    if current:
        studies.append(_clinical_study_from_flat(current))
    payload: dict[str, Any] = {"studies": studies[:5]}
    if next_page_token:
        payload["nextPageToken"] = next_page_token
    if total_count is not None:
        payload["totalCount"] = total_count
    return payload


def _apply_mcp_key_value(target: dict[str, Any], text: str) -> None:
    match = re.match(r"(?P<key>[A-Za-z_]+)(?:\[[^\]]+\])?:\s*(?P<value>.*)$", text)
    if not match:
        return
    key = {
        "NCTId": "nctId",
        "briefTitle": "title",
        "overallStatus": "status",
    }.get(match.group("key"), match.group("key"))
    value = match.group("value").strip().strip('"')
    if key in {
        "clinicaltrials_url",
        "nctId",
        "title",
        "officialTitle",
        "status",
        "studyType",
        "allocation",
        "masking",
        "interventionModel",
        "sponsor",
        "startDate",
        "url",
    }:
        target[key] = value
    elif key in {"phase", "conditions"}:
        target[key] = [part.strip() for part in value.split(",") if part.strip()]


def _clinical_study_from_flat(flat: dict[str, Any]) -> dict[str, Any]:
    nct_id = str(flat.get("nctId") or "")
    official_title = str(flat.get("officialTitle") or "")
    title = str(flat.get("title") or official_title)
    status = str(flat.get("status") or "")
    phases = flat.get("phase") if isinstance(flat.get("phase"), list) else []
    interventions = flat.get("interventions") if isinstance(flat.get("interventions"), list) else []
    study = {
        "NCTId": nct_id,
        "briefTitle": title,
        "overallStatus": status,
        "url": flat.get("url") or flat.get("clinicaltrials_url"),
        "protocolSection": {
            "identificationModule": {
                "nctId": nct_id,
                "briefTitle": title,
                "officialTitle": official_title or None,
            },
            "statusModule": {"overallStatus": status, "startDate": flat.get("startDate")},
            "designModule": {
                "phases": phases,
                "studyType": flat.get("studyType"),
                "allocation": flat.get("allocation"),
                "masking": flat.get("masking"),
                "interventionModel": flat.get("interventionModel"),
            },
            "sponsorCollaboratorsModule": {"leadSponsor": {"name": flat.get("sponsor")}},
            "armsInterventionsModule": {"interventions": interventions},
        },
    }
    conditions = flat.get("conditions")
    if isinstance(conditions, list):
        study["protocolSection"]["conditionsModule"] = {"conditions": conditions}
    return study


def _nct_ids_from_studies(studies: list[Any]) -> list[str]:
    out: list[str] = []
    for study in studies:
        if not isinstance(study, dict):
            continue
        nct_id = study.get("NCTId")
        if isinstance(nct_id, str) and nct_id:
            out.append(nct_id)
    return out


def _first_study_value(studies: list[Any], key: str) -> str:
    if not studies or not isinstance(studies[0], dict):
        return ""
    value = studies[0].get(key)
    return value if isinstance(value, str) else ""


def _int_or_none(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _bounded_web_results(max_results: int) -> int:
    return max(1, min(max_results, WEB_SEARCH_MAX_RESULTS))


def _missing_web_key(provider: str, key_env: str, query: str) -> ExternalCall:
    return ExternalCall(
        tool="web_search",
        source=WEB_SEARCH_SOURCE,
        status="missing_key",
        summary_text=f"{provider} 웹검색 API 키({key_env})가 없어 웹 검색을 실행하지 않았습니다.",
        render_data={
            "provider": provider,
            "query": query,
            "items": [],
            "message": f"{key_env} 미설정",
            "external_claim_policy": "web_results_unverified",
        },
        elapsed_ms=0.0,
    )


def _web_error(provider: str, query: str, exc: Exception, elapsed: float) -> ExternalCall:
    error_text = str(exc)
    quota_exceeded = failure_class_from_exception(RuntimeError(error_text)) == "quota"
    return ExternalCall(
        tool="web_search",
        source=WEB_SEARCH_SOURCE,
        status="error",
        summary_text=(
            f"{provider} 웹검색 조회 한도 초과"
            if quota_exceeded
            else f"{provider} 웹검색 실패: {error_text}"
        ),
        render_data={
            "provider": provider,
            "query": query,
            "items": [],
            "message": "웹검색 실패",
            "error": error_text,
            **({"error_type": "quota"} if quota_exceeded else {}),
            "external_claim_policy": "web_results_unverified",
        },
        elapsed_ms=elapsed,
    )


def _web_call(provider: str, query: str, items: list[dict[str, Any]], elapsed: float) -> ExternalCall:
    normalized_items: list[dict[str, Any]] = []
    publication_date_required = bool(_PUBLICATION_DATE_QUERY_RE.search(query))
    collected_at = datetime.now(timezone.utc).isoformat()
    for raw_item in items:
        item = dict(raw_item)
        url = str(item.get("url") or "")
        host = (urlparse(url).hostname or "").casefold()
        if host in _PERSONAL_BLOG_HOSTS or any(
            host.endswith(f".{blocked}") for blocked in _PERSONAL_BLOG_HOSTS
        ):
            continue
        published_at = item.get("published_at") or item.get("published_date")
        if publication_date_required and not published_at:
            published_at = _fetch_publication_date(url)
            if published_at:
                item["published_at_source"] = "page_metadata"
        item["published_at"] = published_at
        item["published_date"] = published_at
        item["published_at_label"] = published_at or "게시일 미상"
        item["collected_at"] = collected_at
        item["content_status"] = "snippet_only"
        item.pop("raw_content", None)
        normalized_items.append(item)
    status = "live" if normalized_items else "no_data"
    summary = f"{provider} 웹검색 결과 {len(normalized_items)}건을 확인했습니다." if normalized_items else f"{provider} 웹검색 결과가 없습니다."
    return ExternalCall(
        tool="web_search",
        source=WEB_SEARCH_SOURCE,
        status=status,
        summary_text=summary,
        render_data={
            "provider": provider,
            "query": query,
            "items": normalized_items,
            "external_claim_policy": "web_results_unverified",
            "verification_notice": "웹 검색 결과(미검증): URL과 snippet을 출처로 분리 표시하고 내부 fact로 승격하지 않습니다.",
        },
        elapsed_ms=elapsed,
    )


def _fetch_publication_date(url: str) -> str | None:
    """Read only the public page head and expose an explicit publication date."""

    if not _is_public_web_url(url):
        return None
    response: requests.Response | None = None
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; JWChatSourceDate/1.0)"},
            timeout=(2, 3),
            allow_redirects=True,
            stream=True,
        )
        response.raise_for_status()
        if not _is_public_web_url(response.url):
            return None
        content_type = str(response.headers.get("Content-Type") or "").casefold()
        if "html" not in content_type and "text" not in content_type:
            return None
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=16 * 1024):
            if not chunk:
                continue
            remaining = _PUBLICATION_METADATA_MAX_BYTES - size
            if remaining <= 0:
                break
            chunks.append(chunk[:remaining])
            size += min(len(chunk), remaining)
        page = b"".join(chunks).decode(response.encoding or "utf-8", errors="ignore")
    except requests.RequestException:
        return None
    finally:
        if response is not None:
            response.close()
    for pattern in _PUBLICATION_DATE_PATTERNS:
        match = pattern.search(page)
        if match is not None:
            return match.group(1).strip()
    return None


def _is_public_web_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or "." not in host:
        return False
    if host.endswith((".svc", ".cluster.local")) or host == "localhost":
        return False
    try:
        address = ip_address(host)
    except ValueError:
        return True
    return not (address.is_private or address.is_loopback or address.is_link_local)
