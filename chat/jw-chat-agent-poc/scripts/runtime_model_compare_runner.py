from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any

from scripts.fact_scoreboard.recal_runner import _is_degraded_answer
from scripts.fact_scoreboard.sse import parse_sse_text

from jw_chat_agent_poc.service.app import SessionStore, _answer_question, _default_agent_factory, _sse_events
from jw_chat_agent_poc.tools.metrics.market_scope import MarketScopeResolver


QUESTIONS: tuple[tuple[str, str], ...] = (
    ("Q01", "리바로와 리바로젯의 최근 6개월 매출 추이를 비교해줘"),
    ("Q02", "리바로 시장 상위 3개 브랜드의 점유율 변화를 비교해줘"),
    ("Q03", "리바로 점유율이 최근 하락하는 이유가 뭐야?"),
    ("Q04", "리바로 2월 매출이 떨어진 게 시장 전체 영향이야, 리바로만의 문제야?"),
    ("Q05", "리바로 시장의 경쟁 구도가 최근 어떻게 변하고 있어?"),
    ("Q06", "아토젯이 리바로를 위협하고 있어?"),
    ("Q07", "아토젯 점유율이 오르는 동안 리바로 점유율은 어떻게 됐어?"),
    ("Q08", "최근 뉴스 이슈가 리바로 매출에 영향을 줬는지 봐줘"),
    ("Q09", "리바로 매출의 작년 동기 대비 성장률은?"),
    ("Q10", "리바로의 지난 6개월 평균 점유율은?"),
    ("Q11", "리바로 시장은 집중된 시장이야, 분산된 시장이야? (상위 비중·집중도)"),
    ("Q12", "리바로 시장에서 상위 5개 브랜드가 차지하는 비중은?"),
    ("Q13", "리바로가 점유율 4%를 회복하려면 매출이 얼마나 늘어야 해?"),
    ("M01", "리바로 의원 채널에서 성분별 점유율"),
    ("M02", "리바로와 아토젯의 채널별 점유율 차이"),
    ("M03", "리바로 시장 오리지널 vs 제네릭 비중"),
    ("M04", "리바로 상위 경쟁사 3개의 진료과별 매출"),
    ("M05", "리바로 제형별 매출 추이(최근 1년)"),
    ("M06", "리바로 시장에서 급매출 회사 top3와 그 성분"),
    ("M07", "리바로 급여/비급여 매출 구성과 추이"),
)

CONFIGS: tuple[tuple[str, str, str, str, str], ...] = (
    ("flash_all", "76", "76", "flash", "flash"),
    ("lite_all", "163", "163", "lite", "lite"),
    ("mixed_lite_plan_flash_final", "163", "76", "lite", "flash"),
)


def _token(name: str) -> str:
    encoded = os.environ[f"{name.upper()}_TOKEN_B64"]
    return base64.b64decode(encoded).decode()


def _parse_events(events: list[str]) -> tuple[str, dict[str, int]]:
    parsed = parse_sse_text("".join(events))
    counts = {
        "delta_count": parsed.delta_count,
        "done_count": parsed.done_count,
        "error_count": parsed.error_count,
        "charts_count": len(parsed.charts),
        "timing_count": 1 if parsed.timing else 0,
    }
    return parsed.answer_markdown, counts


def _trace_payload(qid: str, question: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "qid": qid,
        "question": question,
        "tool_calls": result.get("tool_calls", []),
        "sources": result.get("sources", []),
        "timing": result.get("timing", {}),
        "resolution": result.get("resolution", {}),
    }


def _configure_model(
    planner_id: str,
    final_id: str,
    planner_token: str,
    final_token: str,
) -> None:
    timeout_s = os.environ.get("MODEL_COMPARE_TIMEOUT_S", "180")
    attempts = os.environ.get("MODEL_COMPARE_GENERATION_ATTEMPTS", "3")
    os.environ["GENOS_PLANNER_SERVING_ID"] = planner_id
    os.environ["GENOS_FINAL_SERVING_ID"] = final_id
    os.environ["GENOS_PLANNER_BEARER_TOKEN"] = planner_token
    os.environ["GENOS_FINAL_BEARER_TOKEN"] = final_token
    os.environ["GENOS_BEARER_TOKEN"] = final_token
    os.environ["GENOS_AGENT_TIMEOUT_S"] = timeout_s
    os.environ["GENOS_FINAL_TIMEOUT_S"] = timeout_s
    os.environ["GENOS_ROUTER_TIMEOUT_S"] = timeout_s
    os.environ["GENOS_GENERATION_ATTEMPTS"] = attempts


def main() -> None:
    tokens = {"flash": _token("flash"), "lite": _token("lite")}
    root = Path(os.environ["MODEL_COMPARE_OUT"])
    root.mkdir(parents=True, exist_ok=True)
    summary: list[dict[str, Any]] = []
    for config_name, planner_id, final_id, planner_token_key, final_token_key in CONFIGS:
        _configure_model(planner_id, final_id, tokens[planner_token_key], tokens[final_token_key])
        config_dir = root / config_name
        for child in ("sse", "markdown", "traces"):
            (config_dir / child).mkdir(parents=True, exist_ok=True)
        for qid, question in QUESTIONS:
            started = time.perf_counter()
            status = "ok"
            error_text = ""
            events: list[str] = []
            result: dict[str, Any] = {}
            try:
                item = _answer_question(SessionStore(), MarketScopeResolver(), _default_agent_factory, question, "live", None)
                result = item["result"]
                events = list(_sse_events(question, result, item.get("conversation_id")))
            except Exception as exc:  # noqa: BLE001 - top-level audit runner records and continues matrix
                status = "error"
                error_text = f"{type(exc).__name__}: {exc}"
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            answer, counts = _parse_events(events)
            (config_dir / "sse" / f"{qid}.sse").write_text("".join(events), encoding="utf-8")
            (config_dir / "markdown" / f"{qid}.md").write_text(answer, encoding="utf-8")
            (config_dir / "traces" / f"{qid}.json").write_text(
                json.dumps(_trace_payload(qid, question, result), ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            row = {
                "config": config_name,
                "qid": qid,
                "status": status,
                "error": error_text,
                "elapsed_ms": elapsed_ms,
                "answer_chars": len(answer),
                "degraded": _is_degraded_answer(answer),
                "planner_serving_id": planner_id,
                "final_serving_id": final_id,
                **counts,
            }
            summary.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    (root / "model_compare_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
