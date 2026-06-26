from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from typing import Any, Mapping

from jw_chat_agent_poc.agent_loop.models import AgentDecision, AgentObservation, ToolCallPlan, ToolPlanner
from jw_chat_agent_poc.agent_loop.news_query import normalize_news_query
from jw_chat_agent_poc.genos_config import resolve_planner_genos_base_url, resolve_planner_genos_token


@dataclass(frozen=True, slots=True)
class GenosToolPlanner:
    fallback: ToolPlanner | None = None
    base_url: str = field(default_factory=resolve_planner_genos_base_url)
    token: str | None = field(default_factory=resolve_planner_genos_token)
    timeout_s: int = field(default_factory=lambda: int(os.environ.get("GENOS_AGENT_TIMEOUT_S", "30")))

    def decide(
        self,
        question: str,
        observations: tuple[AgentObservation, ...],
        schemas: tuple[dict[str, Any], ...],
        allowed_brands: tuple[str, ...] = (),
        allowed_periods: tuple[str, ...] = (),
    ) -> AgentDecision:
        if not self.token:
            return self._fallback(question, observations, schemas, allowed_brands, allowed_periods)
        try:
            return self._request_decision(question, observations, schemas, allowed_brands, allowed_periods)
        except (RuntimeError, json.JSONDecodeError, TypeError, KeyError, ValueError):
            return self._fallback(question, observations, schemas, allowed_brands, allowed_periods)

    def _request_decision(
        self,
        question: str,
        observations: tuple[AgentObservation, ...],
        schemas: tuple[dict[str, Any], ...],
        allowed_brands: tuple[str, ...],
        allowed_periods: tuple[str, ...],
    ) -> AgentDecision:
        import requests

        try:
            response = requests.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.token}"},
                json={
                    "messages": _messages(question, observations, allowed_brands, allowed_periods),
                    "tools": list(schemas),
                    "tool_choice": "auto",
                    "stream": False,
                    "temperature": 0.0,
                },
                timeout=self.timeout_s,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError("GenOS tool planner request failed") from exc
        return _decision_from_payload(response.json())

    def _fallback(
        self,
        question: str,
        observations: tuple[AgentObservation, ...],
        schemas: tuple[dict[str, Any], ...],
        allowed_brands: tuple[str, ...],
        allowed_periods: tuple[str, ...],
    ) -> AgentDecision:
        if self.fallback is None:
            return AgentDecision(final_answer="도구 호출 없이 기존 경로로 처리하세요.")
        return self.fallback.decide(question, observations, schemas, allowed_brands, allowed_periods)


class HeuristicToolPlanner:
    def decide(
        self,
        question: str,
        observations: tuple[AgentObservation, ...],
        schemas: tuple[dict[str, Any], ...],
        allowed_brands: tuple[str, ...] = (),
        allowed_periods: tuple[str, ...] = (),
    ) -> AgentDecision:
        if not observations and "상위" in question and _has_tool(schemas, "get_top_brands"):
            return AgentDecision(tool_calls=(ToolCallPlan("get_top_brands", {"brand": _brand(question, allowed_brands), "limit": "5"}, "시장 상위 브랜드 확인"),))
        if not observations and _needs_expanded_tools(question):
            return AgentDecision(tool_calls=_expanded_tool_calls(question, allowed_brands, allowed_periods))
        if not observations and "같은 시장" in question:
            return AgentDecision(tool_calls=(ToolCallPlan("get_market_scope", {"brand": _brand(question, allowed_brands), "view": "market_landscape"}, "시장 scope 확인"),))
        if not observations and "대비" in question:
            return AgentDecision(tool_calls=(ToolCallPlan("resolve_relative_date", {"expression": _relative_expression(question)}, "비교 기간 해석"),))
        if "같은 시장" in question and not _has_metric_observation(observations):
            brands = _observed_members(observations) or (_brand(question, allowed_brands),)
            return AgentDecision(
                tool_calls=tuple(ToolCallPlan("get_metric", {"brand": brand, "measure": "sales", "period": "previous_year"}, "같은 시장 브랜드 작년 매출") for brand in brands)
            )
        if "대비" in question and _has_date_observation(observations) and not _has_metric_observation(observations):
            period = _observed_period(observations)
            return AgentDecision(
                tool_calls=(
                    ToolCallPlan("get_metric", {"brand": _brand(question, allowed_brands), "measure": "market_share", "period": period}, "비교 시점 점유율"),
                    ToolCallPlan("get_metric", {"brand": _brand(question, allowed_brands), "measure": "market_share", "period": _latest_period(allowed_periods)}, "최신 점유율"),
                )
            )
        return AgentDecision(final_answer="도구 결과로 답변하세요.")


def _messages(
    question: str,
    observations: tuple[AgentObservation, ...],
    allowed_brands: tuple[str, ...],
    allowed_periods: tuple[str, ...],
) -> list[dict[str, str]]:
    brand_hint = _brand_hint(allowed_brands)
    period_hint = _period_hint(allowed_periods)
    return [
        {
            "role": "system",
            "content": (
                "You choose only the provided tools. Do not invent numbers. "
                "Stop when enough cache facts exist. "
                "For any brand argument, use only the canonical brand enum provided by the tool schema. "
                "Never invent, translate, or typo-correct Korean brand names yourself. "
                "For any period argument, use only the period enum provided by the tool schema. "
                "Never invent unavailable months such as future or not-yet-loaded months. "
                f"{brand_hint} {period_hint}"
            ),
        },
        {"role": "user", "content": question},
        {"role": "assistant", "content": json.dumps([item.to_dict() for item in observations], ensure_ascii=False)},
    ]


def _decision_from_payload(payload: Mapping[str, Any]) -> AgentDecision:
    message = _message(payload)
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        return AgentDecision(tool_calls=tuple(_tool_call(item) for item in tool_calls if isinstance(item, dict)))
    content = message.get("content")
    if isinstance(content, str) and content.strip().startswith("{"):
        parsed = json.loads(content)
        return _decision_from_json(parsed)
    return AgentDecision(final_answer=content if isinstance(content, str) and content else "도구 결과로 답변하세요.")


def _message(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            return message
    raise RuntimeError("GenOS tool planner response did not contain message")


def _tool_call(raw: Mapping[str, Any]) -> ToolCallPlan:
    function = raw.get("function")
    if not isinstance(function, dict):
        raise TypeError("tool_call.function must be an object")
    args = function.get("arguments")
    parsed = json.loads(args) if isinstance(args, str) else args
    if not isinstance(parsed, dict):
        raise TypeError("tool arguments must be an object")
    return ToolCallPlan(name=str(function.get("name") or ""), arguments={str(key): str(value) for key, value in parsed.items()}, reason=str(raw.get("reason") or ""))


def _decision_from_json(raw: Mapping[str, Any]) -> AgentDecision:
    items = raw.get("tool_calls")
    if isinstance(items, list) and items:
        return AgentDecision(tool_calls=tuple(_json_tool_call(item) for item in items if isinstance(item, dict)))
    final = raw.get("final_answer")
    return AgentDecision(final_answer=str(final) if final else "도구 결과로 답변하세요.")


def _json_tool_call(raw: Mapping[str, Any]) -> ToolCallPlan:
    args = raw.get("arguments") if isinstance(raw.get("arguments"), dict) else {}
    return ToolCallPlan(name=str(raw.get("name") or ""), arguments={str(key): str(value) for key, value in args.items()}, reason=str(raw.get("reason") or ""))


def _brand(question: str, allowed_brands: tuple[str, ...]) -> str:
    if allowed_brands:
        return allowed_brands[0]
    return "리바로젯" if "리바로젯" in question else "리바로"


def _brand_hint(allowed_brands: tuple[str, ...]) -> str:
    if not allowed_brands:
        return "No brand has been pre-resolved yet; if a brand tool is needed, use an exact canonical brand from observations only."
    return "Pre-resolved canonical brand enum: " + ", ".join(allowed_brands) + "."


def _period_hint(allowed_periods: tuple[str, ...]) -> str:
    if not allowed_periods:
        return "No period enum has been provided yet; avoid period arguments unless a tool resolves them."
    months = tuple(period for period in allowed_periods if "-" in period)
    latest = months[-1] if months else "latest"
    return f"Available monthly data period enum ends at {latest}; use latest or an enum value only."


def _relative_expression(question: str) -> str:
    import re

    match = re.search(r"\d{1,2}\s*(?:달|개월)\s*전", question)
    return match.group(0) if match else "3달전"


def _needs_expanded_tools(question: str) -> bool:
    if _asks_series_metric(question):
        return True
    return any(token in question for token in ("뉴스", "이슈", "환자", "질병", "질환", "HIRA", "임상", "특허", "라벨", "FDA"))


def _expanded_tool_calls(question: str, allowed_brands: tuple[str, ...], allowed_periods: tuple[str, ...]) -> tuple[ToolCallPlan, ...]:
    brand = _brand(question, allowed_brands)
    calls: list[ToolCallPlan] = []
    if any(token in question for token in ("뉴스", "이슈")):
        calls.append(ToolCallPlan("search_news", {"brand": brand, "query": _news_query(question)}, "뉴스/이슈 확인"))
    if any(token in question for token in ("환자", "질병", "질환", "HIRA")):
        calls.append(ToolCallPlan("get_disease_stats", {"brand": brand}, "HIRA 질병 통계 확인"))
    if "임상" in question:
        calls.append(ToolCallPlan("search_clinical", {"brand": brand}, "임상 근거 확인"))
    if any(token in question for token in ("특허", "라벨", "FDA")):
        calls.append(ToolCallPlan("search_patent", {"brand": brand}, "특허/라벨 근거 확인"))
    if _asks_series_metric(question) or any(token in question for token in ("매출", "점유율", "순위", "시장")):
        measure = (
            "series"
            if _asks_series_metric(question) or _asks_patient_sales_context(question)
            else ("market_share" if any(token in question for token in ("점유율", "순위")) else "sales")
        )
        calls.append(ToolCallPlan("get_metric", {"brand": brand, "measure": measure, "period": "latest"}, "지표 확인"))
    return tuple(calls)


def _asks_sales_change(question: str) -> bool:
    return "매출" in question and any(token in question for token in ("변화", "증감", "추이", "하락"))


def _asks_series_metric(question: str) -> bool:
    if _asks_sales_change(question):
        return True
    if "점유율" in question and any(token in question for token in ("변화", "추이", "비교", "오르는", "동안")):
        return True
    return any(token in question for token in ("경쟁 구도", "위협"))


def _asks_patient_sales_context(question: str) -> bool:
    return "매출" in question and any(token in question for token in ("환자", "환자수", "질병", "질환", "HIRA"))


def _news_query(question: str) -> str:
    for token in ("아토젯", "약가", "임상", "특허"):
        if token in question:
            return normalize_news_query(token)
    return normalize_news_query(question)


def _has_tool(schemas: tuple[dict[str, Any], ...], name: str) -> bool:
    return any(schema.get("function", {}).get("name") == name for schema in schemas)


def _has_metric_observation(observations: tuple[AgentObservation, ...]) -> bool:
    return any(item.tool_name == "get_metric" for item in observations)


def _has_date_observation(observations: tuple[AgentObservation, ...]) -> bool:
    return any(item.tool_name == "resolve_relative_date" for item in observations)


def _observed_period(observations: tuple[AgentObservation, ...]) -> str:
    for item in observations:
        data = (item.call or {}).get("render_data", {})
        if item.tool_name == "resolve_relative_date" and isinstance(data, dict):
            return str(data.get("period") or "latest")
    return "latest"


def _latest_period(allowed_periods: tuple[str, ...]) -> str:
    months = tuple(period for period in allowed_periods if "-" in period)
    return months[-1] if months else "latest"


def _observed_members(observations: tuple[AgentObservation, ...]) -> tuple[str, ...]:
    for item in observations:
        data = (item.call or {}).get("render_data", {})
        members = data.get("member_brands") if isinstance(data, dict) else None
        if isinstance(members, tuple | list):
            return tuple(str(member) for member in members)
    return ()
