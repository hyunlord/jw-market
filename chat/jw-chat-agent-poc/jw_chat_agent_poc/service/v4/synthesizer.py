from __future__ import annotations

import json
from collections.abc import Sequence

from jw_chat_agent_poc.service.conversation import ConversationTurn
from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, SourceResult
from jw_chat_agent_poc.service.v4.llm import GenOSV4Client


class V4Synthesizer:
    def __init__(self, client: GenOSV4Client) -> None:
        self._client = client

    def synthesize(
        self,
        plan: PlannerOutput,
        results: Sequence[SourceResult],
        turns: Sequence[ConversationTurn],
        *,
        budget_s: float = 15.0,
    ) -> str:
        usable = [result for result in results if result.status == "ok"]
        if not usable:
            return "7개 조회 경로에서 이번 질문에 사용할 근거를 확인하지 못했습니다."
        evidence = [
            {
                "source": result.source,
                "query": result.query,
                "payload": result.payload,
                "citations": [citation.model_dump(mode="json") for citation in result.citations],
            }
            for result in usable
        ]
        history = [
            {"question": turn.question, "answer": turn.answer}
            for turn in tuple(turns)[-10:]
        ]
        messages = [
            {
                "role": "system",
                "content": (
                    "너는 CHAT-V4의 답변 합성기다. 사용자의 질문에 가능한 범위에서 직접 답한다. "
                    "고정 템플릿, '핵심 결과', '요청 지표 미제공' 헤더를 쓰지 않는다. 긴 자연어 설명을 기본으로 하고, "
                    "결론, 근거의 연결, 실무적 인사이트를 포함한다. 표와 차트용 JSON은 유용할 때만 마크다운 표로 표현한다. "
                    "외부·웹 근거는 출처 범위 안에서 자유롭게 서술한다. mart 매출·점유율·순위·HHI·성장률 숫자는 "
                    "payload의 값을 글자 그대로 복사하고 계산·반올림·합산하지 않는다. UBIST와 IQVIA를 합산하지 않는다. "
                    "답변 끝 출처 블록은 시스템이 붙이므로 작성하지 않는다. 못 찾은 부분은 마지막에 한 줄로만 적는다."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "resolved_question": plan.resolved_question,
                        "expanded_intents": plan.expanded_intents,
                        "recent_turns": history,
                        "evidence": evidence,
                    },
                    ensure_ascii=False,
                    default=str,
                ),
            },
        ]
        try:
            answer = self._client.complete(messages, budget_s=budget_s).strip()
        except Exception:  # noqa: BLE001 - evidence fallback is preferable to a 500
            answer = ""
        return answer or _evidence_fallback(usable)


def _evidence_fallback(results: Sequence[SourceResult]) -> str:
    lines = ["확인된 근거를 출처별로 정리합니다."]
    for result in results:
        payload = json.dumps(result.payload, ensure_ascii=False, default=str)
        lines.append(f"- {result.source}: {payload[:1200]}")
    return "\n\n".join(lines)
