from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from typing import Any, Mapping, Protocol

import requests

from jw_chat_agent_poc.genos_config import DEFAULT_GENOS_BASE_URL, resolve_genos_base_url
from jw_chat_agent_poc.portfolio_scope import portfolio_scope_for_question
from .bq_router import BQRouter, BQSubQuestion, BQ_SYSTEM_PROMPT, _is_forecast_question
from .llm_filter_entries import filters_for_sources
from .llm_route_helpers import (
    bool_value,
    confidence,
    first_valid_bq,
    parse_json_object,
    question_label,
    string_items,
    valid_bq_ids,
    valid_scope,
    valid_sources,
)
from .llm_router_prompts import build_system_prompt


NO_DATA_KEYWORDS = (
    "영업활동",
    "영업 활동",
    "영업 impact",
    "영업 Impact",
    "포트폴리오",
    "사업성",
    "신사업",
    "사업 타당성",
)
DEFAULT_BASE_URL = DEFAULT_GENOS_BASE_URL


class BQDecomposer(Protocol):
    def decompose(self, question: str, has_documents: bool) -> str: ...


@dataclass(frozen=True, slots=True)
class RouteDiagnostics:
    mode: str
    fallback_used: bool
    reason: str
    raw_output: str = ""
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class GenosBQDecomposer:
    base_url: str = field(default_factory=resolve_genos_base_url)
    token: str | None = field(default_factory=lambda: os.environ.get("GENOS_BEARER_TOKEN") or os.environ.get("GENOS_TOKEN"))
    timeout_s: int = field(default_factory=lambda: int(os.environ.get("GENOS_ROUTER_TIMEOUT_S", "30")))

    def decompose(self, question: str, has_documents: bool) -> str:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        response = requests.post(
            f"{self.base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json={
                "messages": [
                    {"role": "system", "content": build_system_prompt(has_documents)},
                    {"role": "user", "content": question},
                ],
                "stream": False,
                "temperature": 0.0,
            },
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        payload = response.json()
        return self._extract_content(payload)

    @staticmethod
    def _extract_content(payload: Mapping[str, Any]) -> str:
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict) and isinstance(message.get("content"), str):
                    return message["content"]
                text = first.get("text")
                if isinstance(text, str):
                    return text
        content = payload.get("content")
        if isinstance(content, str):
            return content
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("content"), str):
            return data["content"]
        raise RuntimeError("GenOS serving response did not contain message content")  # noqa: GENERIC_ERR_OK


class LLMFirstBQRouter:
    def __init__(
        self,
        decomposer: BQDecomposer | None = None,
        keyword_router: BQRouter | None = None,
        confidence_threshold: float = 0.55,
    ) -> None:
        self._decomposer = decomposer or GenosBQDecomposer()
        self._keyword_router = keyword_router or BQRouter()
        self._confidence_threshold = confidence_threshold
        self.last_diagnostics = RouteDiagnostics(mode="keyword", fallback_used=False, reason="not_called")

    @property
    def system_prompt(self) -> str:
        return BQ_SYSTEM_PROMPT

    def route(self, question: str, has_documents: bool = False) -> list[BQSubQuestion]:
        boundary = self._boundary_route(question, has_documents)
        if boundary:
            self.last_diagnostics = RouteDiagnostics(mode="guard", fallback_used=False, reason="no_data_boundary")
            return boundary
        try:
            raw = self._decomposer.decompose(question, has_documents)
            routes, confidence = self._parse_routes(raw, question, has_documents)
        except (RuntimeError, requests.RequestException, json.JSONDecodeError, TypeError, KeyError) as exc:
            return self._fallback(question, has_documents, f"llm_failed:{type(exc).__name__}")

        if confidence is not None and confidence < self._confidence_threshold:
            return self._fallback(question, has_documents, f"low_confidence:{confidence:.2f}", raw, confidence)
        if not routes or not any(source != "none" for route in routes for source in route.sources):
            return self._fallback(question, has_documents, "empty_or_no_action", raw, confidence)
        self.last_diagnostics = RouteDiagnostics(
            mode="llm",
            fallback_used=False,
            reason="ok",
            raw_output=raw,
            confidence=confidence,
        )
        return routes

    def _fallback(
        self,
        question: str,
        has_documents: bool,
        reason: str,
        raw_output: str = "",
        confidence: float | None = None,
    ) -> list[BQSubQuestion]:
        routes = self._keyword_router.route(question, has_documents)
        self.last_diagnostics = RouteDiagnostics(
            mode="keyword",
            fallback_used=True,
            reason=reason,
            raw_output=raw_output,
            confidence=confidence,
        )
        return routes

    @classmethod
    def _boundary_route(cls, question: str, has_documents: bool = False) -> list[BQSubQuestion]:
        lower = question.lower()
        if "영업활동" in question or "영업 활동" in question or "영업 impact" in lower:
            return [
                BQSubQuestion(
                    bq="Q4",
                    question="영업 Impact",
                    sources=("none",),
                    reason="LLM-first guard preserves Q4 데이터 없음 boundary.",
                )
            ]
        if not has_documents and _is_forecast_question(question):
            return [
                BQSubQuestion(
                    bq="Q1",
                    question="시장정의·규모·성장예측",
                    sources=("none",),
                    reason="LLM-first guard preserves forecast 데이터 없음 boundary.",
                )
            ]
        if any(token in question for token in ("포트폴리오", "사업성", "신사업", "사업 타당성")) and portfolio_scope_for_question(question) != "portfolio":
            return [
                BQSubQuestion(
                    bq="Q5",
                    question="포트폴리오/사업성",
                    sources=("none",),
                    reason="LLM-first guard preserves Q5 포트폴리오·사업성 boundary.",
                )
            ]
        return []

    def _parse_routes(self, raw: str, question: str, has_documents: bool) -> tuple[list[BQSubQuestion], float | None]:
        data = parse_json_object(raw)
        if bool_value(data.get("no_data_flag")):
            return (
                [
                    BQSubQuestion(
                        bq=first_valid_bq(data.get("bq_ids")) or "Q5",
                        question=str(data.get("reason") or question),
                        sources=("none",),
                        reason="LLM marked this question as outside available data.",
                    )
                ],
                confidence(data),
            )
        bq_ids = valid_bq_ids(data.get("bq_ids"))
        sources = valid_sources(data.get("tools"), question, has_documents)
        if has_documents and "document" in sources and {"Q1", "Q5"}.issubset(set(bq_ids)):
            bq_ids = ("Q1/Q5",)
        if has_documents and "document" not in sources:
            sources = (*sources, "document")
        if not bq_ids or not sources:
            return [], confidence(data)
        reason = str(data.get("reason") or "LLM BQ decomposition")
        filters = filters_for_sources(data, question, sources)
        brands = string_items(data.get("brands"))
        scope = valid_scope(data.get("scope"))
        return (
            [
                BQSubQuestion(
                    bq=bq,
                    question=question_label(bq),
                    sources=sources,
                    reason=reason,
                    filters=filters,
                    brands=brands,
                    scope=scope,
                )
                for bq in bq_ids
            ],
            confidence(data),
        )
