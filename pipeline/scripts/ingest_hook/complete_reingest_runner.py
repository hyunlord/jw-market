"""Mart-only complete reingest runner for already-complete raw submissions."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pipeline.scripts.deploy.mart_load_ops import (
    publish_table_group_atomically,
    restore_table_group_atomically,
)
from pipeline.scripts.ingest_hook import config
from pipeline.scripts.ingest_hook import csd_channel_activation
from pipeline.scripts.ingest_hook import csd_keyword_activation
from pipeline.scripts.ingest_hook import iqvia_nsa_mart_activation as iqvia_activation
from pipeline.scripts.ingest_hook import ubist_mart_activation
from pipeline.scripts.ingest_hook.category_map import CategorySpec, resolve_category
from pipeline.scripts.ingest_hook.completion_signal import PublishResult
from pipeline.scripts.ingest_hook.contract import load_manifest, parse_manifest_bytes


MODE = "mart_from_existing_raw"
REQUEST_SOURCE = "complete_reingest_request"
STAGE_SEQUENCES = {
    "ubist": (
        "g3",
        "load",
        "load_verify",
        "mart_build",
        "sigma",
        "post_gate",
        "mart_publish",
        "refresh",
        "signal",
    ),
    "iqvia_nsa": (
        "g3",
        "load",
        "load_verify",
        "mart_build",
        "sigma",
        "post_gate",
        "mart_publish",
        "refresh",
        "signal",
    ),
    "iqvia_csd_channel": (
        "g3",
        "load",
        "load_verify",
        "mart_publish",
        "context_bridge",
        "dashboard",
        "signal",
    ),
    "iqvia_csd_keyword": (
        "g3",
        "load",
        "load_verify",
        "post_gate",
        "mart_publish",
        "topic_extraction",
        "dashboard",
        "signal",
    ),
}
ACTOR = "complete_reingest_runner"
_SECRET_RE = re.compile(
    r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)"
    r"(\s*[:=]\s*)([^,\s;]+)"
)


class CompleteReingestRejected(RuntimeError):
    """A complete reingest attempt failed a prerequisite gate."""


@dataclass(frozen=True, slots=True)
class RequestContext:
    identity: tuple[str, str, str]
    run_id: str
    category: str
    request_id: str
    parent_run_id: str
    affected_scope: dict[str, object]
    scope_values: tuple[str, ...] | None
    period_scope: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class Publication:
    target_db: str
    actions: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class PreparedMart:
    target_db: str
    build_db: str
    tables: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TerminalOutcome:
    request_id: str
    run_id: str
    category: str
    status: str
    reason: str


def run(
    manifest_path: Path,
    *,
    request_id: str,
    run_id: str,
    ledger=None,
    expected_affected_scope: dict[str, object] | None = None,
    input_source=None,
) -> TerminalOutcome:
    manifest, _local_manifest = _load_manifest_only(manifest_path, input_source=input_source)
    active_ledger = ledger or config.open_configured_ledger()
    context = _validate_request(
        active_ledger,
        identity=(manifest.epoch, manifest.category, manifest.manifest_sha),
        request_id=request_id,
        run_id=run_id,
    )
    if expected_affected_scope is not None and context.affected_scope != expected_affected_scope:
        raise CompleteReingestRejected("CLI affected_scope differs from persisted request")

    try:
        spec = resolve_category(context.category)
        _record_existing_raw_prelude(active_ledger, context)
        match context.category:
            case "ubist":
                prepared = _recompute_ubist_mart(context, active_ledger)
            case "iqvia_nsa":
                prepared = _recompute_iqvia_mart(context, active_ledger)
            case "iqvia_csd_channel":
                _recompute_publish_csd_channel(context, active_ledger)
                _record_external_stage_chain(
                    active_ledger, context, ("context_bridge", "dashboard")
                )
                return _complete_terminal(
                    active_ledger,
                    context,
                    "recomputed CSD channel stage candidate and atomically published",
                )
            case "iqvia_csd_keyword":
                _recompute_publish_csd_keyword(context, active_ledger)
                _record_external_stage_chain(
                    active_ledger, context, ("topic_extraction", "dashboard")
                )
                return _complete_terminal(
                    active_ledger,
                    context,
                    "recomputed CSD keyword stage candidate and atomically published",
                )
            case unsupported:
                raise CompleteReingestRejected(
                    f"complete reingest mart-only mode is unsupported for {unsupported!r}"
                )

        _record_stage(active_ledger, context, "sigma", "running")
        _record_stage(active_ledger, context, "post_gate", "running")
        try:
            _run_numeric_gates(context, prepared, spec)
        except Exception as exc:
            reason = _reason(exc)
            _record_stage(active_ledger, context, "sigma", "failed", reason)
            _record_stage(active_ledger, context, "post_gate", "failed", reason)
            raise
        _record_stage(
            active_ledger,
            context,
            "sigma",
            reason=f"market sigma passed for {spec.sigma_source}",
        )
        _record_stage(
            active_ledger,
            context,
            "post_gate",
            reason="numeric post-gates passed for recomputed mart",
        )
        _publish_and_refresh_numeric(context, active_ledger, prepared, spec)
        return _complete_terminal(
            active_ledger,
            context,
            "recomputed mart from existing raw and atomically published",
        )
    except Exception as exc:
        try:
            _failed_terminal(active_ledger, context, _reason(exc))
        except Exception as terminal_exc:
            raise RuntimeError(
                f"{_reason(exc)}; failed terminal record also failed: "
                f"{_reason(terminal_exc)}"
            ) from exc
        raise


def _record_stage(
    ledger,
    context: RequestContext,
    stage: str,
    status: str = "complete",
    reason: str | None = None,
) -> None:
    stamp = _stamp()
    seq = _stage_seq(context, stage)
    _emit_stage_marker(stage, status, reason)
    ledger.record_stage(
        *context.identity, run_id=context.run_id, seq=seq, stage=stage,
        status=status, reason=reason, started_at=stamp, finished_at=stamp, duration_ms=0,
    )


def _stage_seq(context: RequestContext, stage: str) -> int:
    try:
        return STAGE_SEQUENCES[context.category].index(stage) + 1
    except (KeyError, ValueError) as exc:
        raise CompleteReingestRejected(
            f"stage {stage!r} is not valid for complete reingest category {context.category!r}"
        ) from exc


def _emit_stage_marker(stage: str, status: str, reason: str | None) -> None:
    if status == "running":
        print(f"[stage] {stage} start", flush=True)
    elif status == "complete":
        print(f"[stage] {stage} end", flush=True)
    elif status == "failed":
        suffix = f" reason={_redact_text(reason)}" if reason else ""
        print(f"[stage] {stage} end rc=1{suffix}", flush=True)


def _validate_request(
    ledger,
    *,
    identity: tuple[str, str, str],
    request_id: str,
    run_id: str,
) -> RequestContext:
    parent = ledger.status(*identity)
    if parent is None:
        raise CompleteReingestRejected("parent ingest_ledger row is required")
    lookup = getattr(ledger, "complete_reingest_request", None)
    transition = (
        lookup(*identity, request_id=request_id)
        if callable(lookup)
        else _request_from_transitions(ledger, identity, request_id)
    )
    if transition is None:
        raise CompleteReingestRejected("persisted complete_reingest_request transition is required")
    evidence = getattr(transition, "evidence", None)
    if (
        getattr(transition, "source", None) != REQUEST_SOURCE
        or not isinstance(evidence, dict)
        or evidence.get("mode") != MODE
        or evidence.get("request_id") != request_id
        or evidence.get("run_id") != run_id
    ):
        raise CompleteReingestRejected("persisted complete_reingest_request contract mismatch")
    affected_scope = evidence.get("affected_scope")
    if not isinstance(affected_scope, dict):
        raise CompleteReingestRejected("affected_scope is required")
    category = identity[1]
    scope_values, period_scope = _validate_scope(category, affected_scope)
    parent_run_id = str(getattr(parent, "run_id", "") or "")
    if not parent_run_id:
        raise CompleteReingestRejected("parent complete run_id is required")
    return RequestContext(
        identity,
        run_id,
        category,
        request_id,
        parent_run_id,
        affected_scope,
        scope_values,
        period_scope,
    )


def _request_from_transitions(ledger, identity: tuple[str, str, str], request_id: str):
    matches = [
        transition
        for transition in ledger.status_transitions(*identity)
        if getattr(transition, "event_id", None) == request_id
    ]
    return matches[0] if len(matches) == 1 else None


def _validate_scope(
    category: str, affected_scope: dict[str, object]
) -> tuple[tuple[str, ...] | None, tuple[str, ...] | None]:
    dimension = affected_scope.get("dimension")
    count = affected_scope.get("count")
    values = affected_scope.get("values")
    if not (
        isinstance(dimension, str)
        and isinstance(count, int)
        and isinstance(values, list)
    ):
        raise CompleteReingestRejected("affected_scope must contain dimension, count, and values")
    clean_values = tuple(value for value in values if isinstance(value, str) and value)
    if len(clean_values) != count or not clean_values:
        raise CompleteReingestRejected("affected_scope values must exactly match count")
    match category:
        case "ubist":
            if dimension == "source" and clean_values == ("ubist",):
                return None, None
            if dimension != "atc4":
                raise CompleteReingestRejected("UBIST complete reingest scope is invalid")
            periods = affected_scope.get("periods")
            clean_periods = (
                tuple(value for value in periods if isinstance(value, str) and value)
                if isinstance(periods, list)
                else ()
            )
            if not clean_periods:
                raise CompleteReingestRejected("UBIST affected_scope requires period scope")
            return clean_values, clean_periods
        case "iqvia_nsa":
            if dimension != "source" or clean_values != ("iqvia_nsa",):
                raise CompleteReingestRejected("IQVIA NSA complete reingest requires source scope")
            return clean_values, ()
        case "iqvia_csd_channel":
            if dimension != "source" or clean_values != ("iqvia_csd_channel",):
                raise CompleteReingestRejected("CSD channel complete reingest requires source scope")
            return clean_values, ()
        case "iqvia_csd_keyword":
            if dimension != "source" or clean_values != ("iqvia_csd_keyword",):
                raise CompleteReingestRejected("CSD keyword complete reingest requires source scope")
            return clean_values, ()
        case "skeleton" | "mi_master":
            raise CompleteReingestRejected(f"unsupported complete reingest category: {category}")
        case _:
            raise CompleteReingestRejected(f"unknown complete reingest category: {category}")


def _record_existing_raw_prelude(ledger, context: RequestContext) -> None:
    reasons = {
        "g3": "persisted complete manifest identity reused; no raw source loading",
        "load": "existing live raw reused for complete reingest attempt",
        "load_verify": "parent complete load verification retained for existing raw",
    }
    for stage, reason in reasons.items():
        _record_stage(ledger, context, stage, reason=reason)


def _recompute_ubist_mart(context: RequestContext, ledger) -> PreparedMart:
    activation = ubist_mart_activation.from_env(run_id=context.run_id)
    catalog_conn = None
    try:
        _record_stage(ledger, context, "mart_build", "running")
        target_root, _staging_verify = config.load_output_root()
        ubist_dir = target_root / "ubist"
        catalog_root = ubist_mart_activation.production_catalog_root_from_env()
        catalog_conn = config.open_mart_connection(activation.source_db)
        ubist_mart_activation.prepare_catalog_for_mart(
            catalog_root=catalog_root,
            ubist_dir=ubist_dir,
            source_db=activation.source_db,
            conn=catalog_conn,
            run_id=context.run_id,
            output_parent=target_root,
        )
        ubist_mart_activation.build_shadow(
            activation,
            catalog_root=catalog_root,
            ubist_dir=ubist_dir,
            atc4_scope=context.scope_values,
            period_scope=context.period_scope,
        )
        _record_stage(
            ledger,
            context,
            "mart_build",
            reason=f"recomputed UBIST mart from live corpus: {activation.build_db}",
        )
        return PreparedMart(
            target_db=activation.target_db,
            build_db=activation.build_db,
            tables=ubist_mart_activation.NUMERIC_TABLES,
        )
    except Exception as exc:
        _record_stage(ledger, context, "mart_build", "failed", _reason(exc))
        raise
    finally:
        if catalog_conn is not None:
            catalog_conn.close()


def _recompute_iqvia_mart(context: RequestContext, ledger) -> PreparedMart:
    activation = iqvia_activation.from_env(run_id=context.run_id)
    build_conn = None
    try:
        _record_stage(ledger, context, "mart_build", "running")
        iqvia_activation.initialize_build_schema(activation)
        copied = iqvia_activation.copy_existing_raw(activation)
        build_conn = config.open_mart_connection(activation.build_db)
        retained_quarters = iqvia_activation.trim_raw_retention(build_conn, activation)
        iqvia_activation.build_mart(activation)
        _record_stage(
            ledger,
            context,
            "mart_build",
            reason=(
                f"recomputed IQVIA NSA mart from live raw: {activation.build_db}; "
                f"raw_rows={copied}; retained_quarters={retained_quarters}"
            ),
        )
        return PreparedMart(
            target_db=activation.target_db,
            build_db=activation.build_db,
            tables=iqvia_activation.NSA_PUBLISH_TABLES,
        )
    except Exception as exc:
        _record_stage(ledger, context, "mart_build", "failed", _reason(exc))
        raise
    finally:
        if build_conn is not None:
            build_conn.close()


def _run_numeric_gates(
    context: RequestContext,
    prepared: PreparedMart,
    spec: CategorySpec,
) -> None:
    if spec.sigma_source is None:
        raise CompleteReingestRejected(
            f"numeric recomputation requires a sigma source for {context.category}"
        )

    from pipeline.scripts.ingest_hook.post_gate import (
        SigmaEvidence,
        fingerprint_untouched_sources,
        run_post_gates,
        sample_existing_periods,
    )
    from pipeline.scripts.ingest_hook.sigma_market import check_market_sigma

    conn = config.open_mart_connection(prepared.build_db)
    try:
        untouched_before = fingerprint_untouched_sources(
            conn, touched_source=spec.sigma_source
        )
        affected_periods = context.period_scope or ()
        sampled_periods = sample_existing_periods(
            conn,
            source=spec.sigma_source,
            excluded=affected_periods,
        )
        periods = tuple(sorted(set(affected_periods + sampled_periods)))
        report = check_market_sigma(
            conn, source=spec.sigma_source, periods=tuple(periods)
        )
        sigma = SigmaEvidence(
            checked=report.cells_checked,
            population=report.cells_checked,
            detail=(
                f"source={report.source} markets={report.markets_checked} "
                f"periods={','.join(report.periods)}"
            ),
        )
        untouched_after = fingerprint_untouched_sources(
            conn, touched_source=spec.sigma_source
        )
        run_post_gates(
            run_id=context.run_id,
            epoch=context.identity[0],
            category=context.category,
            sigma_check=lambda: sigma,
            expected_rows=sigma.population,
            actual_rows=sigma.checked,
            untouched_before=untouched_before,
            untouched_after=untouched_after,
            report_path=Path(tempfile.mkdtemp(prefix="complete_reingest_post_gate_"))
            / "post_gate_report.json",
        )
    finally:
        conn.close()


def _publish_and_refresh_numeric(
    context: RequestContext,
    ledger,
    prepared: PreparedMart,
    spec: CategorySpec,
) -> Publication:
    writer_conn = config.open_mart_connection(prepared.target_db)
    lock_name = ubist_mart_activation.WRITER_LOCK_NAME
    lock_acquired = False
    primary_failure_reason: str | None = None
    try:
        ubist_mart_activation.acquire_writer_lock(
            writer_conn,
            timeout_seconds=0,
            lock_name=lock_name,
        )
        lock_acquired = True
        try:
            _record_stage(ledger, context, "mart_publish", "running")
            actions = _publish_table_group(
                writer_conn,
                build_db=prepared.build_db,
                target_db=prepared.target_db,
                run_id=context.run_id,
                tables=prepared.tables,
            )
        except Exception as exc:
            _record_stage(ledger, context, "mart_publish", "failed", _reason(exc))
            raise
        publication = Publication(prepared.target_db, tuple(actions))
        _record_stage(
            ledger,
            context,
            "mart_publish",
            reason=(
                "atomically published recomputed mart; "
                f"rollback_anchor={_rollback_anchor(publication)}"
            ),
        )

        try:
            _record_stage(ledger, context, "refresh", "running")
            _run_refresh_argv(
                spec.refresh_argv,
                connection=writer_conn,
                lock_name=lock_name,
            )
        except Exception as exc:
            _record_stage(ledger, context, "refresh", "failed", _reason(exc))
            try:
                _restore_publication(
                    writer_conn,
                    publication=publication,
                    run_id=context.run_id,
                )
            except Exception as restore_exc:
                raise RuntimeError(
                    f"{_reason(exc)}; atomic mart restore failed: "
                    f"{_reason(restore_exc)}"
                ) from exc
            raise
        _record_stage(
            ledger,
            context,
            "refresh",
            reason=f"executed refresh_argv: {' '.join(spec.refresh_argv)}",
        )
        return publication
    except Exception as exc:
        primary_failure_reason = _reason(exc)
        raise
    finally:
        if lock_acquired:
            try:
                ubist_mart_activation.release_writer_lock(
                    writer_conn, lock_name=lock_name
                )
            except Exception as cleanup_exc:
                if primary_failure_reason is None:
                    raise
                print(
                    "cleanup=writer_lock_release_failed "
                    f"primary_preserved={primary_failure_reason} "
                    f"cleanup_reason={_reason(cleanup_exc)}",
                    file=sys.stderr,
                )
        writer_conn.close()


def _run_refresh_argv(
    argv: tuple[str, ...],
    *,
    connection,
    lock_name: str,
) -> None:
    from pipeline.scripts.ingest_hook.job_runner import _run_commands_with_writer_lock

    _run_commands_with_writer_lock(
        "complete reingest refresh",
        argv,
        connection=connection,
        lock_name=lock_name,
    )


def _recompute_publish_csd_channel(
    context: RequestContext, ledger
) -> Publication:
    raw_schema, stage_schema = config.csd_channel_live_schemas(mode="production")
    plan = csd_channel_activation.plan_for_run(
        context.run_id,
        raw_schema=raw_schema,
        stage_schema=stage_schema,
    )
    conn = config.open_csd_channel_connection()
    lock_acquired = False
    primary_failure_reason: str | None = None
    try:
        ubist_mart_activation.acquire_writer_lock(
            conn,
            timeout_seconds=0,
            lock_name=csd_channel_activation.WRITER_LOCK_NAME,
        )
        lock_acquired = True
        _record_stage(ledger, context, "mart_publish", "running")
        evidence = csd_channel_activation.prepare_candidate(
            conn,
            plan,
            source_paths=(),
            enforce_post_gate=True,
        )
        verdict = csd_channel_activation.publish_candidate(conn, plan, evidence)
        if verdict is not csd_channel_activation.SwapVerdict.APPLIED:
            raise RuntimeError(f"CSD channel publish was not applied: {verdict}")
        _record_stage(
            ledger,
            context,
            "mart_publish",
            reason="atomically published CSD channel raw/stage candidate",
        )
        return Publication(stage_schema, ())
    except Exception as exc:
        primary_failure_reason = _reason(exc)
        _record_stage(ledger, context, "mart_publish", "failed", primary_failure_reason)
        raise
    finally:
        if lock_acquired:
            _release_lock_preserving_primary(
                conn,
                lock_name=csd_channel_activation.WRITER_LOCK_NAME,
                primary_failure_reason=primary_failure_reason,
            )
        conn.close()


def _recompute_publish_csd_keyword(
    context: RequestContext, ledger
) -> Publication:
    raw_schema, stage_schema = config.csd_keyword_live_schemas()
    plan = csd_keyword_activation.plan_for_run(
        context.run_id,
        raw_schema=raw_schema,
        stage_schema=stage_schema,
    )
    activation_conn = config.open_csd_channel_connection()
    writer_conn = None
    lock_acquired = False
    primary_failure_reason: str | None = None
    try:
        ubist_mart_activation.acquire_writer_lock(
            activation_conn,
            timeout_seconds=0,
            lock_name=csd_keyword_activation.WRITER_LOCK_NAME,
        )
        lock_acquired = True
        writer_conn = config.open_mart_connection()
        _record_stage(ledger, context, "post_gate", "running")
        evidence = csd_keyword_activation.prepare_candidate_from_live_raw(
            writer_conn, plan
        )
        _record_stage(
            ledger,
            context,
            "post_gate",
            reason="recomputed CSD keyword candidate from live raw",
        )
        _record_stage(ledger, context, "mart_publish", "running")
        csd_keyword_activation.publish_candidate(activation_conn, plan, evidence)
        _record_stage(
            ledger,
            context,
            "mart_publish",
            reason="atomically published CSD keyword raw/stage candidate",
        )
        return Publication(stage_schema, ())
    except Exception as exc:
        primary_failure_reason = _reason(exc)
        stage = "mart_publish" if any(
            record.get("stage") == "post_gate" and record.get("status") == "complete"
            for record in getattr(ledger, "stage_records", ())
        ) else "post_gate"
        _record_stage(ledger, context, stage, "failed", primary_failure_reason)
        raise
    finally:
        if lock_acquired:
            _release_lock_preserving_primary(
                activation_conn,
                lock_name=csd_keyword_activation.WRITER_LOCK_NAME,
                primary_failure_reason=primary_failure_reason,
            )
        if writer_conn is not None:
            writer_conn.close()
        activation_conn.close()


def _rollback_anchor(publication: Publication) -> str:
    anchors = tuple(
        f"{getattr(action, 'table', '<unknown>')}:{getattr(action, 'backup_table', '<missing>')}"
        for action in publication.actions
    )
    return ",".join(anchors) or "none"


def _record_external_stage_chain(
    ledger,
    context: RequestContext,
    stages: tuple[str, ...],
) -> None:
    for stage in stages:
        _record_stage(
            ledger,
            context,
            stage,
            reason="external source-owned stage completed by normal activation helper",
        )


def _publish_table_group(
    conn,
    *,
    build_db: str,
    target_db: str,
    run_id: str,
    tables: tuple[str, ...],
):
    return publish_table_group_atomically(
        conn,
        build_db=build_db,
        target_db=target_db,
        run_id=run_id,
        tables=tables,
    )


def _restore_publication(
    connection,
    *,
    publication: Publication,
    run_id: str,
) -> None:
    restore_table_group_atomically(
        connection,
        target_db=publication.target_db,
        actions=publication.actions,
        run_id=run_id,
    )


def _release_lock_preserving_primary(
    connection,
    *,
    lock_name: str,
    primary_failure_reason: str | None,
) -> None:
    try:
        ubist_mart_activation.release_writer_lock(connection, lock_name=lock_name)
    except Exception as cleanup_exc:
        if primary_failure_reason is None:
            raise
        print(
            "cleanup=writer_lock_release_failed "
            f"primary_preserved={primary_failure_reason} "
            f"cleanup_reason={_reason(cleanup_exc)}",
            file=sys.stderr,
        )


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _reason(exc: BaseException) -> str:
    return _redact_text(f"{type(exc).__name__}: {exc}")


def _redact_text(value: object) -> str:
    text = str(value)
    return _SECRET_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}<redacted>", text)


def _complete_terminal(
    ledger, context: RequestContext, reason: str
) -> TerminalOutcome:
    outcome = TerminalOutcome(
        request_id=context.request_id,
        run_id=context.run_id,
        category=context.category,
        status="complete",
        reason=reason,
    )
    _record_stage(ledger, context, "signal", "running")
    _record_terminal(ledger, context, outcome)
    _emit_reingest_terminal_callback(context, outcome)
    _record_stage(ledger, context, "signal", reason="complete reingest terminal signal emitted")
    return outcome


def _failed_terminal(
    ledger, context: RequestContext, reason: str
) -> TerminalOutcome:
    outcome = TerminalOutcome(
        request_id=context.request_id,
        run_id=context.run_id,
        category=context.category,
        status="failed",
        reason=reason,
    )
    _record_terminal(ledger, context, outcome)
    _emit_reingest_terminal_callback(context, outcome)
    return outcome


def _record_terminal(
    ledger, context: RequestContext, outcome: TerminalOutcome
) -> None:
    recorder = getattr(ledger, "record_complete_reingest_terminal", None)
    if callable(recorder):
        recorder(
            *context.identity,
            request_id=outcome.request_id,
            run_id=outcome.run_id,
            status=outcome.status,
            reason=outcome.reason,
            actor=ACTOR,
            job_name=os.environ.get("HOSTNAME"),
            affected_scope=context.affected_scope,
        )


def _emit_reingest_terminal_callback(
    context: RequestContext, outcome: TerminalOutcome
) -> None:
    """Best-effort callback to release the durable global queue slot."""

    try:
        endpoint, attempts = config.queue_drain_webhook()
        if not endpoint:
            return
        endpoint = _reingest_terminal_endpoint(endpoint)
        epoch, category, manifest_sha = context.identity
        payload = {
            "epoch": epoch,
            "category": category,
            "manifest_sha": manifest_sha,
            "request_id": context.request_id,
            "run_id": outcome.run_id,
            "status": outcome.status,
            "reason": outcome.reason,
            "job_name": os.environ.get("HOSTNAME"),
        }
        result = _publish_reingest_terminal(
            payload,
            endpoint=endpoint,
            attempts=attempts,
        )
        if result.status == "failed":
            print(
                "reingest_terminal_callback=failed "
                f"attempts={result.attempts} reason={result.reason}",
                file=sys.stderr,
            )
    except Exception as exc:
        print(
            f"reingest_terminal_callback=failed reason={_reason(exc)}",
            file=sys.stderr,
        )


def _reingest_terminal_endpoint(queue_endpoint: str) -> str:
    parsed = urlsplit(queue_endpoint)
    if parsed.path.rstrip("/") != "/ingest/terminal":
        raise ValueError(
            "INGEST_QUEUE_DRAIN_WEBHOOK_URL must end with /ingest/terminal"
        )
    return urlunsplit(
        (parsed.scheme, parsed.netloc, "/ingest/reingest/terminal", parsed.query, parsed.fragment)
    )


def _publish_reingest_terminal(
    payload: dict[str, object],
    *,
    endpoint: str,
    attempts: int,
    opener=urllib.request.urlopen,
    sleeper=time.sleep,
) -> PublishResult:
    attempts = min(max(int(attempts), 3), 5)
    body = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    last_reason = None
    for index in range(attempts):
        request = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with opener(request, timeout=15) as response:
                status = int(getattr(response, "status", 0))
            if 200 <= status < 300:
                return PublishResult("published", index + 1)
            last_reason = f"HTTP {status}"
        except Exception as exc:  # queue startup reconciliation is the recovery path
            last_reason = _reason(exc)
        if index + 1 < attempts:
            sleeper(float(2**index))
    return PublishResult("failed", attempts, last_reason)


def _parse_scope_json(raw: str) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CompleteReingestRejected("affected_scope JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise CompleteReingestRejected("affected_scope JSON must be an object")
    return payload


def _load_manifest_only(manifest_path: Path, *, input_source=None):
    if input_source is None:
        return load_manifest(manifest_path), manifest_path
    key = str(manifest_path).lstrip("/")
    payload = input_source.read(key)
    workdir = Path(tempfile.mkdtemp(prefix="complete_reingest_manifest_"))
    local_manifest = workdir / key
    local_manifest.parent.mkdir(parents=True, exist_ok=True)
    local_manifest.write_bytes(payload)
    return parse_manifest_bytes(payload, manifest_path=key), local_manifest


def _require_cli_identity(args: argparse.Namespace, *, input_source=None) -> Path:
    manifest_path = Path(args.manifest)
    manifest, local_manifest = _load_manifest_only(manifest_path, input_source=input_source)
    actual = (manifest.epoch, manifest.category, manifest.manifest_sha)
    expected = (args.epoch, args.category, args.manifest_sha)
    if actual != expected:
        raise CompleteReingestRejected(
            f"CLI identity differs from manifest: expected={expected} actual={actual}"
        )
    return local_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--epoch", required=True)
    parser.add_argument("--category", required=True)
    parser.add_argument("--manifest-sha", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--affected-scope-json", required=True)
    args = parser.parse_args(argv)
    input_source = config.open_input_source()
    manifest_path = _require_cli_identity(args, input_source=input_source)
    run(
        manifest_path,
        request_id=args.request_id,
        run_id=args.run_id,
        expected_affected_scope=_parse_scope_json(args.affected_scope_json),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
