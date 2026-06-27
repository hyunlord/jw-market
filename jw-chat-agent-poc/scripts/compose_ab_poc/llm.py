from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

import requests

from jw_chat_agent_poc.genos_config import resolve_genos_base_url
from scripts.compose_ab_poc.catalog import CompositionCatalog
from scripts.compose_ab_poc.models import Approach
from scripts.compose_ab_poc.questions import INTENT_DESCRIPTIONS


@dataclass(frozen=True, slots=True)
class GenosJsonClient:
    """Small JSON-only Flash client for the offline composition PoC."""

    base_url: str = resolve_genos_base_url()
    catalog: CompositionCatalog | None = None
    token: str | None = os.environ.get("GENOS_BEARER_TOKEN")
    host_header: str | None = os.environ.get("GENOS_HOST_HEADER")
    timeout_s: int = 90

    def plan(self, question: str, approach: Approach) -> tuple[str, dict[str, Any]]:
        """Ask Flash to produce a primitive chain or query spec JSON object."""

        messages = [
            {"role": "system", "content": self._system_prompt(approach)},
            {"role": "user", "content": self._user_prompt(question, approach)},
        ]
        raw = self._chat(messages)
        return raw, _extract_json(raw)

    def _chat(self, messages: list[dict[str, str]]) -> str:
        if not self.token:
            raise RuntimeError("GENOS_BEARER_TOKEN is required for this PoC")
        headers = {"Authorization": f"Bearer {self.token}"}
        if self.host_header:
            headers["Host"] = self.host_header
        response = requests.post(
            f"{self.base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json={"messages": messages, "temperature": 0.0, "stream": False},
            timeout=self.timeout_s,
        )
        response.raise_for_status()
        payload = response.json()
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        message = choices[0].get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
        return ""

    def _system_prompt(self, approach: Approach) -> str:
        catalog = self.catalog
        if catalog is None:
            msg = "CompositionCatalog is required for grounded planning"
            raise RuntimeError(msg)
        base = (
            "너는 JW 시장분석 도구 조합 PoC의 planner다. "
            "숫자 공식과 계산은 코드의 compute_* 함수가 수행하므로, 너는 질문을 알맞은 intent와 도구 조합으로만 매핑한다. "
            "반드시 JSON 한 개만 출력하고 설명 문장을 붙이지 않는다. "
            "식별자는 아래 catalog enum에 있는 정확한 문자열만 써라. 유사어·번역어·새 이름을 만들지 마라. "
            "없는 차원은 쓰지 말고, 지원 불가 intent를 고른 뒤 derive에는 unsupported_dimension만 써라. "
            "가능한 intent_id 목록은 다음과 같다:\n"
            + "\n".join(f"- {key}: {value}" for key, value in INTENT_DESCRIPTIONS.items())
            + "\n\n"
            + catalog.prompt_block()
        )
        if approach == "primitive":
            return (
                base
                + "\n\n출력 스키마: "
                '{"intent_id":"...", "steps":[{"tool":"<primitive_tools enum only>", "args":{}}], "reason":"..."}'
                "\nprimitive tool은 반드시 primitive_tools enum 중 하나다. compute_* 와 임의 compute 이름은 금지한다."
            )
        return (
            base
            + "\n\n출력 스키마: "
            '{"intent_id":"...", "spec":{"source":"ubist","view":"market_landscape","market":"ml_006","dimensions":[],"filters":[],"group_by":[],"metrics":[],"derive":[],"sort":null,"limit":null}, "reason":"..."}'
            "\nmetrics는 sales/share/rank/hhi/growth 중에서만, group_by는 catalog group_by 중에서만 고른다."
        )

    @staticmethod
    def _user_prompt(question: str, approach: Approach) -> str:
        if approach == "primitive":
            return (
                f"질문: {question}\n"
                "primitive chain으로 필요한 최소 단계만 작성해라. "
                "중간 결과는 result_id 핸들로 이어진다고 가정한다."
            )
        return (
            f"질문: {question}\n"
            "query(spec) 방식으로 한 번에 가능한 집계는 spec에 넣고, 필요한 파생 계산은 derive에 적어라."
        )


def _extract_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("LLM JSON root is not an object")
    return value
