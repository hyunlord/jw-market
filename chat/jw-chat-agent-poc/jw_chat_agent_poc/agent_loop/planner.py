from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from typing import Any, Mapping

from jw_chat_agent_poc.agent_loop.models import AgentDecision, AgentObservation, ToolCallPlan, ToolPlanner
from jw_chat_agent_poc.agent_loop.news_query import normalize_news_query
from jw_chat_agent_poc.genos_config import resolve_planner_genos_base_url, resolve_planner_genos_token
from jw_chat_agent_poc.common.token_usage import usage_call_from_payload
from jw_chat_agent_poc.tools.external import resolve_patent_ingredient_query


@dataclass(frozen=True, slots=True)
class GenosToolPlanner:
    fallback: ToolPlanner | None = None
    base_url: str = field(default_factory=resolve_planner_genos_base_url)
    token: str | None = field(default_factory=resolve_planner_genos_token)
    timeout_s: int = field(default_factory=lambda: int(os.environ.get("GENOS_AGENT_TIMEOUT_S", "30")))
    last_token_usage: dict[str, Any] | None = field(default=None, init=False, repr=False, compare=False)

    def decide(
        self,
        question: str,
        observations: tuple[AgentObservation, ...],
        schemas: tuple[dict[str, Any], ...],
        allowed_brands: tuple[str, ...] = (),
        allowed_periods: tuple[str, ...] = (),
    ) -> AgentDecision:
        object.__setattr__(self, "last_token_usage", None)
        deterministic_external = _deterministic_external_decision(question, observations, allowed_brands, allowed_periods)
        if deterministic_external is not None:
            return deterministic_external
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
                    "tools": list(_planner_schemas(question, schemas, observations)),
                    "tool_choice": "auto",
                    "stream": False,
                    "temperature": 0.0,
                    "max_tokens": _planner_max_tokens(),
                },
                timeout=self.timeout_s,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise RuntimeError("GenOS tool planner request failed") from exc
        payload = response.json()
        object.__setattr__(self, "last_token_usage", usage_call_from_payload(payload, base_url=self.base_url, stream=False))
        return _decision_from_payload(payload)

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


def _deterministic_external_decision(
    question: str,
    observations: tuple[AgentObservation, ...],
    allowed_brands: tuple[str, ...],
    allowed_periods: tuple[str, ...],
) -> AgentDecision | None:
    """Run explicit external/API intents instead of letting the planner skip them.

    The LLM planner is useful for ambiguous metric decomposition, but for
    external source questions the safe default is to call the explicit source
    tool and render only returned payload fields. This keeps metric fallback
    from replacing unsupported external questions.
    """
    if observations:
        return None
    external_intent = _asks_clinical(question) or _asks_patent(question) or _asks_web_search(question) or _asks_hira_procedure(question)
    if not external_intent:
        return None
    calls = _expanded_tool_calls(question, allowed_brands, allowed_periods)
    external_calls = tuple(
        call
        for call in calls
        if call.name in {"search_clinical", "search_patent", "web_search", "get_procedure_stats"}
        or (call.name in {"get_metric", "get_brand_sales", "get_brand_share", "get_brand_series"} and _asks_explicit_metric(question))
    )
    if not external_calls:
        return None
    return AgentDecision(tool_calls=external_calls)


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
        {"role": "assistant", "content": json.dumps(_summarize_observations(observations), ensure_ascii=False)},
    ]


_METRIC_TOOL_NAMES = (
    "get_brand_series",
    "get_brand_sales",
    "get_brand_share",
    "get_metric",
    "query",
    "compare_brands_series",
    "get_top_brands",
    "get_brand_channel_breakdown",
    "get_brand_specialty_breakdown",
)
_MARKET_TOOL_NAMES = ("get_market_scope",)
_RELATIVE_DATE_TOOL_NAMES = ("resolve_relative_date",)
_NEWS_TOOL_NAMES = ("search_news",)
_HIRA_TOOL_NAMES = ("get_disease_stats",)
_HIRA_PROCEDURE_TOOL_NAMES = ("get_procedure_stats",)
_CLINICAL_TOOL_NAMES = ("search_clinical",)
_PATENT_TOOL_NAMES = ("search_patent",)
_DRUG_INFO_TOOL_NAMES = ("search_drug_info",)
_WEB_SEARCH_TOOL_NAMES = ("web_search",)
_CORE_OBSERVATION_KEYS = (
    "brand",
    "metric",
    "period",
    "source_label",
    "sales_억원",
    "sales_delta_억원",
    "sales_delta_pct",
    "ms_recent_pct",
    "share_delta_pct",
    "rank",
    "market_size_억원",
    "query_result_id",
    "data_scope",
    "status",
    "message",
)
_SERIES_OBSERVATION_KEYS = (
    "brand_value_series_10pt",
    "level_top5_trend_series",
    "rows",
    "series",
)


def _planner_max_tokens() -> int:
    try:
        return int(os.environ.get("GENOS_PLANNER_MAX_TOKENS", "512"))
    except ValueError:
        return 512


def _planner_schemas(
    question: str,
    schemas: tuple[dict[str, Any], ...],
    observations: tuple[AgentObservation, ...],
) -> tuple[dict[str, Any], ...]:
    selected = select_candidate_tools(question, schemas, observations)
    return tuple(compact_tool_schema(schema) for schema in selected)


def select_candidate_tools(
    question: str,
    schemas: tuple[dict[str, Any], ...],
    observations: tuple[AgentObservation, ...],
) -> tuple[dict[str, Any], ...]:
    """Return a conservative tool subset for the planner prompt.

    The planner still decides which tool to call; this only removes clearly
    unrelated schema prose. When the intent is ambiguous, keep the full set.
    """
    by_name = {_schema_name(schema): schema for schema in schemas if _schema_name(schema)}
    if not by_name:
        return schemas

    names: list[str] = []
    if _asks_news(question):
        names.extend(_NEWS_TOOL_NAMES)
    if _asks_hira(question):
        names.extend(_HIRA_TOOL_NAMES)
    if _asks_hira_procedure(question):
        names.extend(_HIRA_PROCEDURE_TOOL_NAMES)
    if _asks_clinical(question):
        names.extend(_CLINICAL_TOOL_NAMES)
    if _asks_patent(question):
        names.extend(_PATENT_TOOL_NAMES)
    if _asks_drug_info(question):
        names.extend(_DRUG_INFO_TOOL_NAMES)
    if _asks_web_search(question):
        names.extend(_WEB_SEARCH_TOOL_NAMES)
    if _asks_market_scope(question):
        names.extend(_MARKET_TOOL_NAMES)
    if _asks_relative_date(question):
        names.extend(_RELATIVE_DATE_TOOL_NAMES)
    external_intent = _asks_clinical(question) or _asks_patent(question) or _asks_web_search(question) or _asks_hira_procedure(question)
    metric_context_allowed = not external_intent or _asks_explicit_metric(question)
    if (metric_context_allowed and _asks_metric_or_analysis(question)) or not names:
        names.extend(_METRIC_TOOL_NAMES)

    if observations and _has_successful_metric_observation(observations) and not _needs_external_context(question):
        names = [name for name in names if name in {*_METRIC_TOOL_NAMES, "query"}]

    deduped = tuple(dict.fromkeys(name for name in names if name in by_name))
    if len(deduped) < 2 and not external_intent:
        return schemas
    return tuple(by_name[name] for name in deduped)


def compact_tool_schema(schema: dict[str, Any]) -> dict[str, Any]:
    function = schema.get("function") if isinstance(schema.get("function"), dict) else {}
    name = str(function.get("name") or "")
    compact: dict[str, Any] = {
        "type": schema.get("type", "function"),
        "function": {
            "name": name,
            "description": _shorten(str(function.get("description") or ""), 360),
        },
    }
    parameters = function.get("parameters")
    if isinstance(parameters, dict):
        compact["function"]["parameters"] = _compact_parameters(parameters)
    return compact


def truncate_observation_message(observations: tuple[AgentObservation, ...]) -> list[dict[str, Any]]:
    return _summarize_observations(observations)


def _summarize_observations(observations: tuple[AgentObservation, ...]) -> list[dict[str, Any]]:
    return [_summarize_observation(item) for item in observations]


def _summarize_observation(item: AgentObservation) -> dict[str, Any]:
    call = item.call if isinstance(item.call, dict) else {}
    summary: dict[str, Any] = {
        "step": item.step,
        "tool_name": item.tool_name,
        "status": item.status,
        "arguments": dict(item.arguments),
        "preview": _shorten(item.preview, 240),
    }
    if call:
        compact_call: dict[str, Any] = {
            "tool": call.get("tool"),
            "source": call.get("source"),
            "summary_text": _shorten(str(call.get("summary_text") or ""), 360),
        }
        render_data = call.get("render_data")
        if isinstance(render_data, dict):
            compact_call["render_data"] = _compact_render_data(render_data)
        summary["call"] = compact_call
    return summary


def _compact_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {"type": parameters.get("type", "object")}
    properties = parameters.get("properties")
    if isinstance(properties, dict):
        compact["properties"] = {
            str(name): _compact_property(prop)
            for name, prop in properties.items()
            if isinstance(prop, dict)
        }
    if isinstance(parameters.get("required"), list):
        compact["required"] = parameters["required"]
    if parameters.get("additionalProperties") is False:
        compact["additionalProperties"] = False
    return compact


def _compact_property(prop: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in ("type", "enum", "items", "default"):
        if key in prop:
            compact[key] = prop[key]
    if "description" in prop:
        compact["description"] = _shorten(str(prop.get("description") or ""), 160)
    return compact


def _compact_render_data(data: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {key: data[key] for key in _CORE_OBSERVATION_KEYS if key in data}
    for key in _SERIES_OBSERVATION_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            compact[key] = _compact_sequence(value)
        elif isinstance(value, tuple):
            compact[key] = _compact_sequence(list(value))
    return compact


def _compact_sequence(items: list[Any]) -> dict[str, Any]:
    if len(items) <= 4:
        sample = items
    else:
        sample = [items[0], items[1], items[-2], items[-1]]
    return {"count": len(items), "sample": sample}


def _schema_name(schema: dict[str, Any]) -> str:
    function = schema.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "")
    return ""


def _shorten(text: str, limit: int) -> str:
    stripped = " ".join(text.split())
    return stripped if len(stripped) <= limit else stripped[: limit - 1].rstrip() + "…"


def _asks_metric_or_analysis(question: str) -> bool:
    return any(
        token in question
        for token in (
            "매출",
            "점유율",
            "MS",
            "순위",
            "시장",
            "추이",
            "변화",
            "증감",
            "하락",
            "상승",
            "impact",
            "Impact",
            "영업활동",
            "상기 콜",
            "채널",
            "Class",
            "Molecule",
            "브랜드",
            "용량",
            "제형",
        )
    )


def _asks_explicit_metric(question: str) -> bool:
    return any(token in question for token in ("매출", "점유율", "MS", "순위", "시장규모", "시장 규모"))


def _asks_news(question: str) -> bool:
    return any(token in question for token in ("뉴스", "이슈", "소식", "출시", "정책", "약가"))


def _asks_hira(question: str) -> bool:
    return any(token in question for token in ("환자수", "환자 수", "질병", "질환", "HIRA"))


def _asks_hira_procedure(question: str) -> bool:
    return any(token in question for token in ("진료행위", "행위코드", "수가코드", "검사", "수술", "입원외래", "기관종별", "요양기관종별"))


def _asks_clinical(question: str) -> bool:
    if _has_explicit_web_search_cue(question):
        return False
    return any(token in question for token in ("임상", "clinical", "연구", "study", "결과"))


def _asks_patent(question: str) -> bool:
    return any(token in question for token in ("특허", "독점권", "patent", "Orange", "orange", "라벨", "FDA"))


def _asks_market_scope(question: str) -> bool:
    return any(token in question for token in ("같은 시장", "경쟁제품", "경쟁 제품", "경쟁품", "경쟁 구도", "상위"))


def _asks_relative_date(question: str) -> bool:
    return "전" in question and "대비" in question


def _needs_external_context(question: str) -> bool:
    return _asks_news(question) or _asks_hira(question) or _asks_hira_procedure(question) or _asks_clinical(question) or _asks_patent(question) or _asks_drug_info(question) or _asks_web_search(question)


def _asks_web_search(question: str) -> bool:
    if _asks_patent(question) or _asks_drug_info(question) or _asks_hira(question) or _asks_hira_procedure(question):
        return False
    if _has_explicit_web_search_cue(question):
        return True
    if _asks_clinical(question):
        return False
    return any(
        token in question
        for token in (
            "디테일링",
            "상기되는",
            "KOL",
            "자문",
            "시장동향",
            "시장 동향",
            "트렌드",
            "경쟁제품의 최근",
            "경쟁 제품의 최근",
            "프로모션",
            "학회",
            "가이드라인",
        )
    )


def _has_explicit_web_search_cue(question: str) -> bool:
    return any(
        token in question
        for token in (
            "웹검색",
            "웹 검색",
            "검색해줘",
            "검색 결과",
            "URL",
            "snippet",
            "최신 동향",
            "최근 동향",
        )
    )


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
    if _asks_drug_info(question):
        return True
    return any(token in question for token in ("뉴스", "이슈", "환자", "질병", "질환", "HIRA", "진료행위", "행위코드", "수가코드", "검사", "수술", "임상", "특허", "라벨", "FDA", "디테일링", "KOL", "시장동향", "웹검색", "웹 검색", "검색해줘", "검색 결과", "최신 동향", "최근 동향"))


def _expanded_tool_calls(question: str, allowed_brands: tuple[str, ...], allowed_periods: tuple[str, ...]) -> tuple[ToolCallPlan, ...]:
    brand = _brand(question, allowed_brands)
    calls: list[ToolCallPlan] = []
    if any(token in question for token in ("뉴스", "이슈")):
        calls.append(ToolCallPlan("search_news", {"brand": brand, "query": _news_query(question)}, "뉴스/이슈 확인"))
    if any(token in question for token in ("환자", "질병", "질환", "HIRA")):
        calls.append(ToolCallPlan("get_disease_stats", {"brand": brand}, "HIRA 질병 통계 확인"))
    if _asks_hira_procedure(question):
        calls.append(ToolCallPlan("get_procedure_stats", {"brand": brand, "query": question}, "HIRA 진료행위 통계 확인"))
    if _asks_clinical(question):
        calls.append(ToolCallPlan("search_clinical", {"brand": brand}, "임상 근거 확인"))
    if _asks_drug_info(question):
        calls.append(ToolCallPlan("search_drug_info", {"brand": brand}, "식약처 허가정보 확인"))
    if _asks_patent(question):
        patent_args = {"query": question}
        ingredient = resolve_patent_ingredient_query(question)
        if ingredient and not any(allowed_brand in question for allowed_brand in allowed_brands):
            patent_args["ingredient"] = ingredient
        else:
            patent_args["brand"] = brand
        calls.append(ToolCallPlan("search_patent", patent_args, "특허/라벨 근거 확인"))
    if _asks_web_search(question):
        calls.append(ToolCallPlan("web_search", {"brand": brand, "query": question}, "웹 검색 결과 확인"))
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


def _asks_drug_info(question: str) -> bool:
    if any(token in question for token in ("허가", "품목", "식약처", "MFDS", "의약품정보", "의약품 정보")):
        return True
    if "성분" in question and not any(token in question for token in ("특허", "임상", "FDA", "라벨")):
        return True
    return any(token in question for token in ("효능", "용법")) and any(token in question for token in ("국내", "식약처", "허가"))


def _news_query(question: str) -> str:
    for token in ("아토젯", "약가", "임상", "특허"):
        if token in question:
            return normalize_news_query(token)
    return normalize_news_query(question)


def _has_tool(schemas: tuple[dict[str, Any], ...], name: str) -> bool:
    return any(schema.get("function", {}).get("name") == name for schema in schemas)


def _has_metric_observation(observations: tuple[AgentObservation, ...]) -> bool:
    return _has_successful_metric_observation(observations)


def _has_successful_metric_observation(observations: tuple[AgentObservation, ...]) -> bool:
    return any(
        item.status == "ok" and item.tool_name in {"get_metric", "get_brand_metric"}
        for item in observations
    )


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
