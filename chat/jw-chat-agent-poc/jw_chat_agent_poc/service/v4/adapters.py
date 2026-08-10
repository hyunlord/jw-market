from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from jw_chat_agent_poc.service.v4.contracts import Citation, SourceName, SourceResult


def build_source_adapters() -> dict[SourceName, Any]:
    from jw_chat_agent_poc.agent_loop.factory import build_chat_agent_dependencies
    from jw_chat_agent_poc.service.general_view_routing import GeneralRoute, GeneralViewService

    dependencies = build_chat_agent_dependencies(external_mode="live")
    external = dependencies.external
    general_view = GeneralViewService.from_env(dependencies.resolver)

    def external_call(source: SourceName, query: str, call) -> SourceResult:
        started_at = datetime.now(UTC)
        result = call(query)
        payload = asdict(result)
        status = (
            "ok"
            if result.status not in {"error", "no_data", "unsupported"} and _has_payload(payload)
            else "empty"
        )
        citation = Citation(
            source=result.source or source,
            query=query,
            url=result.safe_url,
            retrieved_at=started_at,
            used=False,
        )
        return SourceResult(
            source=source,
            query=query,
            status=status,
            payload=payload,
            citations=(citation,),
            notice=None if status == "ok" else result.summary_text,
        )

    def mart(query: str) -> SourceResult:
        started_at = datetime.now(UTC)
        payloads: list[dict[str, Any]] = []
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

    return {
        "mart": mart,
        "nedrug": lambda query: external_call("nedrug", query, external.mfds_permission_search),
        "hira": lambda query: external_call("hira", query, external.hira_disease_name_code),
        "openfda": lambda query: external_call("openfda", query, external.openfda_label_search),
        "clinicaltrials": lambda query: external_call(
            "clinicaltrials", query, external.clinicaltrials_v2_search
        ),
        "web": lambda query: external_call("web", query, external.web_search),
        "patent": lambda query: external_call(
            "patent", f"{query} 특허 만료 공식", external.web_search
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
    for metric in dict.fromkeys(metrics):
        try:
            calls.append(layer.brand_metric(brand, metric, "latest"))
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
