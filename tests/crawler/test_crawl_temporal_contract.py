from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from pipeline.scripts.crawler import crawl_temporal_contract as temporal_contract
from pipeline.scripts.crawler.crawl_temporal_contract import (
    ACTIVITY_STAGES,
    INTERNAL_STAGE_BY_ACTIVITY,
    CrawlDailyInput,
    StageGateError,
    activity_command,
    orchestrator_failure_count,
    read_stage_gate,
    run_dependency_sequence,
    write_content_addressed_baseline,
)


def _gate(path: Path, **overrides: object) -> Path:
    payload: dict[str, object] = {
        "schema": "crawl-stage-gate/v1",
        "stage": "tier2_classify_v2_and_refresh",
        "exit_code": 0,
        "failures": 0,
        "events_raw_gap": 0,
        "pending_gap": 0,
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_stage_gate_requires_all_four_success_conditions(tmp_path: Path) -> None:
    accepted = read_stage_gate(_gate(tmp_path / "accepted.json"))

    assert accepted.exit_code == 0
    assert accepted.failures == 0
    assert accepted.events_raw_gap == 0
    assert accepted.pending_gap == 0


@pytest.mark.parametrize(
    ("field", "value", "error_code"),
    [
        ("exit_code", 17, "nonzero_exit"),
        ("failures", 1, "reported_failures"),
        ("events_raw_gap", 1, "events_raw_gap"),
        ("pending_gap", 1, "pending_gap"),
    ],
)
def test_stage_gate_fails_closed_for_each_condition(
    tmp_path: Path,
    field: str,
    value: int,
    error_code: str,
) -> None:
    with pytest.raises(StageGateError) as raised:
        read_stage_gate(_gate(tmp_path / f"{field}.json", **{field: value}))

    assert raised.value.error_code == error_code
    assert raised.value.non_retryable is True


def test_stage_gate_rejects_missing_or_non_numeric_fields(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    missing.write_text(
        json.dumps(
            {
                "schema": "crawl-stage-gate/v1",
                "stage": "tier1_collect",
                "exit_code": 0,
                "failures": 0,
                "events_raw_gap": 0,
            }
        ),
        encoding="utf-8",
    )
    malformed = _gate(tmp_path / "malformed.json", pending_gap="0")

    with pytest.raises(StageGateError, match="missing"):
        read_stage_gate(missing)
    with pytest.raises(StageGateError, match="integer"):
        read_stage_gate(malformed)


def test_orchestrator_failure_count_rejects_partial_site_results() -> None:
    report = {
        "results": [
            {"site": "ok", "exit_code": 0},
            {"site": "nonzero", "exit_code": 7},
            {"site": "worker-error", "exit_code": -1, "error": "worker failed"},
            {"site": "error-only", "exit_code": 0, "error": "partial"},
        ]
    }

    assert orchestrator_failure_count(report) == 3


@pytest.mark.parametrize("report", [None, {}, {"results": "invalid"}])
def test_orchestrator_failure_count_fails_closed_for_missing_schema(report: object) -> None:
    with pytest.raises(ValueError):
        orchestrator_failure_count(report)


def test_activity_command_runs_one_durable_stage_only(tmp_path: Path) -> None:
    config = CrawlDailyInput(
        run_id="jw-agent-crawl-shadow-20260723-a",
        state_root=str(tmp_path / "state"),
        stage_script="/work/pipeline/scripts/crawler/crawl_chain_steps.sh",
        repo_root="/work",
        command_revision="abc123",
    )

    command = activity_command(config, "tier1_collect")

    assert command[:3] == ["python", "/work/pipeline/scripts/crawler/crawl_chain.py", "run-stage"]
    assert command[command.index("--stage") + 1] == "tier1_collect"
    assert "run" not in command[2:]


def test_fixed_shadow_run_id_remains_unchanged() -> None:
    config = CrawlDailyInput(
        run_id="jw-agent-crawl-shadow-fixed",
        command_revision="abc123",
    )

    resolved = temporal_contract.resolve_execution_config(
        config,
        temporal_run_id="019f8c72-2279-7828-b67e-906261a393f8",
    )

    assert resolved.run_id == "jw-agent-crawl-shadow-fixed"


def test_production_run_id_is_unique_per_temporal_execution() -> None:
    config = CrawlDailyInput(
        run_id="jw-agent-crawl-daily",
        command_revision="abc123",
        use_temporal_run_id=True,
    )

    first = temporal_contract.resolve_execution_config(
        config,
        temporal_run_id="019f8c72-2279-7828-b67e-906261a393f8",
    )
    replay = temporal_contract.resolve_execution_config(
        config,
        temporal_run_id="019f8c72-2279-7828-b67e-906261a393f8",
    )
    next_day = temporal_contract.resolve_execution_config(
        config,
        temporal_run_id="019f91a0-1170-7a75-b03f-80734a30b402",
    )

    assert first.run_id == replay.run_id
    assert first.run_id == "jw-agent-crawl-daily-019f8c72-2279-7828-b67e-906261a393f8"
    assert next_day.run_id == "jw-agent-crawl-daily-019f91a0-1170-7a75-b03f-80734a30b402"
    assert first.run_id != next_day.run_id
    assert first.use_temporal_run_id is False


def test_dependency_sequence_never_schedules_tier2_after_tier1_failure() -> None:
    seen: list[str] = []

    async def execute(stage: str) -> dict[str, str]:
        seen.append(stage)
        if stage == "tier1_collect":
            raise StageGateError(stage, "reported_failures", "injected failures=1")
        return {"stage": stage}

    with pytest.raises(StageGateError):
        asyncio.run(run_dependency_sequence(execute))

    assert seen == ["capture_exposure_baseline", "tier1_collect"]
    assert not any(stage.startswith("tier2") for stage in seen)
    assert ACTIVITY_STAGES == (
        "capture_exposure_baseline",
        "tier1_collect",
        "tier1_classify",
        "tier2_collect",
        "tier2_classify_and_refresh",
    )
    assert INTERNAL_STAGE_BY_ACTIVITY["tier2_collect"] == "tier2_collect_exact"


def test_content_addressed_baseline_is_order_independent_and_receipt_backed(
    tmp_path: Path,
) -> None:
    rows = [
        {"brand_canonical": "브랜드B", "news_id": "news-2"},
        {"brand_canonical": "브랜드A", "news_id": "news-1"},
        {"brand_canonical": "브랜드A", "news_id": "news-1"},
    ]

    first = write_content_addressed_baseline(
        state_root=tmp_path,
        run_id="jw-agent-crawl-shadow-20260723-a",
        rows=rows,
        eligibility_revision="eligibility-sha",
        captured_at="2026-07-23T00:00:00+00:00",
    )
    second = write_content_addressed_baseline(
        state_root=tmp_path,
        run_id="jw-agent-crawl-shadow-20260723-a",
        rows=reversed(rows),
        eligibility_revision="eligibility-sha",
        captured_at="2026-07-23T00:01:00+00:00",
    )

    assert first["content_sha256"] == second["content_sha256"]
    assert first["snapshot_path"] == second["snapshot_path"]
    assert first["brand_count"] == 2
    assert first["pair_count"] == 2
    snapshot = json.loads(Path(first["snapshot_path"]).read_text(encoding="utf-8"))
    assert snapshot["brands"] == [
        {"brand_canonical": "브랜드A", "news_ids": ["news-1"]},
        {"brand_canonical": "브랜드B", "news_ids": ["news-2"]},
    ]
    pointer = json.loads(
        (tmp_path / "runs" / "jw-agent-crawl-shadow-20260723-a" / "baseline.json").read_text(
            encoding="utf-8"
        )
    )
    assert pointer["content_sha256"] == first["content_sha256"]
    assert pointer["snapshot_path"] == first["snapshot_path"]
