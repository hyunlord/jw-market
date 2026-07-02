from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import re
import time
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

import requests

from jw_chat_agent_poc.tools.external.mcp_client import McpClientError, McpJsonClient
from jw_chat_agent_poc.tools.external.response_parsing import parse_response, render_payload, summary


DATA_GO_KR_KEY_ENV = "DATA_GO_KR_KEY"
CLINICAL_TRIALS_MCP_URL_ENV = "CLINICAL_TRIALS_MCP_URL"
WEB_SEARCH_PROVIDER_ENV = "WEB_SEARCH_PROVIDER"
TAVILY_API_KEY_ENV = "TAVILY_API_KEY"
SERPER_API_KEY_ENV = "SERPER_API_KEY"
BRAVE_SEARCH_API_KEY_ENV = "BRAVE_SEARCH_API_KEY"
MFDS_PATENT_QUERY_ALIASES = {
    "pitavastatin": "리바로",
    "ezetimibe": "리바로젯",
}
HIRA_DISEASE_SOURCE = "hira_disease"
HIRA_PROCEDURE_SOURCE = "hira_procedure"
WEB_SEARCH_SOURCE = "web_search"


@dataclass(frozen=True)
class ExternalCall:
    tool: str
    source: str
    status: str
    summary_text: str
    render_data: dict[str, Any]
    safe_url: str | None = None
    elapsed_ms: float | None = None


class ExternalApiClient:
    def __init__(self, mode: str = "fixture", fixture_path: Path | None = None, timeout_s: int = 12) -> None:
        self.mode = mode
        self.timeout_s = timeout_s
        path = fixture_path or Path(__file__).resolve().parents[2] / "fixtures" / "external_api_fixtures.json"
        self.fixtures = json.loads(path.read_text(encoding="utf-8"))

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

    def mfds_composition(self, item_seq: str) -> ExternalCall:
        return self._fixture_or_live("mfds_composition", {"item_seq": item_seq})

    def mfds_easy_drug(self, item_seq: str) -> ExternalCall:
        return self._fixture_or_live("mfds_easy_drug", {"item_seq": item_seq})

    def mfds_clinical_trial_kr(self, keyword: str) -> ExternalCall:
        return self._fixture_or_live("mfds_clinical_trial_kr", {"keyword": keyword})

    def clinicaltrials_v2_search(self, query_intr: str) -> ExternalCall:
        if self.mode == "live":
            return self._clinicaltrials_mcp_search(query_intr)
        return self._fixture_or_live("clinicaltrials_v2_search", {"query.intr": query_intr})

    def openfda_label_search(self, substance_name: str) -> ExternalCall:
        query = f'openfda.substance_name:"{substance_name.upper()}"'
        return self._fixture_or_live("openfda_label_search", {"search": query})

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

    def mfds_patent(self, ingredient_en: str) -> ExternalCall:
        query = MFDS_PATENT_QUERY_ALIASES.get(ingredient_en.lower(), ingredient_en)
        return self._fixture_or_live("mfds_patent", {"query": query}, xml=True)

    def mfds_fda_orangebook(self, ingredient_en: str) -> ExternalCall:
        return self._fixture_or_live("mfds_fda_orangebook", {"query": ingredient_en.title()}, xml=True)

    def hira_disease_name_code(self, sick_cd: str) -> ExternalCall:
        call = self._fixture_or_live("hira_disease_name_code", {"sickCd": sick_cd}, xml=True)
        return self._with_source(call, HIRA_DISEASE_SOURCE)

    def hira_disease_hospitalization_outpatient_stats(self, sick_cd: str, year: str = "2024") -> ExternalCall:
        call = self._fixture_or_live(
            "hira_disease_hospitalization_outpatient_stats",
            {"sickCd": sick_cd, "year": year},
            xml=True,
        )
        return self._with_source(call, HIRA_DISEASE_SOURCE)

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

    def web_search(self, query: str, max_results: int = 5) -> ExternalCall:
        if self.mode != "live":
            call = self._fixture_or_live("web_search", {"query": query, "max_results": str(max_results)})
            return self._with_source(call, WEB_SEARCH_SOURCE)
        return self._live_web_search(query, max_results=max_results)

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
        return self._live_get(tool, params, xml=xml)

    def _clinicaltrials_mcp_search(self, query_intr: str) -> ExternalCall:
        url = os.environ.get(CLINICAL_TRIALS_MCP_URL_ENV)
        if not url:
            return self._clinicaltrials_fail_closed(
                query_intr,
                f"{CLINICAL_TRIALS_MCP_URL_ENV} is not configured",
                elapsed_ms=0.0,
            )
        start = time.monotonic()
        try:
            result = McpJsonClient(url, timeout_s=self.timeout_s).call_tool(
                "search_studies",
                {"intervention": query_intr, "pageSize": 5},
            )
        except McpClientError as exc:
            elapsed = round((time.monotonic() - start) * 1000, 1)
            return self._clinicaltrials_fail_closed(query_intr, exc.message, elapsed_ms=elapsed, safe_url=url)
        elapsed = round((time.monotonic() - start) * 1000, 1)
        payload = _clinicaltrials_mcp_payload(result.content_text)
        studies = payload.get("studies", [])
        if not isinstance(studies, list) or not studies:
            return ExternalCall(
                tool="clinicaltrials_v2_search",
                source="external_api",
                status="no_data",
                summary_text="ClinicalTrials MCP 조회 결과가 없어 외부 임상 근거를 생성하지 않습니다.",
                render_data={
                    "request": {"query.intr": query_intr},
                    "payload": {"studies": []},
                    "mcp": {"tool": "search_studies", "content_text": result.content_text},
                    "external_claim_policy": "fail_closed_no_source_rows",
                    "message": "ClinicalTrials MCP 조회 결과 없음",
                },
                safe_url=url,
                elapsed_ms=elapsed,
            )
        nct_ids = _nct_ids_from_studies(studies)
        return ExternalCall(
            tool="clinicaltrials_v2_search",
            source="external_api",
            status="live",
            summary_text=f"ClinicalTrials MCP에서 {query_intr} 관련 NCT 원문 결과를 확인했습니다: {','.join(nct_ids[:3])}",
            render_data={
                "request": {"query.intr": query_intr},
                "payload": payload,
                "nct_ids": nct_ids,
                "briefTitle": _first_study_value(studies, "briefTitle"),
                "overallStatus": _first_study_value(studies, "overallStatus"),
                "mcp": {"tool": "search_studies", "content_text": result.content_text},
                "external_claim_policy": "source_relay_only",
            },
            safe_url=url,
            elapsed_ms=elapsed,
        )

    def _live_web_search(self, query: str, max_results: int = 5) -> ExternalCall:
        provider = os.environ.get(WEB_SEARCH_PROVIDER_ENV, "tavily").strip().lower()
        if provider == "tavily":
            return self._live_tavily_search(query, max_results)
        if provider == "serper":
            return self._live_serper_search(query, max_results)
        if provider == "brave":
            return self._live_brave_search(query, max_results)
        return ExternalCall(
            tool="web_search",
            source=WEB_SEARCH_SOURCE,
            status="unsupported",
            summary_text=f"지원하지 않는 web search provider: {provider}",
            render_data={"query": query, "provider": provider, "items": [], "external_claim_policy": "web_results_unverified"},
            elapsed_ms=0.0,
        )

    def _live_tavily_search(self, query: str, max_results: int) -> ExternalCall:
        key = os.environ.get(TAVILY_API_KEY_ENV)
        if not key:
            return _missing_web_key("tavily", TAVILY_API_KEY_ENV, query)
        start = time.monotonic()
        try:
            response = requests.post(
                "https://api.tavily.com/search",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"query": query, "max_results": max(1, min(max_results, 5)), "search_depth": "basic", "include_answer": False},
                timeout=min(self.timeout_s, 10),
            )
            elapsed = round((time.monotonic() - start) * 1000, 1)
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            elapsed = round((time.monotonic() - start) * 1000, 1)
            return _web_error("tavily", query, exc, elapsed)
        items = [
            {"title": item.get("title"), "url": item.get("url"), "snippet": item.get("content") or item.get("snippet")}
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
            {"title": item.get("title"), "url": item.get("link"), "snippet": item.get("snippet")}
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
            {"title": item.get("title"), "url": item.get("url"), "snippet": item.get("description")}
            for item in web.get("results", [])[:max_results]
            if isinstance(item, dict)
        ]
        return _web_call("brave", query, items, elapsed)

    @staticmethod
    def _clinicaltrials_fail_closed(
        query_intr: str,
        reason: str,
        *,
        elapsed_ms: float | None,
        safe_url: str | None = None,
    ) -> ExternalCall:
        return ExternalCall(
            tool="clinicaltrials_v2_search",
            source="external_api",
            status="error",
            summary_text=f"ClinicalTrials MCP 조회 실패: {reason}. 외부 임상 근거를 생성하지 않습니다.",
            render_data={
                "request": {"query.intr": query_intr},
                "payload": {"studies": []},
                "error": reason,
                "external_claim_policy": "fail_closed_error",
                "message": "ClinicalTrials MCP 조회 실패",
            },
            safe_url=safe_url,
            elapsed_ms=elapsed_ms,
        )

    def _live_get(self, tool: str, params: dict[str, str], xml: bool = False) -> ExternalCall:
        spec = self.fixtures[tool]["live"]
        query = self._live_query(spec, params)
        if spec.get("requires_service_key"):
            key = os.environ.get(DATA_GO_KR_KEY_ENV)
            if not key:
                raise RuntimeError(f"{DATA_GO_KR_KEY_ENV} is required for live {tool}")
            query["serviceKey"] = key
        url = self._url_with_query(spec["url"], query)
        start = time.monotonic()
        last_error: Exception | None = None
        for _ in range(2):
            try:
                response = requests.get(url, timeout=self.timeout_s)
                elapsed = round((time.monotonic() - start) * 1000, 1)
                response.raise_for_status()
                payload = parse_response(response, xml or spec.get("format") == "xml")
                return ExternalCall(
                    tool=tool,
                    source="external_api",
                    status="live",
                    summary_text=summary(tool, response.status_code, payload),
                    render_data={**render_payload(payload), "request": params},
                    safe_url=self.redact_url(url),
                    elapsed_ms=elapsed,
                )
            except Exception as exc:
                last_error = exc
                time.sleep(0.2)
        elapsed = round((time.monotonic() - start) * 1000, 1)
        error_text = self.redact_url(str(last_error)) if last_error else "unknown"
        return ExternalCall(
            tool=tool,
            source="external_api",
            status="error",
            summary_text=f"{tool} failed: {error_text}",
            render_data={"error": error_text, "request": params},
            safe_url=self.redact_url(url),
            elapsed_ms=elapsed,
        )

    @staticmethod
    def _live_query(spec: dict[str, Any], params: dict[str, str]) -> dict[str, str]:
        mapped: dict[str, str] = {}
        for key, value in spec.get("default_params", {}).items():
            mapped[key] = str(value)
        param_map = spec.get("param_map", {})
        for key, value in params.items():
            target = param_map.get(key, key)
            if target:
                mapped[target] = value
        return mapped

    @staticmethod
    def _url_with_query(url: str, query: dict[str, str]) -> str:
        parts = urlsplit(url)
        existing = dict(parse_qsl(parts.query, keep_blank_values=True))
        existing.update(query)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query and urlencode(existing) or urlencode(existing), parts.fragment))


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
    key = match.group("key")
    value = match.group("value").strip().strip('"')
    if key in {"clinicaltrials_url", "nctId", "title", "status", "studyType", "sponsor", "startDate", "url"}:
        target[key] = value
    elif key in {"phase", "conditions"}:
        target[key] = [part.strip() for part in value.split(",") if part.strip()]


def _clinical_study_from_flat(flat: dict[str, Any]) -> dict[str, Any]:
    nct_id = str(flat.get("nctId") or "")
    title = str(flat.get("title") or "")
    status = str(flat.get("status") or "")
    phases = flat.get("phase") if isinstance(flat.get("phase"), list) else []
    interventions = flat.get("interventions") if isinstance(flat.get("interventions"), list) else []
    study = {
        "NCTId": nct_id,
        "briefTitle": title,
        "overallStatus": status,
        "url": flat.get("url") or flat.get("clinicaltrials_url"),
        "protocolSection": {
            "identificationModule": {"nctId": nct_id, "briefTitle": title},
            "statusModule": {"overallStatus": status, "startDate": flat.get("startDate")},
            "designModule": {"phases": phases, "studyType": flat.get("studyType")},
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
    return ExternalCall(
        tool="web_search",
        source=WEB_SEARCH_SOURCE,
        status="error",
        summary_text=f"{provider} 웹검색 실패: {str(exc)}",
        render_data={
            "provider": provider,
            "query": query,
            "items": [],
            "message": "웹검색 실패",
            "error": str(exc),
            "external_claim_policy": "web_results_unverified",
        },
        elapsed_ms=elapsed,
    )


def _web_call(provider: str, query: str, items: list[dict[str, Any]], elapsed: float) -> ExternalCall:
    status = "live" if items else "no_data"
    summary = f"{provider} 웹검색 결과 {len(items)}건을 확인했습니다." if items else f"{provider} 웹검색 결과가 없습니다."
    return ExternalCall(
        tool="web_search",
        source=WEB_SEARCH_SOURCE,
        status=status,
        summary_text=summary,
        render_data={
            "provider": provider,
            "query": query,
            "items": items,
            "external_claim_policy": "web_results_unverified",
            "verification_notice": "웹 검색 결과(미검증): URL과 snippet을 출처로 분리 표시하고 내부 fact로 승격하지 않습니다.",
        },
        elapsed_ms=elapsed,
    )
