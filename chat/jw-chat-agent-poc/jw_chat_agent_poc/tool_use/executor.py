from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
import json
import logging

from pydantic import BaseModel, ValidationError
import requests

from jw_chat_agent_poc.tool_use.contracts import AgentResult, FallbackCode, ToolEnvelope, ToolTrace
from jw_chat_agent_poc.tool_use.ledger import EvidenceLedger
from jw_chat_agent_poc.tool_use.provider import ToolChoiceProvider, ToolProviderConfigurationError
from jw_chat_agent_poc.tool_use.renderer import render_evidence_answer
from jw_chat_agent_poc.tool_use.specs import ToolSpec


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AgentExecutor:
    provider: ToolChoiceProvider
    max_steps: int = 4

    def run(self, *, user_text: str, tools: tuple[ToolSpec, ...]) -> AgentResult:
        ledger = EvidenceLedger()
        traces: list[ToolTrace] = []
        tool_calls: list[dict] = []
        messages: list[dict] = [
            {
                "role": "system",
                "content": (
                    "필요한 경우에만 tool 을 호출한다. 수치, 날짜, 점유율은 tool 근거가 없으면 추정하지 않는다. "
                    "내부 테이블명, 캐시 키, 시스템 용어를 노출하지 않는다. 근거가 없으면 확인 불가라고 답한다."
                ),
            },
            {"role": "user", "content": user_text},
        ]
        by_name = {tool.name: tool for tool in tools}
        for step in range(1, self.max_steps + 1):
            try:
                choice = self.provider.choose(user_text=user_text, messages=messages, tools=[tool.openai_schema() for tool in tools])
            except requests.Timeout:
                return _terminal("tool timeout", FallbackCode.TOOL_TIMEOUT, traces, tool_calls)
            except (ToolProviderConfigurationError, requests.RequestException, KeyError, TypeError, ValueError) as exc:
                LOGGER.warning("tool-use provider schema invalid: %s", exc)
                return _terminal("provider schema invalid", FallbackCode.SCHEMA_INVALID, traces, tool_calls)
            if choice.name is None:
                code = None if ledger.is_complete() else FallbackCode.UNSUPPORTED_QUERY
                status = "ok" if ledger.is_complete() else "unsupported"
                answer = render_evidence_answer(tuple(ledger.facts)) if ledger.is_complete() else "이 질문에 맞는 도구가 없습니다."
                trace_message = "evidence complete" if ledger.is_complete() else "no matching tool"
                traces.append(ToolTrace(step=step, tool=None, status=status, fallback_code=code, message=trace_message))
                return AgentResult(status=status, answer=answer, tool_calls=tuple(tool_calls), sources=ledger.sources(), traces=tuple(traces), fallback_code=code)
            spec = by_name.get(choice.name)
            if spec is None:
                traces.append(ToolTrace(step=step, tool=choice.name, status="unsupported", fallback_code=FallbackCode.UNSUPPORTED_QUERY, message="unknown tool"))
                return _terminal("이 질문에 맞는 도구가 없습니다.", FallbackCode.UNSUPPORTED_QUERY, traces, tool_calls)
            try:
                payload = spec.input_model.model_validate(choice.arguments)
            except ValidationError as exc:
                LOGGER.warning("tool-use arguments rejected tool=%s error=%s", choice.name, exc)
                traces.append(ToolTrace(step=step, tool=choice.name, status="schema_invalid", fallback_code=FallbackCode.SCHEMA_INVALID, message="tool arguments rejected"))
                return _terminal("tool argument schema invalid", FallbackCode.SCHEMA_INVALID, traces, tool_calls)
            try:
                envelope = _execute_with_timeout(spec, payload)
            except (FutureTimeoutError, requests.Timeout):
                traces.append(ToolTrace(step=step, tool=choice.name, status="timeout", fallback_code=FallbackCode.TOOL_TIMEOUT, message="tool timeout"))
                return _terminal("tool timeout", FallbackCode.TOOL_TIMEOUT, traces, tool_calls)
            except (requests.RequestException, ValidationError, KeyError, TypeError, ValueError) as exc:
                LOGGER.warning("tool-use execution failed tool=%s error=%s", choice.name, exc)
                traces.append(ToolTrace(step=step, tool=choice.name, status="schema_invalid", fallback_code=FallbackCode.SCHEMA_INVALID, message=type(exc).__name__))
                return _terminal("tool response schema invalid", FallbackCode.SCHEMA_INVALID, traces, tool_calls)
            ledger.add(envelope)
            public_preview = _public_preview(envelope)
            safe_envelope = envelope.model_dump(exclude={"raw"}, mode="json")
            safe_envelope["preview"] = public_preview
            tool_calls.append({"tool": spec.name, "source": spec.tags[0] if spec.tags else "tool_use", "status": "ok" if envelope.ok else "error", "summary_text": public_preview, "render_data": safe_envelope})
            traces.append(ToolTrace(step=step, tool=spec.name, status="ok" if envelope.ok else "no_evidence", fallback_code=None if envelope.ok else FallbackCode.VERIFICATION_FAIL, message=public_preview))
            if not envelope.ok or not ledger.is_complete():
                return _terminal(envelope.error_message or "도구 근거가 비었습니다.", FallbackCode.VERIFICATION_FAIL, traces, tool_calls)
            call_id = choice.call_id or f"tool-use-{step}"
            messages.extend(
                (
                    {
                        "role": "assistant",
                        "content": choice.message or None,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {"name": spec.name, "arguments": json.dumps(choice.arguments, ensure_ascii=False)},
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": spec.name,
                        "content": json.dumps(safe_envelope, ensure_ascii=False),
                    },
                )
            )
        return _terminal("tool-use step limit exceeded", FallbackCode.STEP_LIMIT, traces, tool_calls)


def _terminal(message: str, code: FallbackCode, traces: list[ToolTrace], calls: list[dict]) -> AgentResult:
    return AgentResult(status="fallback", answer=message, tool_calls=tuple(calls), sources=(), traces=tuple(traces), fallback_code=code)


def _public_preview(envelope: ToolEnvelope) -> str:
    if not envelope.ok or not envelope.evidence:
        return "검증 가능한 도구 근거 없음"
    sources = tuple(dict.fromkeys(fact.source_name for fact in envelope.evidence))
    return f"{', '.join(sources)} 근거 {len(envelope.evidence)}건 확인"


def _execute_with_timeout(spec: ToolSpec, payload: BaseModel) -> ToolEnvelope:
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"tool-use-{spec.name}")
    future = pool.submit(spec.execute, payload)
    try:
        return future.result(timeout=spec.timeout_s)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
