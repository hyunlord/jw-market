"""Stage registry: the canonical six-stage chain and its builder commands.

Incremental capability is declared per stage and must stay honest:

* ``native_hash`` — the builder itself skips unchanged inputs (input_hash);
  the incremental invocation is the same command, the builder pays only for
  new/changed rows.
* ``new_brands`` — the orchestrator detects brand_keys present in the mart
  universe but missing from the stage's target table and scopes the builder
  to those brands.
* ``market_epoch`` — brand-level increments are not supported by design
  (the builder loads market-scoped frames); increments happen at mart-epoch
  granularity: same epoch -> no-op, changed epoch -> full rebuild.
* ``full_only`` — no increment smaller than the stage's atomic promotion
  unit exists; the reason is recorded on the spec.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Callable

PY = sys.executable or "python3"

AGENT3_REV_ENV = "AGENT3_WORKFLOW_REV"
AGENT3_EXPECTED_REV_ENV = "AGENT3_EXPECTED_WORKFLOW_REV"


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    purpose: str
    writes_live: bool


@dataclass(frozen=True)
class StageSpec:
    key: str
    description: str
    deps: tuple[str, ...]
    incremental: str  # native_hash | new_brands | market_epoch | full_only
    incremental_reason: str
    supports_brands: bool
    # SQL pair for new_brands detection (universe minus covered), else None.
    universe_sql: str | None
    covered_sql: str | None
    # (mode, brands, force, run_id, scope_source, scope_market_ids) -> commands
    commands: Callable[..., list[Command]]
    required_env: tuple[str, ...] = field(default=())


def _module_cmd(module: str, *args: str) -> tuple[str, ...]:
    return (PY, "-m", module, *args)


def _brand_args(flag: str, brands: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for brand in brands:
        out.extend([flag, brand])
    return out


def _market_status_commands(
    mode: str,
    brands: tuple[str, ...],
    force: bool,
    run_id: str,
    scope_source: str | None = None,
    scope_market_ids: tuple[str, ...] = (),
) -> list[Command]:
    return [
        Command(
            _module_cmd("pipeline.scripts.etl.build_cache_market_status"),
            "numeric market-status cache rebuild from published strategic mart",
            True,
        )
    ]


def _cache_commands(
    mode: str,
    brands: tuple[str, ...],
    force: bool,
    run_id: str,
    scope_source: str | None = None,
    scope_market_ids: tuple[str, ...] = (),
) -> list[Command]:
    argv = list(_module_cmd("pipeline.scripts.etl.build_cache_deep_analysis_general"))
    argv.extend(_brand_args("--brand", brands))
    return [Command(tuple(argv), "general deep-analysis cache build (staging writer + gates in builder)", True)]


def _forecast_commands(
    mode: str,
    brands: tuple[str, ...],
    force: bool,
    run_id: str,
    scope_source: str | None = None,
    scope_market_ids: tuple[str, ...] = (),
) -> list[Command]:
    argv = list(_module_cmd("pipeline.scripts.etl.ops_forecast_builder"))
    if force:
        argv.append("--force")
    if scope_source is not None:
        argv.extend(["--scope-source", scope_source])
        for market_id in scope_market_ids:
            argv.extend(["--scope-market-id", market_id])
        argv.extend(_brand_args("--brand", brands))
    return [Command(tuple(argv), "forecast staging build (epoch gate + expected-count gates in builder)", False)]


def _strength_commands(
    mode: str,
    brands: tuple[str, ...],
    force: bool,
    run_id: str,
    scope_source: str | None = None,
    scope_market_ids: tuple[str, ...] = (),
) -> list[Command]:
    expected_rev = os.environ.get(AGENT3_EXPECTED_REV_ENV, "")
    argv = list(
        _module_cmd(
            "pipeline.scripts.agent3.run_source",
            "--brand-source",
            "general_all",
            "--mode",
            "full",
            "--expected-workflow-rev",
            expected_rev or "<missing:AGENT3_EXPECTED_WORKFLOW_REV>",
            "--output",
            f"/tmp/agent3_source_{run_id}.json",
        )
    )
    source = {"ubist": "ubist", "iqvia_nsa": "iqvia"}.get(scope_source or "")
    if source is not None:
        argv.extend(["--source", source])
    if brands:
        argv.extend(["--brands", ",".join(brands)])
    return [Command(tuple(argv), "Agent3 strength refresh (input_hash-incremental, rev pin fail-closed)", True)]


def _shortlong_commands(
    mode: str,
    brands: tuple[str, ...],
    force: bool,
    run_id: str,
    scope_source: str | None = None,
    scope_market_ids: tuple[str, ...] = (),
) -> list[Command]:
    commands = []
    for variant in ("short", "long"):
        argv = list(
            _module_cmd(
                "pipeline.scripts.ai_analysis.agent2_regen_orchestrator",
                "--brand-source",
                "general-density",
                "--bundle-kind",
                "general",
                "--dry-run",
                "--analysis-variant",
                variant,
                "--work-dir",
                f"outputs/phase_zeta_agent2_regen_orchestrator/orchestrated_{run_id}_{variant}",
            )
        )
        if brands:
            argv.append("--brands")
            argv.extend(brands)
        commands.append(
            Command(
                tuple(argv),
                f"Agent2 {variant} variant generation into staging (live swap stays a separate, "
                "PL-approved agent2_variant_promotion run)",
                False,
            )
        )
    return commands


def _events_commands(
    mode: str,
    brands: tuple[str, ...],
    force: bool,
    run_id: str,
    scope_source: str | None = None,
    scope_market_ids: tuple[str, ...] = (),
) -> list[Command]:
    module = "pipeline.scripts.etl.cache_refresh.cache_deep_analysis_events_update"
    validate = "pipeline.scripts.etl.cache_refresh.cache_deep_analysis_refresh_validate"
    live = os.environ.get("LIVE_TABLE", "cache_deep_analysis")
    staging = f"cache_deep_analysis_events_staging_{run_id}"
    backup = f"cache_deep_analysis_bak_d2_prev3_{run_id}"
    base = ("--live-table", live, "--staging-table", staging)
    return [
        Command(_module_cmd(module, *base, "--build-staging"), "events: build staging table", False),
        Command(_module_cmd(validate, "--live-table", live, "--staging-table", staging), "events: validate staging vs live", False),
        Command(_module_cmd(module, "--live-table", live, "--backup-table", backup, "--backup-live"), "events: backup live", True),
        Command(_module_cmd(module, *base, "--apply-update"), "events: apply staged update to live", True),
        Command(_module_cmd(module, *base, "--post-verify"), "events: post verify", False),
        Command(_module_cmd(module, *base, "--drop-staging"), "events: drop staging", False),
    ]


def _elements_commands(
    mode: str,
    brands: tuple[str, ...],
    force: bool,
    run_id: str,
    scope_source: str | None = None,
    scope_market_ids: tuple[str, ...] = (),
) -> list[Command]:
    agent3_schema = os.environ.get("AGENT3_DB_NAME", "")
    argv = list(
        _module_cmd(
            "pipeline.scripts.etl.cache_brand_elements",
            "--ensure-table",
            "--pilot-fill",
            "--verify",
            "--agent3-schema",
            agent3_schema or "<missing:AGENT3_DB_NAME>",
        )
    )
    argv.extend(_brand_args("--brand", brands))
    return [Command(tuple(argv), "brand elements cache fill + verify", True)]


STAGES: tuple[StageSpec, ...] = (
    StageSpec(
        key="market_status",
        description="numeric market-status cache rebuild from published mart",
        deps=(),
        incremental="market_epoch",
        incremental_reason="rebuild once for each published numeric mart epoch",
        supports_brands=False,
        universe_sql=None,
        covered_sql=None,
        commands=_market_status_commands,
    ),
    StageSpec(
        key="cache",
        description="cache_deep_analysis_general rebuild from mart",
        deps=(),
        incremental="new_brands",
        incremental_reason="builder supports --brand scoping; new brand_keys are detected against the mart universe",
        supports_brands=True,
        universe_sql="SELECT DISTINCT brand_key FROM mart_general_brand_metric",
        covered_sql="SELECT DISTINCT brand_key FROM cache_deep_analysis_general",
        commands=_cache_commands,
    ),
    StageSpec(
        key="forecast",
        description="deep_forecast_block/horizon staging build (ops builder)",
        deps=("cache",),
        incremental="market_epoch",
        incremental_reason=(
            "full-only below epoch granularity: the builder loads market-scoped frames, so per-brand "
            "increments would recompute whole markets anyway; increment = rebuild when mart epoch changes"
        ),
        supports_brands=False,
        universe_sql=None,
        covered_sql=None,
        commands=_forecast_commands,
    ),
    StageSpec(
        key="strength",
        description="Agent3 brand strength refresh (wf316)",
        deps=("cache",),
        incremental="native_hash",
        incremental_reason="run_source skips rows whose input_hash+workflow_rev match; only new/changed brands call the LLM",
        supports_brands=True,
        universe_sql=None,
        covered_sql=None,
        commands=_strength_commands,
        required_env=(AGENT3_REV_ENV, AGENT3_EXPECTED_REV_ENV),
    ),
    StageSpec(
        key="shortlong",
        description="Agent2 short/long variant generation (staging only)",
        deps=("cache",),
        incremental="native_hash",
        incremental_reason=(
            "agent2_regen_orchestrator stages per-brand outputs and is safe to re-run; live swap is a "
            "separate PL-approved promotion (agent2_variant_promotion), never orchestrated here"
        ),
        supports_brands=True,
        universe_sql=None,
        covered_sql=None,
        commands=_shortlong_commands,
    ),
    StageSpec(
        key="events",
        description="cache_deep_analysis events payload refresh (staging -> validate -> swap)",
        deps=("cache", "strength"),
        incremental="full_only",
        incremental_reason=(
            "full-only: promotion is an atomic staging-table swap of cache_deep_analysis events payloads; "
            "there is no smaller safe unit than one staged run"
        ),
        supports_brands=False,
        universe_sql=None,
        covered_sql=None,
        commands=_events_commands,
    ),
    StageSpec(
        key="elements",
        description="cache_brand_elements fill from agent3 + deep-analysis caches",
        deps=("cache", "strength"),
        incremental="new_brands",
        incremental_reason="builder supports --brand scoping; new brand_keys detected against agent3_brand_strength",
        supports_brands=True,
        universe_sql="SELECT DISTINCT brand_key FROM agent3_brand_strength",
        covered_sql="SELECT DISTINCT brand_key FROM cache_brand_elements",
        commands=_elements_commands,
        required_env=("AGENT3_DB_NAME",),
    ),
)

STAGE_ORDER: tuple[str, ...] = tuple(spec.key for spec in STAGES)
STAGE_BY_KEY: dict[str, StageSpec] = {spec.key: spec for spec in STAGES}

PROFILE_STAGES: dict[str, tuple[str, ...]] = {
    # Cache regeneration is a separate track, not part of refresh profiles.
    "numeric": ("market_status",),
    "agent": ("forecast", "strength", "shortlong", "events", "elements"),
    "all": STAGE_ORDER,
}
