from __future__ import annotations

from threading import Lock
import time

from jw_chat_agent_poc.agent_loop.models import ToolCallPlan
from jw_chat_agent_poc.agent_loop.parallel_execution import execute_tool_batch


def test_parallel_support_tools_overlap_but_results_keep_plan_order() -> None:
    plans = (
        ToolCallPlan("search_news", {"brand": "리바로"}),
        ToolCallPlan("get_disease_stats", {"brand": "리바로"}),
        ToolCallPlan("csd_activity_trend", {"brand": "리바로"}),
    )
    delays = {"search_news": 0.08, "get_disease_stats": 0.02, "csd_activity_trend": 0.05}

    started = time.perf_counter()
    results = execute_tool_batch(plans, lambda plan: _delayed_name(plan, delays[plan.name]), max_workers=3)
    elapsed = time.perf_counter() - started

    assert [item.result for item in results] == [plan.name for plan in plans]
    assert {item.mode for item in results} == {"parallel"}
    assert elapsed < 0.13


def test_market_query_tools_remain_serial() -> None:
    plans = (
        ToolCallPlan("get_brand_series", {"brand": "리바로"}),
        ToolCallPlan("get_top_brands", {"brand": "리바로"}),
    )
    state = {"active": 0, "peak": 0}
    lock = Lock()

    def execute(plan: ToolCallPlan) -> str:
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.01)
        with lock:
            state["active"] -= 1
        return plan.name

    results = execute_tool_batch(plans, execute, max_workers=3)

    assert state["peak"] == 1
    assert {item.mode for item in results} == {"serial"}


def _delayed_name(plan: ToolCallPlan, delay: float) -> str:
    time.sleep(delay)
    return plan.name
