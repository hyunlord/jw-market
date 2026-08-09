"""Turns mode + selection + probe results into an execution plan."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from pipeline.orchestrator.stages import (
    PROFILE_STAGES,
    STAGE_BY_KEY,
    STAGE_ORDER,
    Command,
    StageSpec,
)
from pipeline.orchestrator.state import StateStore

MODES = ("full", "incremental")


@dataclass
class StagePlan:
    key: str
    action: str  # run | skip_fresh | skip_unselected | skip_no_brand_scope | blocked
    reason: str
    commands: list[Command] = field(default_factory=list)
    scope_brands: tuple[str, ...] = ()
    forced: bool = False


@dataclass
class Plan:
    mode: str
    run_id: str
    epoch: str | None
    stages: list[StagePlan] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> list[StagePlan]:
        return [stage for stage in self.stages if stage.action == "blocked"]

    @property
    def runnable(self) -> list[StagePlan]:
        return [stage for stage in self.stages if stage.action == "run"]

    def to_json(self) -> dict:
        return {
            "mode": self.mode,
            "run_id": self.run_id,
            "epoch": self.epoch,
            "warnings": list(self.warnings),
            "stages": [
                {
                    "stage": stage.key,
                    "action": stage.action,
                    "reason": stage.reason,
                    "forced": stage.forced,
                    "scope_brands": list(stage.scope_brands),
                    "incremental": STAGE_BY_KEY[stage.key].incremental,
                    "commands": [
                        {"argv": list(command.argv), "purpose": command.purpose, "writes_live": command.writes_live}
                        for command in stage.commands
                    ],
                }
                for stage in self.stages
            ],
        }


def resolve_selection(stages_csv: str | None, from_stage: str | None) -> tuple[str, ...]:
    if stages_csv and from_stage:
        raise ValueError("--stages and --from-stage are mutually exclusive")
    if stages_csv:
        keys = tuple(key.strip() for key in stages_csv.split(",") if key.strip())
        unknown = [key for key in keys if key not in STAGE_BY_KEY]
        if unknown:
            raise ValueError(f"unknown stages: {unknown}; valid: {list(STAGE_ORDER)}")
        return tuple(key for key in STAGE_ORDER if key in keys)
    if from_stage:
        if from_stage not in STAGE_BY_KEY:
            raise ValueError(f"unknown stage {from_stage!r}; valid: {list(STAGE_ORDER)}")
        index = STAGE_ORDER.index(from_stage)
        return STAGE_ORDER[index:]
    return STAGE_ORDER


def build_plan(
    *,
    mode: str,
    run_id: str,
    probe,
    state: StateStore,
    stages_csv: str | None = None,
    from_stage: str | None = None,
    brands: tuple[str, ...] = (),
    force: bool = False,
    dry_run: bool = False,
    profile: str = "all",
    scope_source: str | None = None,
    scope_market_ids: tuple[str, ...] = (),
    brands_file: str | None = None,
) -> Plan:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; valid: {list(MODES)}")
    if profile not in PROFILE_STAGES:
        raise ValueError(f"unknown profile {profile!r}; valid: {sorted(PROFILE_STAGES)}")
    if scope_source not in {None, "ubist", "iqvia_nsa"}:
        raise ValueError(f"unsupported scope source: {scope_source!r}")
    if scope_market_ids and scope_source is None:
        raise ValueError("scope market IDs require --scope-source")
    if scope_source is not None and not brands:
        raise ValueError("scoped agent runs require resolved brand keys")
    if profile != "all" and (stages_csv or from_stage):
        raise ValueError("--profile cannot be combined with --stages or --from-stage")
    selected = (
        PROFILE_STAGES[profile]
        if profile != "all"
        else resolve_selection(stages_csv, from_stage)
    )

    epoch: str | None
    try:
        epoch = probe.current_epoch()
    except Exception as exc:  # noqa: BLE001 - probe failure is a planning fact
        epoch = None
        message = f"mart epoch probe unavailable ({exc}); freshness unknown"
        if dry_run:
            plan_warning = message + " - dry-run continues with unknown freshness"
        else:
            plan_warning = message + " - execution will fail closed"
        plan = Plan(mode=mode, run_id=run_id, epoch=None, warnings=[plan_warning])
        _fill_without_epoch(
            plan,
            selected,
            brands,
            force,
            dry_run,
            run_id,
            scope_source,
            scope_market_ids,
            brands_file,
        )
        return plan

    plan = Plan(mode=mode, run_id=run_id, epoch=epoch)
    for key in STAGE_ORDER:
        spec = STAGE_BY_KEY[key]
        if key not in selected:
            plan.stages.append(StagePlan(key=key, action="skip_unselected", reason="not selected"))
            continue
        plan.stages.append(
            _plan_stage(
                spec,
                mode,
                epoch,
                probe,
                state,
                brands,
                force,
                run_id,
                plan,
                scope_source,
                scope_market_ids,
                brands_file,
            )
        )

    _validate_dependencies(plan, state, force)
    return plan


def _plan_stage(
    spec: StageSpec,
    mode: str,
    epoch: str,
    probe,
    state: StateStore,
    brands: tuple[str, ...],
    force: bool,
    run_id: str,
    plan: Plan,
    scope_source: str | None,
    scope_market_ids: tuple[str, ...],
    brands_file: str | None,
) -> StagePlan:
    supports_requested_scope = spec.supports_brands or (
        spec.key == "forecast" and scope_source is not None
    )
    if brands and not supports_requested_scope:
        return StagePlan(
            key=spec.key,
            action="skip_no_brand_scope",
            reason=f"brand-scoped run requested but stage is {spec.incremental}: {spec.incremental_reason}",
        )

    scope: tuple[str, ...] = brands
    reason = f"{mode} run"
    if mode == "incremental" and not brands and spec.incremental == "new_brands":
        # Coverage detection is authoritative over the state record: a brand
        # missing from the target table must be built even if a completion
        # record exists for the current epoch.
        assert spec.universe_sql and spec.covered_sql
        new_keys = tuple(probe.new_brand_keys(spec.universe_sql, spec.covered_sql))
        if not new_keys and not force:
            return StagePlan(
                key=spec.key,
                action="skip_fresh",
                reason="incremental: no new brand_keys against target coverage",
            )
        scope = new_keys
        reason = f"incremental: {len(new_keys)} new brand_key(s) detected"
    else:
        fresh = state.completed_at_epoch(spec.key, epoch)
        if fresh and not force and not brands:
            return StagePlan(
                key=spec.key,
                action="skip_fresh",
                reason=f"already completed at current mart epoch {epoch[:12]}… (idempotent no-op; use --force to rebuild)",
            )
        if mode == "incremental" and not brands:
            if spec.incremental == "native_hash":
                reason = "incremental: builder-native hash skip (only new/changed inputs pay)"
            else:
                reason = f"incremental at {spec.incremental} granularity: {spec.incremental_reason}"

    missing_env = [name for name in spec.required_env if not os.environ.get(name)]
    if missing_env:
        plan.warnings.append(f"stage {spec.key}: required env missing: {missing_env} (execution fails closed)")

    return StagePlan(
        key=spec.key,
        action="run",
        reason=reason,
        commands=spec.commands(
            mode,
            scope,
            force,
            run_id,
            scope_source,
            scope_market_ids,
            brands_file,
        ),
        scope_brands=scope,
        forced=force,
    )


def _fill_without_epoch(
    plan: Plan,
    selected: tuple[str, ...],
    brands: tuple[str, ...],
    force: bool,
    dry_run: bool,
    run_id: str,
    scope_source: str | None,
    scope_market_ids: tuple[str, ...],
    brands_file: str | None,
) -> None:
    for key in STAGE_ORDER:
        spec = STAGE_BY_KEY[key]
        if key not in selected:
            plan.stages.append(StagePlan(key=key, action="skip_unselected", reason="not selected"))
        elif not dry_run:
            plan.stages.append(StagePlan(key=key, action="blocked", reason="epoch unknown: refusing to execute (fail-closed)"))
        elif brands and not spec.supports_brands:
            plan.stages.append(
                StagePlan(key=key, action="skip_no_brand_scope", reason=f"stage is {spec.incremental}")
            )
        else:
            plan.stages.append(
                StagePlan(
                    key=key,
                    action="run",
                    reason="dry-run plan with unknown freshness (probe unavailable)",
                    commands=spec.commands(
                        plan.mode,
                        brands,
                        force,
                        run_id,
                        scope_source,
                        scope_market_ids,
                        brands_file,
                    ),
                    scope_brands=brands,
                )
            )


def _validate_dependencies(plan: Plan, state: StateStore, force: bool) -> None:
    if plan.epoch is None:
        return
    # A stage verified current (skip_fresh) satisfies its dependents just like
    # one that runs in this plan.
    satisfied = {stage.key for stage in plan.stages if stage.action in ("run", "skip_fresh")}
    for stage in plan.stages:
        if stage.action != "run":
            continue
        spec = STAGE_BY_KEY[stage.key]
        stale = [
            dep
            for dep in spec.deps
            if dep not in satisfied and not state.completed_at_epoch(dep, plan.epoch)
        ]
        if not stale:
            continue
        if force:
            stage.forced = True
            plan.warnings.append(
                f"stage {stage.key}: upstream {stale} not completed at current epoch - proceeding due to --force "
                "(override recorded in run log and state)"
            )
        else:
            stage.action = "blocked"
            stage.reason = (
                f"upstream stage(s) {stale} have no completed record at the current mart epoch; "
                "run them first or override with --force"
            )
            stage.commands = []
