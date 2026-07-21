"""Temporal pilot for the existing Tier2 news batch commands.

The workflow deliberately orchestrates the current CLIs. It does not duplicate
their crawl, match, scoring, or validation logic, and it never promotes pilot
rows into ``event_brand_scores``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy


TASK_QUEUE = "jw-market-tier2-pilot-v1"
NAMESPACE = "jw-market-pilot"
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_SAFE_TABLE = re.compile(r"^[a-zA-Z0-9_]+$")


@dataclass(frozen=True)
class PilotInput:
    run_id: str
    brand_file: str
    weekday_slice: int
    limit_brands: int = 1
    sites: str = "약업신문"
    max_articles: int = 1
    match_table: str = "tier2_match_staging"
    llm_limit: int = 1
    work_root: str = "/tmp/jw-market-temporal-pilot"
    inject_failure_stage: str | None = None
    inject_failure_attempts: int = 0


def safe_run_id(value: str) -> str:
    if not _SAFE_ID.fullmatch(value):
        raise ValueError("run_id must match ^[a-z0-9][a-z0-9_-]{0,31}$")
    return value


def _validated(config: PilotInput) -> PilotInput:
    safe_run_id(config.run_id)
    if not 0 <= config.weekday_slice <= 6:
        raise ValueError("weekday_slice must be between 0 and 6")
    if not 1 <= config.limit_brands <= 5:
        raise ValueError("pilot limit_brands must be between 1 and 5")
    if not 1 <= config.max_articles <= 10:
        raise ValueError("pilot max_articles must be between 1 and 10")
    if not 1 <= config.llm_limit <= 10:
        raise ValueError("pilot llm_limit must be between 1 and 10")
    if not _SAFE_TABLE.fullmatch(config.match_table):
        raise ValueError("match_table contains unsafe characters")
    return config


def isolated_staging_table(config: PilotInput) -> str:
    suffix = safe_run_id(config.run_id).replace("-", "_")
    return f"event_brand_scores__temporal_pilot_{suffix}"


def activity_commands(config: PilotInput) -> dict[str, list[str]]:
    config = _validated(config)
    work = Path(config.work_root) / config.run_id
    raw = work / "raw"
    processed = work / "processed"
    plan = work / "tier2_brand_plan.json"
    common = [
        "--brand-file",
        config.brand_file,
        "--weekday-slice",
        str(config.weekday_slice),
        "--limit-brands",
        str(config.limit_brands),
        "--brand-plan-output",
        str(plan),
    ]
    return {
        "select_brand_universe": [
            "python",
            "crawl/crawler/crawl_2tier.py",
            "--tier",
            "2",
            "--dry-run",
            *common,
        ],
        "crawl_news": [
            "python",
            "crawl/crawler/crawl_2tier.py",
            "--tier",
            "2",
            "--run-crawl",
            *common,
            "--days",
            "7",
            "--sites",
            config.sites,
            "--max-pages-per-site",
            "1",
            "--max-links-per-page",
            "10",
            "--max-articles",
            str(config.max_articles),
            "--delay-sec",
            "1",
            "--output-dir",
            str(raw),
        ],
        "match_and_prescore": [
            "python",
            "crawl/crawler/crawl_2tier.py",
            "--tier",
            "2",
            "--score-only",
            *common,
            "--output-dir",
            str(raw),
            "--processed-dir",
            str(processed),
        ],
        "llm_precision_score": [
            "python",
            "/opt/tier2/tier2_full_scoring_runner.py",
            "score-staging",
            "--match-table",
            config.match_table,
            "--staging-table",
            isolated_staging_table(config),
            "--limit",
            str(config.llm_limit),
            "--batch-size",
            "1",
        ],
        "validate_isolated_result": [
            "python",
            "/opt/tier2/tier2_full_scoring_runner.py",
            "validate-staging",
            "--match-table",
            config.match_table,
            "--staging-table",
            isolated_staging_table(config),
        ],
    }


def _receipt_path(config: PilotInput, stage: str) -> Path:
    return Path(config.work_root) / config.run_id / "receipts" / f"{stage}.json"


async def _run_stage(config: PilotInput, stage: str) -> dict[str, Any]:
    config = _validated(config)
    command = activity_commands(config)[stage]
    receipt = _receipt_path(config, stage)
    fingerprint = hashlib.sha256(json.dumps(command, separators=(",", ":")).encode()).hexdigest()
    if receipt.exists():
        saved = json.loads(receipt.read_text(encoding="utf-8"))
        if saved.get("command_sha256") == fingerprint and saved.get("status") == "completed":
            activity.heartbeat({"stage": stage, "state": "receipt-hit"})
            return {**saved, "receipt_hit": True}

    info = activity.info()
    if config.inject_failure_stage == stage and info.attempt <= config.inject_failure_attempts:
        marker = f"TEMPORAL_PILOT_INJECTED_FAILURE stage={stage} attempt={info.attempt}"
        activity.logger.error(marker)
        raise RuntimeError(marker)

    receipt.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=os.getenv("TIER2_REPO_ROOT", "/work"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    lines: list[str] = []
    assert process.stdout is not None
    while True:
        try:
            raw = await asyncio.wait_for(process.stdout.readline(), timeout=30)
        except TimeoutError:
            activity.heartbeat({"stage": stage, "state": "running", "lines": len(lines)})
            continue
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip()
        lines.append(line)
        activity.logger.info("stage=%s %s", stage, line)
        activity.heartbeat({"stage": stage, "state": "running", "lines": len(lines)})
    return_code = await process.wait()
    elapsed = round(time.monotonic() - started, 3)
    if return_code != 0:
        raise RuntimeError(
            f"stage={stage} return_code={return_code} tail={lines[-20:]}"
        )
    result = {
        "stage": stage,
        "status": "completed",
        "attempt": info.attempt,
        "elapsed_seconds": elapsed,
        "command_sha256": fingerprint,
        "output_tail": lines[-20:],
        "receipt_hit": False,
    }
    receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return result


@activity.defn
async def select_brand_universe(config: PilotInput) -> dict[str, Any]:
    return await _run_stage(config, "select_brand_universe")


@activity.defn
async def crawl_news(config: PilotInput) -> dict[str, Any]:
    return await _run_stage(config, "crawl_news")


@activity.defn
async def match_and_prescore(config: PilotInput) -> dict[str, Any]:
    return await _run_stage(config, "match_and_prescore")


@activity.defn
async def llm_precision_score(config: PilotInput) -> dict[str, Any]:
    return await _run_stage(config, "llm_precision_score")


@activity.defn
async def validate_isolated_result(config: PilotInput) -> dict[str, Any]:
    return await _run_stage(config, "validate_isolated_result")


@activity.defn
async def hello_activity(name: str) -> str:
    return f"hello {name}"


@workflow.defn
class HelloWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_activity(
            hello_activity,
            name,
            start_to_close_timeout=timedelta(minutes=1),
        )


@workflow.defn
class Tier2PilotWorkflow:
    @workflow.run
    async def run(self, config: PilotInput) -> dict[str, Any]:
        stages: list[dict[str, Any]] = []
        definitions = [
            (select_brand_universe, timedelta(minutes=10), 2),
            (crawl_news, timedelta(hours=2), 3),
            (match_and_prescore, timedelta(minutes=30), 2),
            (llm_precision_score, timedelta(hours=2), 3),
            (validate_isolated_result, timedelta(minutes=10), 2),
        ]
        for function, timeout, maximum_attempts in definitions:
            stages.append(
                await workflow.execute_activity(
                    function,
                    config,
                    start_to_close_timeout=timeout,
                    heartbeat_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(
                        initial_interval=timedelta(seconds=15),
                        backoff_coefficient=2.0,
                        maximum_interval=timedelta(minutes=5),
                        maximum_attempts=maximum_attempts,
                    ),
                )
            )
        return {
            "run_id": config.run_id,
            "isolated_staging_table": isolated_staging_table(config),
            "stages": stages,
        }


ALL_ACTIVITIES = [
    hello_activity,
    select_brand_universe,
    crawl_news,
    match_and_prescore,
    llm_precision_score,
    validate_isolated_result,
]
ALL_WORKFLOWS = [HelloWorkflow, Tier2PilotWorkflow]


def input_as_dict(config: PilotInput) -> dict[str, Any]:
    return asdict(_validated(config))
