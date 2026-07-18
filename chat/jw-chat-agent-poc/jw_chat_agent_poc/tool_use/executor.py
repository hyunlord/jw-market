from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextvars import copy_context
from dataclasses import dataclass
import json
import logging
from typing import Protocol

from pydantic import BaseModel, ValidationError
import requests

from jw_chat_agent_poc.common.timing import Timing, stage
from jw_chat_agent_poc.tool_use.contracts import AgentResult, FallbackCode, ToolEnvelope, ToolTrace
from jw_chat_agent_poc.tool_use.ledger import EvidenceLedger
from jw_chat_agent_poc.tool_use.provider import ToolChoice, ToolChoiceProvider, ToolProviderConfigurationError
from jw_chat_agent_poc.tool_use.renderer import render_evidence_answer
from jw_chat_agent_poc.tool_use.specs import ToolSpec


LOGGER = logging.getLogger(__name__)


class CompletionPolicy(Protocol):
    def __call__(
        self,
        *,
        user_text: str,
        ledger: EvidenceLedger,
        spec: ToolSpec | None,
        tool_calls: tuple[dict, ...],
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class AgentExecutor:
    provider: ToolChoiceProvider
    max_steps: int = 6
    completion_policy: CompletionPolicy | None = None
    best_effort: bool = False
    forced_choices: tuple[ToolChoice, ...] = ()
    parallel_forced_choices: bool = False
    timing: Timing | None = None

    def run(self, *, user_text: str, tools: tuple[ToolSpec, ...]) -> AgentResult:
        ledger = EvidenceLedger()
        traces: list[ToolTrace] = []
        tool_calls: list[dict] = []
        tool_policy = (
            "질문에서 요청한 근거 유형마다 관련 도구를 독립적으로 시도한다. "
            "한 도구가 실패하거나 비어도 다른 관련 도구를 계속 호출하고, 검증된 결과만 사용한다. "
            if self.best_effort
            else "필요한 경우에만 tool 을 호출한다. "
        )
        messages: list[dict] = [
            {
                "role": "system",
                "content": (
                    f"{tool_policy}수치, 날짜, 점유율은 tool 근거가 없으면 추정하지 않는다. "
                    "내부 테이블명, 캐시 키, 시스템 용어를 노출하지 않는다. 근거가 없으면 확인 불가라고 답한다."
                ),
            },
            {"role": "user", "content": user_text},
        ]
        by_name = {tool.name: tool for tool in tools}
        answer_complete = False
        forced_choices = list(self.forced_choices)
        if self.parallel_forced_choices and len(forced_choices) > 1:
            prepared: list[tuple[ToolChoice, ToolSpec, BaseModel]] = []
            for step, choice in enumerate(forced_choices, start=1):
                spec = by_name.get(choice.name)
                if spec is None:
                    traces.append(
                        ToolTrace(
                            step=step,
                            tool=choice.name,
                            status="unsupported",
                            fallback_code=FallbackCode.UNSUPPORTED_QUERY,
                            message="unknown tool",
                        )
                    )
                    return _terminal(
                        "이 질문에 맞는 도구가 없습니다.",
                        FallbackCode.UNSUPPORTED_QUERY,
                        traces,
                        tool_calls,
                    )
                try:
                    payload = spec.input_model.model_validate(choice.arguments)
                except ValidationError as exc:
                    LOGGER.warning("tool-use arguments rejected tool=%s error=%s", choice.name, exc)
                    traces.append(
                        ToolTrace(
                            step=step,
                            tool=choice.name,
                            status="schema_invalid",
                            fallback_code=FallbackCode.SCHEMA_INVALID,
                            message="tool arguments rejected",
                        )
                    )
                    return _terminal(
                        "tool argument schema invalid",
                        FallbackCode.SCHEMA_INVALID,
                        traces,
                        tool_calls,
                    )
                prepared.append((choice, spec, payload))

            with ThreadPoolExecutor(
                max_workers=min(len(prepared), 8),
                thread_name_prefix="tool-use-batch",
            ) as pool:
                futures = [
                    pool.submit(
                        copy_context().run,
                        _execute_with_progress,
                        self.timing,
                        spec,
                        payload,
                        user_text,
                    )
                    for _choice, spec, payload in prepared
                ]
                for step, ((choice, spec, _payload), future) in enumerate(
                    zip(prepared, futures, strict=True),
                    start=1,
                ):
                    try:
                        envelope = future.result()
                    except (FutureTimeoutError, requests.Timeout):
                        if not self.best_effort:
                            traces.append(
                                ToolTrace(
                                    step=step,
                                    tool=choice.name,
                                    status="timeout",
                                    fallback_code=FallbackCode.TOOL_TIMEOUT,
                                    message="tool timeout",
                                )
                            )
                            return _terminal("tool timeout", FallbackCode.TOOL_TIMEOUT, traces, tool_calls)
                        envelope = ToolEnvelope(
                            ok=False,
                            preview="tool timeout",
                            evidence=(),
                            raw=None,
                            error_code=FallbackCode.TOOL_TIMEOUT.value,
                            error_message="도구 조회 시간이 초과되었습니다.",
                        )
                    except (requests.RequestException, ValidationError, KeyError, TypeError, ValueError) as exc:
                        LOGGER.warning("tool-use execution failed tool=%s error=%s", choice.name, exc)
                        traces.append(
                            ToolTrace(
                                step=step,
                                tool=choice.name,
                                status="schema_invalid",
                                fallback_code=FallbackCode.SCHEMA_INVALID,
                                message=type(exc).__name__,
                            )
                        )
                        return _terminal(
                            "tool response schema invalid",
                            FallbackCode.SCHEMA_INVALID,
                            traces,
                            tool_calls,
                        )
                    ledger.add(envelope)
                    public_preview = _public_preview(envelope)
                    safe_envelope = envelope.model_dump(exclude={"raw"}, mode="json")
                    safe_envelope["preview"] = public_preview
                    tool_calls.append(
                        {
                            "tool": spec.name,
                            "source": spec.tags[0] if spec.tags else "tool_use",
                            "status": "ok" if envelope.ok else "error",
                            "summary_text": public_preview,
                            "render_data": safe_envelope,
                        }
                    )
                    traces.append(
                        ToolTrace(
                            step=step,
                            tool=spec.name,
                            status="ok" if envelope.ok else "no_evidence",
                            fallback_code=None if envelope.ok else FallbackCode.VERIFICATION_FAIL,
                            message=public_preview,
                        )
                    )
                    call_id = choice.call_id or f"tool-call-{step}"
                    messages.extend(_tool_exchange(choice, spec, safe_envelope, call_id))
            forced_choices.clear()
            answer_complete = _is_complete(
                self.completion_policy,
                user_text=user_text,
                ledger=ledger,
                spec=prepared[-1][1],
                tool_calls=tuple(tool_calls),
            )
            if answer_complete:
                return _verified_result(ledger, traces, tool_calls, status="ok")
        total_steps = max(self.max_steps, len(forced_choices) + 1)
        for step in range(1, total_steps + 1):
            if forced_choices:
                choice = forced_choices.pop(0)
            else:
                try:
                    choice = self.provider.choose(user_text=user_text, messages=messages, tools=[tool.openai_schema() for tool in tools])
                except requests.Timeout:
                    return _terminal("tool timeout", FallbackCode.TOOL_TIMEOUT, traces, tool_calls)
                except (ToolProviderConfigurationError, requests.RequestException, KeyError, TypeError, ValueError) as exc:
                    LOGGER.warning("tool-use provider schema invalid: %s", exc)
                    return _terminal("provider schema invalid", FallbackCode.SCHEMA_INVALID, traces, tool_calls)
            if choice.name is None:
                answer_complete = _is_complete(
                    self.completion_policy,
                    user_text=user_text,
                    ledger=ledger,
                    spec=None,
                    tool_calls=tuple(tool_calls),
                )
                if ledger.is_complete() and not answer_complete:
                    if self.best_effort:
                        return _verified_result(ledger, traces, tool_calls, status="ok")
                    traces.append(
                        ToolTrace(
                            step=step,
                            tool=None,
                            status="verification_failed",
                            fallback_code=FallbackCode.VERIFICATION_FAIL,
                            message="planner stopped before required evidence was complete",
                        )
                    )
                    return _terminal(
                        "요청한 근거를 완성할 도구가 선택되지 않았습니다.",
                        FallbackCode.VERIFICATION_FAIL,
                        traces,
                        tool_calls,
                    )
                code = None if answer_complete else FallbackCode.UNSUPPORTED_QUERY
                status = "ok" if answer_complete else "unsupported"
                answer = render_evidence_answer(tuple(ledger.facts)) if answer_complete else "이 질문에 맞는 도구가 없습니다."
                trace_message = "evidence complete" if answer_complete else "no matching tool"
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
                with stage(self.timing, f"tool:{spec.name}", user_text) as progress:
                    envelope = _execute_with_timeout(spec, payload)
                    progress.summary = (
                        f"근거 {len(envelope.evidence)}건 확인"
                        if envelope.ok and envelope.evidence
                        else "확인된 근거 없음"
                    )
            except (FutureTimeoutError, requests.Timeout):
                if not self.best_effort:
                    traces.append(ToolTrace(step=step, tool=choice.name, status="timeout", fallback_code=FallbackCode.TOOL_TIMEOUT, message="tool timeout"))
                    return _terminal("tool timeout", FallbackCode.TOOL_TIMEOUT, traces, tool_calls)
                envelope = ToolEnvelope(
                    ok=False,
                    preview="tool timeout",
                    evidence=(),
                    raw=None,
                    error_code=FallbackCode.TOOL_TIMEOUT.value,
                    error_message="도구 조회 시간이 초과되었습니다.",
                )
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
                if self.best_effort:
                    call_id = choice.call_id or f"tool-call-{step}"
                    messages.extend(_tool_exchange(choice, spec, safe_envelope, call_id))
                    continue
                return _terminal(envelope.error_message or "도구 근거가 비었습니다.", FallbackCode.VERIFICATION_FAIL, traces, tool_calls)
            answer_complete = _is_complete(
                self.completion_policy,
                user_text=user_text,
                ledger=ledger,
                spec=spec,
                tool_calls=tuple(tool_calls),
            )
            if answer_complete and not forced_choices:
                return _verified_result(ledger, traces, tool_calls, status="ok")
            call_id = choice.call_id or f"tool-call-{step}"
            messages.extend(_tool_exchange(choice, spec, safe_envelope, call_id))
        if self.best_effort and ledger.is_complete():
            return _verified_result(ledger, traces, tool_calls, status="ok")
        return _terminal("tool-use step limit exceeded", FallbackCode.STEP_LIMIT, traces, tool_calls)


def _is_complete(
    policy: CompletionPolicy | None,
    *,
    user_text: str,
    ledger: EvidenceLedger,
    spec: ToolSpec | None,
    tool_calls: tuple[dict, ...],
) -> bool:
    if policy is None:
        return ledger.is_complete()
    return policy(user_text=user_text, ledger=ledger, spec=spec, tool_calls=tool_calls)


def _terminal(message: str, code: FallbackCode, traces: list[ToolTrace], calls: list[dict]) -> AgentResult:
    return AgentResult(status="fallback", answer=message, tool_calls=tuple(calls), sources=(), traces=tuple(traces), fallback_code=code)


def _verified_result(
    ledger: EvidenceLedger,
    traces: list[ToolTrace],
    calls: list[dict],
    *,
    status: str,
) -> AgentResult:
    return AgentResult(
        status=status,
        answer=render_evidence_answer(tuple(ledger.facts)),
        tool_calls=tuple(calls),
        sources=ledger.sources(),
        traces=tuple(traces),
        fallback_code=None,
    )


def _tool_exchange(
    choice: ToolChoice,
    spec: ToolSpec,
    safe_envelope: dict[str, object],
    call_id: str,
) -> tuple[dict[str, object], dict[str, object]]:
    return (
        {
            "role": "assistant",
            "content": choice.message or None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "arguments": json.dumps(choice.arguments, ensure_ascii=False, sort_keys=True),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": call_id,
            "name": spec.name,
            "content": json.dumps(safe_envelope, ensure_ascii=False, sort_keys=True),
        },
    )


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


def _execute_with_progress(
    timing: Timing | None,
    spec: ToolSpec,
    payload: BaseModel,
    user_text: str,
) -> ToolEnvelope:
    with stage(timing, f"tool:{spec.name}", user_text) as progress:
        envelope = _execute_with_timeout(spec, payload)
        progress.summary = (
            f"근거 {len(envelope.evidence)}건 확인"
            if envelope.ok and envelope.evidence
            else "확인된 근거 없음"
        )
        return envelope
