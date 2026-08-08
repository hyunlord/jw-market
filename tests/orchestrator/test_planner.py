from pathlib import Path

import pytest

from fakes import EPOCH, FakeProbe

from pipeline.orchestrator.planner import build_plan, resolve_selection
from pipeline.orchestrator.stages import STAGE_ORDER
from pipeline.orchestrator.state import StateStore


def _state(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / "state.json")


def _complete_all(state: StateStore, epoch: str = EPOCH) -> None:
    for key in STAGE_ORDER:
        state.record(key, status="completed", epoch=epoch)


def test_full_plan_orders_all_six_stages(tmp_path):
    plan = build_plan(mode="full", run_id="t", probe=FakeProbe(), state=_state(tmp_path))

    assert [stage.key for stage in plan.stages] == list(STAGE_ORDER)
    assert all(stage.action == "run" for stage in plan.stages)
    assert plan.epoch == EPOCH


def test_full_plan_is_idempotent_at_same_epoch(tmp_path):
    state = _state(tmp_path)
    _complete_all(state)

    plan = build_plan(mode="full", run_id="t", probe=FakeProbe(), state=state)

    assert all(stage.action == "skip_fresh" for stage in plan.stages)
    assert not plan.runnable


def test_epoch_change_invalidates_completion(tmp_path):
    state = _state(tmp_path)
    _complete_all(state, epoch="b" * 64)

    plan = build_plan(mode="full", run_id="t", probe=FakeProbe(), state=state)

    assert all(stage.action == "run" for stage in plan.stages)


def test_force_overrides_freshness(tmp_path):
    state = _state(tmp_path)
    _complete_all(state)

    plan = build_plan(mode="full", run_id="t", probe=FakeProbe(), state=state, force=True)

    assert all(stage.action == "run" for stage in plan.stages)


def test_incremental_new_brand_detection_beats_state_record(tmp_path):
    state = _state(tmp_path)
    _complete_all(state)
    probe = FakeProbe(new_brands={"SELECT DISTINCT brand_key FROM mart_general_brand_metric": ["신규브랜드"]})

    plan = build_plan(mode="incremental", run_id="t", probe=probe, state=state)

    by_key = {stage.key: stage for stage in plan.stages}
    # Coverage detection is authoritative over the completion record: a brand
    # missing from the target table must be built even at the same epoch.
    assert by_key["cache"].action == "run"
    assert by_key["cache"].scope_brands == ("신규브랜드",)


def test_incremental_new_brand_detection_runs_when_not_fresh(tmp_path):
    probe = FakeProbe(new_brands={"SELECT DISTINCT brand_key FROM mart_general_brand_metric": ["신규브랜드"]})

    plan = build_plan(mode="incremental", run_id="t", probe=probe, state=_state(tmp_path))

    by_key = {stage.key: stage for stage in plan.stages}
    assert by_key["cache"].action == "run"
    assert by_key["cache"].scope_brands == ("신규브랜드",)
    assert any("--brand" in " ".join(command.argv) for command in by_key["cache"].commands)


def test_incremental_no_new_brands_is_noop_for_new_brand_stages(tmp_path):
    plan = build_plan(mode="incremental", run_id="t", probe=FakeProbe(), state=_state(tmp_path))

    by_key = {stage.key: stage for stage in plan.stages}
    assert by_key["cache"].action == "skip_fresh"
    assert by_key["elements"].action == "skip_fresh"
    # native_hash stages still run — the builder itself skips unchanged inputs;
    # cache being verified-current satisfies their dependency.
    assert by_key["strength"].action == "run"


def test_partial_run_blocks_on_stale_upstream(tmp_path):
    plan = build_plan(mode="full", run_id="t", probe=FakeProbe(), state=_state(tmp_path), stages_csv="strength")

    by_key = {stage.key: stage for stage in plan.stages}
    assert by_key["strength"].action == "blocked"
    assert "upstream" in by_key["strength"].reason
    assert by_key["cache"].action == "skip_unselected"


def test_partial_run_force_overrides_stale_upstream(tmp_path):
    plan = build_plan(
        mode="full", run_id="t", probe=FakeProbe(), state=_state(tmp_path), stages_csv="strength", force=True
    )

    by_key = {stage.key: stage for stage in plan.stages}
    assert by_key["strength"].action == "run"
    assert by_key["strength"].forced is True
    assert any("--force" in warning or "force" in warning for warning in plan.warnings)


def test_partial_run_allowed_when_upstream_completed_at_epoch(tmp_path):
    state = _state(tmp_path)
    state.record("cache", status="completed", epoch=EPOCH)

    plan = build_plan(mode="full", run_id="t", probe=FakeProbe(), state=state, stages_csv="strength")

    by_key = {stage.key: stage for stage in plan.stages}
    assert by_key["strength"].action == "run"


def test_from_stage_selects_suffix_and_uses_state_for_upstream(tmp_path):
    state = _state(tmp_path)
    state.record("cache", status="completed", epoch=EPOCH)
    state.record("forecast", status="completed", epoch=EPOCH)

    plan = build_plan(mode="full", run_id="t", probe=FakeProbe(), state=state, from_stage="strength")

    by_key = {stage.key: stage for stage in plan.stages}
    assert by_key["cache"].action == "skip_unselected"
    assert by_key["forecast"].action == "skip_unselected"
    assert by_key["strength"].action == "run"
    assert by_key["events"].action == "run"  # strength satisfied within the run set


def test_brand_scope_skips_stages_without_brand_granularity(tmp_path):
    plan = build_plan(mode="incremental", run_id="t", probe=FakeProbe(), state=_state(tmp_path), brands=("리바로",))

    by_key = {stage.key: stage for stage in plan.stages}
    assert by_key["forecast"].action == "skip_no_brand_scope"
    assert by_key["events"].action == "skip_no_brand_scope"
    assert by_key["cache"].action == "run"
    assert by_key["cache"].scope_brands == ("리바로",)
    assert by_key["strength"].action == "run"
    assert any("리바로" in part for command in by_key["strength"].commands for part in command.argv)


def test_probe_unavailable_dry_run_plans_with_warning(tmp_path):
    plan = build_plan(mode="full", run_id="t", probe=FakeProbe(available=False), state=_state(tmp_path), dry_run=True)

    assert plan.epoch is None
    assert plan.warnings
    assert all(stage.action in ("run", "skip_unselected", "skip_no_brand_scope") for stage in plan.stages)


def test_probe_unavailable_execution_blocks(tmp_path):
    plan = build_plan(mode="full", run_id="t", probe=FakeProbe(available=False), state=_state(tmp_path))

    assert plan.blocked


def test_selection_validation():
    with pytest.raises(ValueError, match="mutually exclusive"):
        resolve_selection("cache", "strength")
    with pytest.raises(ValueError, match="unknown stages"):
        resolve_selection("nope", None)
    assert resolve_selection(None, "events") == ("events", "elements")
    assert resolve_selection("elements,cache", None) == ("cache", "elements")


def test_profiles_cover_the_full_chain_without_sharing_general_cache(tmp_path):
    numeric = build_plan(
        mode="incremental",
        run_id="numeric",
        probe=FakeProbe(),
        state=_state(tmp_path),
        profile="numeric",
        force=True,
    )
    agent = build_plan(
        mode="incremental",
        run_id="agent",
        probe=FakeProbe(),
        state=_state(tmp_path),
        profile="agent",
        force=True,
    )

    numeric_keys = {stage.key for stage in numeric.runnable}
    agent_keys = {stage.key for stage in agent.runnable}
    assert numeric_keys == {"market_status"}
    assert agent_keys == {"cache", "forecast", "strength", "shortlong", "events", "elements"}
    assert not numeric_keys & agent_keys
    assert numeric_keys | agent_keys == set(STAGE_ORDER)
