#!/usr/bin/env -S uv run --script
# noqa: SIZE_OK - Single audit CLI keeps dry-run, execute, report, and package ordering traceable for this bounded PoC.
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "httpx2[http2,brotli,zstd]",
#     "pymysql",
#     "rich",
#     "typer",
# ]
# ///

from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
import time

import typer
from rich.console import Console


REPO_ROOT = Path(__file__).resolve().parents[5]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline.scripts.analysis.brand_activity.auto_topic.audit import (  # noqa: E402
    DEFAULT_AUDIT_DIR,
    DEFAULT_DOCS_DIR,
    create_zip_package,
    generated_files,
    raw_text_scan,
    write_git_status,
    write_json,
    write_manifest,
    write_text,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.data_source import (  # noqa: E402
    SCHEMA,
    connect_mariadb,
    fetch_csd_market_bridge,
    fetch_keyword_atc4,
    fetch_keyword_rows,
    fetch_snapshot,
    fetch_topic_covered_atc4,
    load_alias_descriptions,
    load_json_file,
    market_stats,
    read_env_file,
    resolve_alias_source,
    resolve_dictionary_source,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.execution import (  # noqa: E402
    build_call_plan,
    execute_calls,
    execution_summary,
    skipped_execution,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.label_rules import label_quality_summary  # noqa: E402
from pipeline.scripts.analysis.brand_activity.auto_topic.market_groups import (  # noqa: E402
    apply_csd_market_names,
    build_market_group_map,
    scope_metadata_from_group_map,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.market_scope import (  # noqa: E402
    parse_target_markets,
    parse_target_mode,
    select_target_markets,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.models import JsonValue, KeywordRow  # noqa: E402
from pipeline.scripts.analysis.brand_activity.auto_topic.privacy import redacted_rows_for_audit  # noqa: E402
from pipeline.scripts.analysis.brand_activity.auto_topic.prompts import prompt_template_manifest  # noqa: E402
from pipeline.scripts.analysis.brand_activity.auto_topic.quality import quality_summary  # noqa: E402
from pipeline.scripts.analysis.brand_activity.auto_topic.reports import (  # noqa: E402
    render_pipeline_md,
    render_quality_md,
    render_stability_md,
    report_payload,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.sampling import (  # noqa: E402
    DEFAULT_BRANDS_PER_MARKET,
    build_market_samples,
    large_scopes_by_row_count,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.source_sanitize import sanitize_source_text_carryover  # noqa: E402
from pipeline.scripts.analysis.brand_activity.auto_topic.static_quality import inspect_package  # noqa: E402
from pipeline.scripts.analysis.brand_activity.auto_topic.topic_store import load_artifacts  # noqa: E402
from pipeline.scripts.analysis.brand_activity.auto_topic.topic_store_db import (  # noqa: E402
    ensure_store_summary_nonzero,
    save_artifacts,
    store_summary_json,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.verification import write_verification_file  # noqa: E402
from pipeline.scripts.analysis.brand_activity.auto_topic.viz import build_viz_payload, render_html  # noqa: E402


CONSOLE = Console()


class SafetyError(RuntimeError):
    """Raised when the requested run would violate the analysis-only contract."""


def main(
    dry_run: bool = typer.Option(False, "--dry-run", help="Build plans, reports, and audit without GenOS calls."),
    execute: bool = typer.Option(False, "--execute", help="Run bounded real GenOS calls."),
    tag: str = typer.Option("", "--tag", help="Artifact tag. Defaults to local timestamp."),
    max_real_calls: int = typer.Option(86, "--max-real-calls", min=0, help="Hard upper bound for real GenOS calls."),
    axis_per_brand: int = typer.Option(12, "--axis-per-brand", min=3, max=40, help="Rows sampled per selected brand for market axes."),
    axis_rows_cap: int = typer.Option(240, "--axis-rows-cap", min=30, max=300, help="Hard cap for total sampled rows in each market-axis prompt."),
    brand_rows: int = typer.Option(15, "--brand-rows", min=3, max=60, help="Rows sampled per brand-share call."),
    brands_per_market: int | None = typer.Option(DEFAULT_BRANDS_PER_MARKET, "--brands-per-market", min=1, help="Optional storage cap. Omit to include every keyword-bearing brand."),
    axis_lookback_months: int = typer.Option(12, "--axis-lookback-months", min=1, max=36, help="Rolling month window for market-axis and brand-specific topic generation."),
    large_market_limit: int = typer.Option(3, "--large-market-limit", min=0, max=3, help="Large markets receiving Pro/Lite recheck."),
    full_rows: bool = typer.Option(True, "--full-rows/--capped-rows", help="Use all rows for selected scopes, with chunking/batching for GenOS calls."),
    axis_chunk_token_budget: int = typer.Option(8000, "--axis-chunk-token-budget", min=1000, max=20000, help="Estimated input-token cap for one market-axis chunk."),
    brand_batch_token_budget: int = typer.Option(8000, "--brand-batch-token-budget", min=1000, max=20000, help="Estimated input-token cap for one brand-share batch."),
    token_env: str = typer.Option("GENOS_BEARER_TOKEN", "--token-env", help="Environment variable containing the GenOS bearer token."),
    docs_dir: Path = typer.Option(DEFAULT_DOCS_DIR, "--docs-dir", help="Output docs directory."),
    audit_dir: Path = typer.Option(DEFAULT_AUDIT_DIR, "--audit-dir", help="Output audit directory or audit root."),
    stage_schema: str = typer.Option(SCHEMA, "--stage-schema", help="Allowed read-only stage schema."),
    save_to_db: bool = typer.Option(True, "--save-to-db/--no-save-to-db", help="Upsert measured execute results into isolated API tables."),
    target_mode: str = typer.Option("existing", "--target-mode", help="ATC selector: existing, all, uncovered, or explicit."),
    target_atc4: str = typer.Option("", "--target-atc4", help="Comma-separated ATC4 list used by explicit mode."),
) -> None:
    """Run automated Brand Activity LLM topic analysis."""
    result = run_pipeline(
        dry_run=dry_run,
        execute=execute,
        tag=tag or _timestamp_tag(),
        max_real_calls=max_real_calls,
        axis_per_brand=axis_per_brand,
        axis_rows_cap=axis_rows_cap,
        brand_rows=brand_rows,
        brands_per_market=brands_per_market,
        axis_lookback_months=axis_lookback_months,
        large_market_limit=large_market_limit,
        full_rows=full_rows,
        axis_chunk_token_budget=axis_chunk_token_budget,
        brand_batch_token_budget=brand_batch_token_budget,
        token_env=token_env,
        docs_dir=docs_dir,
        audit_dir=audit_dir,
        stage_schema=stage_schema,
        save_to_db=save_to_db,
        target_mode=target_mode,
        target_atc4=target_atc4,
    )
    CONSOLE.print_json(data=result)


def run_pipeline(
    *,
    dry_run: bool,
    execute: bool,
    tag: str,
    max_real_calls: int,
    axis_per_brand: int,
    axis_rows_cap: int,
    brand_rows: int,
    brands_per_market: int | None,
    axis_lookback_months: int,
    large_market_limit: int,
    full_rows: bool,
    axis_chunk_token_budget: int,
    brand_batch_token_budget: int,
    token_env: str,
    docs_dir: Path,
    audit_dir: Path,
    stage_schema: str,
    save_to_db: bool,
    target_mode: str = "existing",
    target_atc4: str = "",
) -> dict[str, JsonValue]:
    """Run read-only data collection, optional bounded GenOS calls, reports, and packaging."""
    _safety_preflight(stage_schema)
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    docs_dir.mkdir(parents=True, exist_ok=True)
    run_audit_dir = _audit_run_dir(audit_dir, tag)
    run_audit_dir.mkdir(parents=True, exist_ok=True)
    dictionary_path, dictionary_source = resolve_dictionary_source()
    dictionary = load_json_file(dictionary_path) if dictionary_path else {}
    alias_path, alias_source = resolve_alias_source()
    alias_payload = load_json_file(alias_path) if alias_path else {}
    before_snapshot, rows, csd_bridge, after_snapshot, target_selection = _load_stage_rows(
        target_mode=target_mode,
        target_atc4=target_atc4,
        stage_schema=stage_schema,
    )
    markets = tuple(str(value) for value in target_selection["selected_atc4"])
    descriptions = load_alias_descriptions(alias_payload, rows)
    group_map = apply_csd_market_names(build_market_group_map(markets), csd_bridge)
    if _dict(group_map.get("sanity_checks")).get("status") != "pass":
        raise SafetyError(f"MI Master group sanity failed: {group_map.get('sanity_checks')}")
    scope_metadata = scope_metadata_from_group_map(group_map)
    samples = build_market_samples(rows, markets, descriptions, axis_per_brand=axis_per_brand, axis_rows_cap=axis_rows_cap, brand_rows=brand_rows, brands_per_market=brands_per_market, full_rows=full_rows, group_map=group_map, axis_lookback_months=axis_lookback_months)
    axis_samples = _typed_samples(samples["axis_samples"])
    brand_samples = _typed_samples(samples["brand_samples"])
    brand_axis_samples = _typed_samples(samples["brand_axis_samples"])
    scope_metadata = _dict(samples.get("scope_metadata")) or scope_metadata
    large_markets = large_scopes_by_row_count(axis_samples, limit=large_market_limit)
    call_plan = build_call_plan(markets=markets, axis_samples=axis_samples, brand_samples=brand_samples, brand_axis_samples=brand_axis_samples, large_markets=large_markets, scope_metadata=scope_metadata, axis_chunk_token_budget=axis_chunk_token_budget, brand_batch_token_budget=brand_batch_token_budget)
    should_execute = execute and not dry_run
    if should_execute and len(call_plan) > max_real_calls:
        # Rationale: bounded PoC must fail before GenOS if scope drifts toward a full brand run.
        raise SafetyError(f"refusing {len(call_plan)} real calls above max_real_calls={max_real_calls}")
    token = os.environ.get(token_env, "")
    if should_execute and not token:
        raise SafetyError(f"missing serving-direct bearer token env: {token_env}")
    auth_mode = "bearer" if token else "dry_run_no_token"
    plan_summary = _plan_summary(call_plan)
    _write_pre_execution_audit(run_audit_dir, before_snapshot, after_snapshot, rows, markets, alias_source, dictionary_source, samples, call_plan, plan_summary, auth_mode, group_map, scope_metadata, csd_bridge, target_selection)
    if should_execute:
        execution = execute_calls(token=token, dictionary=dictionary, axis_samples=axis_samples, brand_samples=brand_samples, brand_axis_samples=brand_axis_samples, descriptions=descriptions, markets=markets, large_markets=large_markets, scope_metadata=scope_metadata, axis_chunk_token_budget=axis_chunk_token_budget, brand_batch_token_budget=brand_batch_token_budget)
    else:
        execution = skipped_execution(dictionary, brand_samples)
    summary = execution_summary(execution, call_plan)
    base_axis_results = {scope_key: summary["axis_results"][scope_key] for scope_key in axis_samples if isinstance(summary.get("axis_results"), dict) and scope_key in summary["axis_results"]}
    flash_brand_results = {key: value for key, value in _dict(summary.get("brand_results")).items() if not _is_tier_result_key(key)}
    quality = quality_summary(base_axis_results, flash_brand_results, large_markets=large_markets, scope_metadata=scope_metadata)
    quality["label_quality"] = label_quality_summary(base_axis_results, flash_brand_results)
    payload = report_payload(
        execution_mode="bounded_real_genos_calls" if should_execute else "dry_run_no_genos_calls",
        auth_mode=auth_mode,
        market_count=len(axis_samples),
        sampled_brand_count=len(brand_samples),
        call_plan=call_plan,
        execution_summary=summary,
        quality=quality,
        group_map=group_map,
        scope_metadata=scope_metadata,
        sample_summary=samples.get("sample_summary") if isinstance(samples.get("sample_summary"), dict) else {},
        plan_summary=plan_summary,
        csd_bridge=csd_bridge,
        open_questions=_open_questions(alias_source, should_execute),
    )
    payload["source_text_sanitize"] = sanitize_source_text_carryover(payload, _all_sampled_rows(axis_samples, brand_samples, brand_axis_samples))
    _write_reports_and_audit(docs_dir, run_audit_dir, payload, rows, summary, auth_mode, token_env, group_map, scope_metadata)
    static_quality = inspect_package(REPO_ROOT / "pipeline/scripts/analysis/brand_activity/auto_topic")
    write_json(run_audit_dir / "static_quality.json", static_quality)
    paths = generated_files(docs_dir, run_audit_dir)
    scan = raw_text_scan(paths, _all_sampled_rows(axis_samples, brand_samples, brand_axis_samples))
    write_json(run_audit_dir / "raw_text_scan.json", scan)
    if scan.get("leak_count") != 0:
        # Rationale: source messages may enter prompts but must never survive into audit/docs/html.
        raise SafetyError(f"raw source text leaked into generated artifacts: {scan.get('leak_count')}")
    manifest = write_manifest(paths, run_audit_dir / "manifest_sha256.csv")
    zip_result = create_zip_package(generated_files(docs_dir, run_audit_dir), tag=tag)
    run_summary = {
        "tag": tag,
        "started_at": started_at,
        "execution_mode": payload["execution_mode"],
        "auth_mode": auth_mode,
        "input_fingerprint": before_snapshot.get("stage_hash_fingerprint"),
        "input_row_count": before_snapshot.get("row_count"),
        "market_count": len(axis_samples),
        "scope_count": len(axis_samples),
        "group_scope_count": len([row for row in scope_metadata.values() if isinstance(row, dict) and row.get("scope_type") == "market_group"]),
        "sampled_brand_count": len(brand_samples),
        "brand_axis_sampled_count": len([rows for rows in brand_axis_samples.values() if rows]),
        "axis_window": samples.get("axis_window"),
        "axis_fallback_scopes": samples.get("axis_fallback_scopes"),
        "planned_call_count": len(call_plan),
        "planned_estimated_input_tokens": plan_summary.get("estimated_input_tokens"),
        "executed_call_count": len(execution.call_logs),
        "large_markets": list(large_markets),
        "quality_grade_distribution": quality.get("grade_distribution"),
        "complex_label_count": _dict(quality.get("label_quality")).get("complex_label_count"),
        "brand_specific_duplicate_pair_count": _dict(quality.get("label_quality")).get("brand_specific_duplicate_pair_count"),
        "group_sanity_checks": group_map.get("sanity_checks"),
        "csd_market_missing_atc4": group_map.get("csd_market_missing_atc4"),
        "dropped_atc4_csd_missing": group_map.get("dropped_atc4_csd_missing"),
        "csd_markets_without_keyword_data": group_map.get("csd_markets_without_keyword_data"),
        "target_selection": target_selection,
        "raw_text_leak_count": scan.get("leak_count"),
        "manifest_entries": len(manifest),
        "static_quality": static_quality,
        "tmp_zip": str(zip_result.tmp_zip),
        "backup_zip": str(zip_result.backup_zip),
        "zip_sha256": zip_result.sha256,
        "open_questions": len(_open_questions(alias_source, should_execute)),
    }
    write_json(run_audit_dir / "run_summary.json", run_summary)
    write_verification_file(run_audit_dir)
    if should_execute and save_to_db:
        db_summary = _save_run_to_db(run_audit_dir, stage_schema, zip_result.sha256)
        write_json(run_audit_dir / "db_save_summary.json", db_summary)
        run_summary["db_save_summary"] = db_summary
        write_json(run_audit_dir / "run_summary.json", run_summary)
    return run_summary


def _safety_preflight(stage_schema: str) -> None:
    """Fail fast on main branch or a non-isolated stage schema."""
    branch = subprocess.run(["git", "branch", "--show-current"], cwd=REPO_ROOT, text=True, capture_output=True, check=False).stdout.strip()
    if branch == "main":
        raise SafetyError("refusing to run on main branch")
    if stage_schema != SCHEMA:
        raise SafetyError(f"refusing schema outside {SCHEMA}: {stage_schema}")


def _load_stage_rows(
    *,
    target_mode: str,
    target_atc4: str,
    stage_schema: str,
) -> tuple[dict[str, JsonValue], list[KeywordRow], dict[str, JsonValue], dict[str, JsonValue], dict[str, JsonValue]]:
    """Resolve ATC targets from live inventories and load their rows read-only."""
    connection = connect_mariadb(read_env_file())
    try:
        before = fetch_snapshot(connection, schema=stage_schema)
        available_markets = fetch_keyword_atc4(connection, schema=stage_schema)
        covered_markets = fetch_topic_covered_atc4(connection, schema=stage_schema)
        mode = parse_target_mode(target_mode)
        markets = select_target_markets(
            available_markets=available_markets,
            covered_markets=covered_markets,
            mode=mode,
            explicit_markets=parse_target_markets(target_atc4),
        )
        if not markets:
            raise SafetyError(f"target selector returned no keyword ATC values: mode={mode}")
        rows = fetch_keyword_rows(connection, markets, schema=stage_schema)
        csd_bridge = fetch_csd_market_bridge(connection, markets, schema=stage_schema)
        after = fetch_snapshot(connection, schema=stage_schema)
    finally:
        connection.close()
    selection: dict[str, JsonValue] = {
        "mode": mode,
        "available_atc4": list(available_markets),
        "covered_atc4": list(covered_markets),
        "selected_atc4": list(markets),
        "selected_existing_overlap": sorted(set(markets) & set(covered_markets)),
    }
    return before, rows, csd_bridge, after, selection


def _write_pre_execution_audit(
    audit_dir: Path,
    before_snapshot: dict[str, JsonValue],
    after_snapshot: dict[str, JsonValue],
    rows: list[KeywordRow],
    markets: tuple[str, ...],
    alias_source: dict[str, JsonValue],
    dictionary_source: dict[str, JsonValue],
    samples: dict[str, JsonValue],
    call_plan: list[dict[str, JsonValue]],
    plan_summary: dict[str, JsonValue],
    auth_mode: str,
    group_map: dict[str, JsonValue],
    scope_metadata: dict[str, JsonValue],
    csd_bridge: dict[str, JsonValue],
    target_selection: dict[str, JsonValue],
) -> None:
    """Write data-source and call-plan audit files before any optional GenOS calls."""
    write_json(audit_dir / "db_snapshot.json", {"before": before_snapshot, "after": after_snapshot, "read_only_equal": before_snapshot == after_snapshot})
    write_json(
        audit_dir / "input_sources.json",
        {
            "raw_keyword_market_count": len(markets),
            "raw_keyword_markets": list(markets),
            "target_selection": target_selection,
            "scope_count": len(scope_metadata),
            "final_scope_keys": list(scope_metadata),
            "group_scope_ids": group_map.get("group_scope_ids"),
            "dropped_atc4_csd_missing": group_map.get("dropped_atc4_csd_missing"),
            "csd_markets_without_keyword_data": group_map.get("csd_markets_without_keyword_data"),
            "alias_source": alias_source,
            "dictionary_path": dictionary_source,
            "read_only_equal": before_snapshot == after_snapshot,
            "sampled_brands_per_market": {market: len(brands) for market, brands in _dict(samples.get("selected_brands")).items()},
        },
    )
    write_json(audit_dir / "market_stats.json", market_stats(rows))
    write_json(audit_dir / "group_map.json", group_map)
    write_json(audit_dir / "csd_market_bridge.json", csd_bridge)
    write_json(audit_dir / "scope_metadata.json", scope_metadata)
    write_json(audit_dir / "sample_summary.json", samples.get("sample_summary"))
    write_json(audit_dir / "call_plan.json", call_plan)
    write_json(audit_dir / "scale_plan_summary.json", plan_summary)
    write_json(audit_dir / "prompt_templates.json", prompt_template_manifest())
    write_json(audit_dir / "credential_check.json", {"auth_mode": auth_mode, "token_value_logged": False, "production_policy": "serving-direct bearer required; token value never logged"})
    write_git_status(audit_dir / "git_status.txt")


def _write_reports_and_audit(
    docs_dir: Path,
    audit_dir: Path,
    payload: dict[str, JsonValue],
    rows: list[KeywordRow],
    summary: dict[str, JsonValue],
    auth_mode: str,
    token_env: str,
    group_map: dict[str, JsonValue],
    scope_metadata: dict[str, JsonValue],
) -> None:
    """Write Markdown, HTML, and sanitized execution audit outputs."""
    write_text(docs_dir / "AUTO_01_QUALITY.md", render_quality_md(payload))
    write_text(docs_dir / "AUTO_02_PIPELINE.md", render_pipeline_md(payload))
    write_text(docs_dir / "AUTO_03_STABILITY_POC.md", render_stability_md(payload))
    write_text(docs_dir / "DESIGN.md", _render_design_tokens())
    write_json(docs_dir / "group_map.json", group_map)
    # Rationale: the static HTML is a reviewer surface and must render only measured payload values.
    write_text(docs_dir / "auto_topic_viz.html", render_html(build_viz_payload(payload)))
    write_json(audit_dir / "call_log_sanitized.json", summary.get("call_logs", []))
    write_json(audit_dir / "axis_results_sanitized.json", summary.get("axis_results", {}))
    write_json(audit_dir / "brand_results_sanitized.json", summary.get("brand_results", {}))
    write_json(audit_dir / "stability_results.json", summary.get("stability_results", {}))
    write_json(audit_dir / "dictionary_baseline.json", summary.get("dictionary_results", {}))
    write_json(audit_dir / "quality_summary.json", payload.get("quality_summary", {}))
    write_json(audit_dir / "source_text_sanitize.json", payload.get("source_text_sanitize", {}))
    write_json(audit_dir / "label_quality_summary.json", _dict(_dict(payload.get("quality_summary")).get("label_quality")))
    write_json(audit_dir / "group_map.json", group_map)
    write_json(audit_dir / "scope_metadata.json", scope_metadata)
    write_json(audit_dir / "viz_payload.json", build_viz_payload(payload))
    write_json(audit_dir / "input_rows_redacted.json", {"rows": redacted_rows_for_audit(rows)})
    write_json(audit_dir / "credential_check.json", {"auth_mode": auth_mode, "token_env": token_env, "token_value_logged": False, "production_policy": "serving-direct bearer required; token value never logged"})


def _plan_summary(call_plan: list[dict[str, JsonValue]]) -> dict[str, JsonValue]:
    """Summarize planned calls, token volume, and rough timing before GenOS execution."""
    by_task: dict[str, int] = {}
    by_model: dict[str, int] = {}
    estimated_tokens = 0
    for row in call_plan:
        task = str(row.get("task") or "")
        model = str(row.get("model_key") or "")
        by_task[task] = by_task.get(task, 0) + 1
        by_model[model] = by_model.get(model, 0) + 1
        value = row.get("estimated_input_tokens")
        if isinstance(value, int | float):
            estimated_tokens += int(value)
    # Rationale: exact GenOS billing is unknown; this is only a bounded preflight estimate for PL.
    return {
        "planned_call_count": len(call_plan),
        "estimated_input_tokens": estimated_tokens,
        "calls_by_task": by_task,
        "calls_by_model": by_model,
        "rough_wall_time_minutes_at_5s_per_call": round(len(call_plan) * 5 / 60, 1),
        "rough_wall_time_minutes_at_15s_per_call": round(len(call_plan) * 15 / 60, 1),
        "vertex_flash_usd_input_only_proxy": round(estimated_tokens / 1_000_000 * 0.50, 4),
    }


def _render_design_tokens() -> str:
    """Render the small local design note for the static visualization artifact."""
    return "\n".join(
        [
            "# DESIGN",
            "",
            "정적 HTML은 운영 대시보드 보조 검토물이다.",
            "",
            "- Layout: 좌측 시장/모델 선택, 우측 품질 요약과 브랜드 막대.",
            "- Radius: 8px 이하.",
            "- Palette: blue/green/amber/red/violet 다중 상태색, 단일 hue 지배 금지.",
            "- Data: `AUTO_TOPIC_DATA` JSON에 포함된 실측 산출물만 렌더링.",
            "",
        ]
    )


def _all_sampled_rows(axis_samples: dict[str, list[KeywordRow]], brand_samples: dict[str, list[KeywordRow]], brand_axis_samples: dict[str, list[KeywordRow]] | None = None) -> list[KeywordRow]:
    """Flatten sampled rows for exact raw-text leakage scanning."""
    rows: list[KeywordRow] = []
    for sample_rows in axis_samples.values():
        rows.extend(sample_rows)
    for sample_rows in brand_samples.values():
        rows.extend(sample_rows)
    for sample_rows in (brand_axis_samples or {}).values():
        rows.extend(sample_rows)
    return rows


def _typed_samples(value: JsonValue) -> dict[str, list[KeywordRow]]:
    """Cast a JSON-shaped sample map back to the typed row map used internally."""
    return value if isinstance(value, dict) else {}


def _audit_run_dir(audit_dir: Path, tag: str) -> Path:
    """Resolve an audit directory without nesting an explicitly tagged path twice."""
    return audit_dir if audit_dir.name == tag or audit_dir.name.startswith("task-") else audit_dir / tag


def _open_questions(alias_source: dict[str, JsonValue], executed: bool) -> list[str]:
    """List decisions that remain with PL or GenOS after this PoC."""
    questions = [
        "GenOS 실제 과금 구조가 Vertex 공개단가와 동일한지 PL/GenOS 확인 필요.",
        "운영에서 serving-direct bearer token 취득·회전 방식을 PL/GenOS 확인 필요.",
        "축 유사도 임계값 0.8의 운영 임계는 2~3회 배치 누적 후 재보정 필요.",
    ]
    if alias_source.get("status") == "fallback_found":
        questions.append("alias 산출물이 docs/design 경로에 있어 요청 경로(docs/research)와 다른 점을 PL 확인 필요.")
    if not executed:
        questions.append("dry-run 산출물은 품질 등급 실측이 아니므로 GenOS execute 결과로 대체 필요.")
    return questions


def _dict(value: JsonValue) -> dict[str, JsonValue]:
    """Return a JSON object or an empty object."""
    return value if isinstance(value, dict) else {}


def _save_run_to_db(audit_dir: Path, stage_schema: str, artifact_sha256: str) -> dict[str, JsonValue]:
    """Upsert measured topic results into isolated API tables after execute runs."""
    connection = connect_mariadb(read_env_file())
    try:
        summary = save_artifacts(
            connection,
            schema=stage_schema,
            artifacts=load_artifacts(audit_dir),
            artifact_sha256=artifact_sha256,
        )
        ensure_store_summary_nonzero(summary)
    finally:
        connection.close()
    return store_summary_json(summary)


def _list(value: JsonValue) -> list[JsonValue]:
    """Return a JSON array or an empty array."""
    return value if isinstance(value, list) else []


def _is_tier_result_key(key: str) -> bool:
    """Return true for Pro/Lite recheck result keys."""
    return key.endswith(":pro") or key.endswith(":lite")


def _timestamp_tag() -> str:
    """Return a sortable local timestamp tag."""
    return time.strftime("%Y%m%d_%H%M%S")


if __name__ == "__main__":
    typer.run(main)
