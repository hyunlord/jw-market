#!/usr/bin/env python3
"""Phase 29 sentiment scoring for Cut B events.

If `ANTHROPIC_API_KEY` is available, this module can call Anthropic Messages
API directly without adding a package dependency. For local verification and
CI it falls back to a deterministic, auditable scorer and marks the method in
the returned payload.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from typing import Any


POSITIVE_MARKERS = (
    "급여",
    "등재",
    "적응증",
    "허가",
    "승인",
    "인상",
    "수급",
    "확대",
    "성공",
    "비열등",
    "공개",
)
NEGATIVE_MARKERS = (
    "인하",
    "부작용",
    "퇴출",
    "중단",
    "회수",
    "제네릭",
    "복제약",
    "경쟁",
    "소송",
    "감소",
)


def deterministic_sentiment(brand: str, event: dict[str, Any]) -> dict[str, Any]:
    text = " ".join(
        str(event.get(key) or "")
        for key in ("title", "summary", "reason", "tag")
    )
    pos = sum(1 for marker in POSITIVE_MARKERS if marker in text)
    neg = sum(1 for marker in NEGATIVE_MARKERS if marker in text)
    if pos > neg:
        score = 1
    elif neg > pos:
        score = -1
    else:
        score = 0

    tag = str(event.get("tag") or "")
    if "정책" in tag:
        duration = 12
    elif "신약" in tag or "R&D" in tag:
        duration = 9
    elif "공급" in tag:
        duration = 6
    else:
        duration = 3
    return {
        "event_id": event.get("event_id") or event.get("id"),
        "brand": brand,
        "sentiment_score": score,
        "duration_months": duration,
        "reasoning": "Deterministic fallback: keyword polarity over title/summary/reason/tag because no LLM credential was available.",
        "method": "deterministic_fallback_no_llm_key",
        "event": event,
    }


def _extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def _prompt(brand: str, event: dict[str, Any]) -> str:
    return f"""
Brand: {brand}
Event date: {event.get('published_date') or event.get('date')}
Event title: {event.get('title')}
Event summary: {event.get('summary')}
Event tag: {event.get('tag')}
Event reason from Agent 1: {event.get('reason')}

이 event가 "{brand}" 매출에 미치는 영향을 평가하세요.

JSON만 출력:
{{
  "sentiment_score": -1, 0, 또는 1,
  "duration_months": 1-24 사이 정수,
  "reasoning": "1-2문장"
}}
""".strip()


def anthropic_sentiment(brand: str, event: dict[str, Any], *, model: str = "claude-sonnet-4-5") -> dict[str, Any]:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    payload = {
        "model": model,
        "max_tokens": 400,
        "messages": [{"role": "user", "content": _prompt(brand, event)}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        body = json.load(response)
    text = "\n".join(part.get("text", "") for part in body.get("content", []) if part.get("type") == "text")
    parsed = _extract_json(text)
    return _normalize_llm_result(brand, event, parsed, method=f"anthropic:{model}")


def claude_cli_sentiment(brand: str, event: dict[str, Any]) -> dict[str, Any]:
    result = subprocess.run(
        ["claude", "-p", "--tools", "", "--max-budget-usd", "0.20", _prompt(brand, event)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    parsed = _extract_json(result.stdout)
    return _normalize_llm_result(brand, event, parsed, method="claude_cli")


def _normalize_llm_result(brand: str, event: dict[str, Any], parsed: dict[str, Any], *, method: str) -> dict[str, Any]:
    score = int(parsed.get("sentiment_score", 0))
    score = max(-1, min(1, score))
    duration = int(parsed.get("duration_months", 3))
    duration = max(1, min(24, duration))
    return {
        "event_id": event.get("event_id") or event.get("id"),
        "brand": brand,
        "sentiment_score": score,
        "duration_months": duration,
        "reasoning": str(parsed.get("reasoning") or ""),
        "method": method,
        "event": event,
    }


def score_event(brand: str, event: dict[str, Any], *, use_llm: bool = False) -> dict[str, Any]:
    if use_llm:
        if os.getenv("ANTHROPIC_API_KEY"):
            return anthropic_sentiment(brand, event)
        if os.getenv("PHASE29_USE_CLAUDE_CLI") == "1":
            return claude_cli_sentiment(brand, event)
    return deterministic_sentiment(brand, event)


def score_events(brand: str, events: list[dict[str, Any]], *, use_llm: bool = False, max_events: int | None = None) -> list[dict[str, Any]]:
    selected = events[:max_events] if max_events else events
    return [score_event(brand, event, use_llm=use_llm) for event in selected]
