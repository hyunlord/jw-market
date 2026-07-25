from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from pipeline.scripts.ai_analysis.agent2_density_worklist import UnknownEventBrandError
from pipeline.scripts.crawler.agent2_hook import (
    Agent2HookCost,
    build_agent2_generation_plan,
    detect_increased_brands_from_rows,
)
from pipeline.scripts.crawler.agent2_hook_receipt import (
    read_detection_receipt,
    write_detection_receipt,
)
from pipeline.scripts.crawler.agent2_hook_runtime import (
    Agent2CommandRequest,
    build_agent2_command,
)


def _brand_rows() -> list[dict[str, object]]:
    return [
        {
            "brand_key": "brand-livalo",
            "brand_name": "리바로",
            "raw_value_history": "[]",
        },
        {
            "brand_key": "brand-rosuzet",
            "brand_name": "로수젯",
            "raw_value_history": "[]",
        },
    ]


def _score_row(
    news_id: str,
    *,
    published_date: date,
    brand: str = "리바로",
) -> dict[str, object]:
    return {
        "news_id": news_id,
        "brand_canonical": brand,
        "brand_name": brand,
        "mirrored_from_jw_brands": "[]",
        "source_processor": "workflow_196_rev5674",
        "derivation": "llm_direct",
        "tag": "자본/경영",
        "score": 60,
        "published_date": published_date,
        "joined_news_id": news_id,
    }


def test_detector_targets_equal_count_replacement_when_effective_evidence_changes() -> None:
    # Given: the pre-crawl baseline has one eligible item and the effective
    # post-crawl selection has one different item.
    baseline = {"리바로": frozenset({"before-news"})}
    score_rows = [
        _score_row("before-news", published_date=date(2026, 7, 1)),
        _score_row("after-news", published_date=date(2026, 7, 2)),
    ]

    # When: the central selector is capped at one direct item.
    result = detect_increased_brands_from_rows(
        brand_rows=_brand_rows(),
        score_rows=score_rows,
        baseline_news_ids_by_brand=baseline,
        snapshot_date=date(2026, 7, 25),
        direct_cap=1,
        cross_cap=1,
    )

    # Then: count equality does not hide the replacement.
    assert result.target_count == 1
    assert result.targets[0].brand_key == "brand-livalo"
    assert result.targets[0].effective_added_news_ids == ("after-news",)
    assert result.targets[0].selected_news_ids == ("after-news",)


def test_detector_hard_fails_an_unregistered_brand_alias() -> None:
    # Given: a central-eligible row carries a brand alias absent from the registry.
    score_rows = [
        _score_row(
            "unknown-news",
            published_date=date(2026, 7, 2),
            brand="미등재",
        )
    ]

    # When / Then: detector construction fails closed before Agent2 planning.
    with pytest.raises(UnknownEventBrandError, match="미등재"):
        detect_increased_brands_from_rows(
            brand_rows=_brand_rows(),
            score_rows=score_rows,
            baseline_news_ids_by_brand={},
            snapshot_date=date(2026, 7, 25),
        )


def test_detector_ignores_baseline_only_names_outside_current_universe() -> None:
    # Given: an old baseline contains a brand no longer present in the mart.
    baseline = {
        "과거제품": frozenset({"old-news"}),
        "리바로": frozenset(),
    }

    # When: current central evidence contains only a registered brand.
    result = detect_increased_brands_from_rows(
        brand_rows=_brand_rows(),
        score_rows=[
            _score_row("after-news", published_date=date(2026, 7, 2))
        ],
        baseline_news_ids_by_brand=baseline,
        snapshot_date=date(2026, 7, 25),
    )

    # Then: historical universe drift does not mask current evidence.
    assert result.target_count == 1
    assert result.targets[0].brand_key == "brand-livalo"


def test_detector_does_not_retrigger_existing_cross_evidence() -> None:
    # Given: the same eligible news already existed under another source name
    # before a cross-match projected it onto the current brand.
    baseline = {"로수젯": frozenset({"existing-cross-news"})}
    cross = _score_row(
        "existing-cross-news",
        published_date=date(2026, 7, 2),
        brand="로수젯",
    )
    cross["derivation"] = "cross_match"
    cross["source_processor"] = "cross_match_adapter_v1"
    cross["mirrored_from_jw_brands"] = '["리바로"]'

    # When: the central selector sees the mirrored cross evidence.
    result = detect_increased_brands_from_rows(
        brand_rows=_brand_rows(),
        score_rows=[cross],
        baseline_news_ids_by_brand=baseline,
        snapshot_date=date(2026, 7, 25),
    )

    # Then: brand projection alone does not create a new-news trigger.
    assert result.target_count == 0


def test_detection_receipt_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    # Given: one deterministic detector result.
    result = detect_increased_brands_from_rows(
        brand_rows=_brand_rows(),
        score_rows=[
            _score_row("after-news", published_date=date(2026, 7, 2))
        ],
        baseline_news_ids_by_brand={"리바로": frozenset()},
        snapshot_date=date(2026, 7, 25),
    )

    # When: the same run writes the same receipt twice.
    first = write_detection_receipt(
        state_root=tmp_path,
        run_id="jw-agent-hook-test",
        result=result,
    )
    second = write_detection_receipt(
        state_root=tmp_path,
        run_id="jw-agent-hook-test",
        result=result,
    )

    # Then: immutable content is reused and only the pointer reports a hit.
    assert first["content_sha256"] == second["content_sha256"]
    assert first["receipt_hit"] is False
    assert second["receipt_hit"] is True
    assert Path(str(first["receipt_path"])).is_file()


def test_detection_receipt_rejects_tampered_content(tmp_path: Path) -> None:
    # Given: a persisted receipt whose immutable content is modified afterward.
    result = detect_increased_brands_from_rows(
        brand_rows=_brand_rows(),
        score_rows=[
            _score_row("after-news", published_date=date(2026, 7, 2))
        ],
        baseline_news_ids_by_brand={"리바로": frozenset()},
        snapshot_date=date(2026, 7, 25),
    )
    receipt = write_detection_receipt(
        state_root=tmp_path,
        run_id="jw-agent-hook-tamper",
        result=result,
    )
    Path(str(receipt["receipt_path"])).write_text("{}\n", encoding="utf-8")

    # When / Then: the pointer cannot load content whose hash changed.
    pointer_path = (
        tmp_path / "runs" / "jw-agent-hook-tamper" / "agent2_detection.json"
    )
    with pytest.raises(RuntimeError, match="hash mismatch"):
        read_detection_receipt(pointer_path)


def test_agent2_plan_blocks_calls_when_shadow_limit_is_zero() -> None:
    # Given: one selected brand and an explicit per-call estimate.
    result = detect_increased_brands_from_rows(
        brand_rows=_brand_rows(),
        score_rows=[
            _score_row("after-news", published_date=date(2026, 7, 2))
        ],
        baseline_news_ids_by_brand={"리바로": frozenset()},
        snapshot_date=date(2026, 7, 25),
    )

    # When: the shadow call limit is zero.
    plan = build_agent2_generation_plan(
        result,
        llm_call_limit=0,
        cost=Agent2HookCost(estimated_usd_per_call=Decimal("0.25")),
    )

    # Then: target sizing is visible but no wf217 call is permitted.
    assert plan.target_count == 1
    assert plan.expected_llm_calls == 1
    assert plan.allowed_llm_calls == 0
    assert plan.estimated_cost_usd == Decimal("0.25")
    assert plan.execution_mode == "selection_only"


def test_agent2_command_routes_exact_brand_keys_without_live_swap(
    tmp_path: Path,
) -> None:
    command = build_agent2_command(
        Agent2CommandRequest(
            repo_root=tmp_path,
            state_root=tmp_path / "state",
            content_sha256="a" * 64,
            brand_keys=("brand-livalo", "brand-rosuzet"),
            snapshot_at="2026-07-25T00:00:00+00:00",
        )
    )

    assert "--dry-run" in command
    assert "--apply" not in command
    assert command[command.index("--brand-source") + 1] == "general-density"
    assert command[command.index("--bundle-kind") + 1] == "general"
    keys_path = Path(command[command.index("--brand-keys-file") + 1])
    assert keys_path.read_text(encoding="utf-8") == (
        '[\n  "brand-livalo",\n  "brand-rosuzet"\n]\n'
    )


def test_agent2_command_reuses_one_durable_idempotency_ledger(
    tmp_path: Path,
) -> None:
    # Given: two crawl receipts whose unchanged brand may have the same bundle.
    requests = [
        Agent2CommandRequest(
            repo_root=tmp_path,
            state_root=tmp_path / "state",
            content_sha256=character * 64,
            brand_keys=("brand-livalo",),
            snapshot_at="2026-07-25T00:00:00+00:00",
        )
        for character in ("a", "b")
    ]

    # When: commands are built for both receipt identities.
    commands = [build_agent2_command(request) for request in requests]

    # Then: inputs stay content-addressed while wf217 success keys share a ledger.
    key_paths = [
        Path(command[command.index("--brand-keys-file") + 1])
        for command in commands
    ]
    work_dirs = [
        Path(command[command.index("--work-dir") + 1])
        for command in commands
    ]
    assert key_paths[0] != key_paths[1]
    assert work_dirs[0] == work_dirs[1]
    assert work_dirs[0] == tmp_path / "state" / "agent2-hook" / "generation"
