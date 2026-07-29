from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from .alert_repository import load_recent_parse_counts, record_alert_status
from .alerts import AlertEvent, evaluate_failed_ratio, publish_alert
from .backfill import BackfillManifest
from .contract import (
    HiraRunMetrics,
    HiraWorkflowInput,
    stage_receipt_name,
    validate_run_metrics,
)
from .discovery import (
    DiscoveryReduceError,
    PageReceipt,
    build_page_receipt,
    load_page_receipts,
    read_page_receipt,
    reduce_page_receipts,
    write_page_receipt,
)
from .http_client import (
    LIST_SLOW_RESPONSE_SECONDS,
    CircuitOpenError,
    HiraHttpClient,
    HiraRequestPolicy,
)
from .models import FieldParseStatus, ParsedNotice, ParseStatus
from .pagination import fetch_page
from .receipts import read_json, run_dir, write_json, write_stage_receipt
from .repository import (
    PersistableNotice,
    connect_from_env,
    has_crawl_state,
    load_jw_brand_scope,
    load_notice_state,
    persist_batch,
)
from .service import collect_details, notice_to_json, plan_discovered_items


def _input(path: Path) -> HiraWorkflowInput:
    payload = json.loads(path.read_text(encoding="utf-8"))
    policy = HiraRequestPolicy(**payload.pop("request_policy", {}))
    return HiraWorkflowInput(**payload, request_policy=policy)


def monitored_user_agent(value: str | None) -> str:
    """Require the F18 monitored identity before any live HIRA request."""

    if not value:
        raise RuntimeError("HIRA_USER_AGENT is required for live HIRA requests")
    if "<monitored-contact>" in value or "monitored-contact-required" in value:
        raise RuntimeError("HIRA_USER_AGENT must include an actual monitored contact")
    return value


def _client(
    config: HiraWorkflowInput,
    *,
    slow_response_seconds: float | None = None,
) -> HiraHttpClient:
    policy = config.request_policy
    if slow_response_seconds is not None:
        policy = replace(policy, slow_response_seconds=slow_response_seconds)
    return HiraHttpClient(
        policy=policy,
        user_agent=monitored_user_agent(os.environ.get("HIRA_USER_AGENT")),
    )


def build_failure_receipt(stage: str, error: Exception) -> dict[str, object]:
    """Describe a circuit stop so Temporal cannot immediately retry it."""

    if isinstance(error, CircuitOpenError):
        return {
            "stage": stage,
            "status": "failed",
            "gate_failures": ["circuit_open"],
            "error_type": type(error).__name__,
            "error": str(error),
            "retry_after_seconds": error.retry_after_seconds,
        }
    if isinstance(error, DiscoveryReduceError):
        # An incomplete or inconsistent page set is structural, not transient:
        # retrying the reducer cannot conjure the missing page back.
        return {
            "stage": stage,
            "status": "failed",
            "gate_failures": ["discovery_incomplete"],
            "error_type": type(error).__name__,
            "error": str(error),
        }
    return {
        "stage": stage,
        "status": "failed",
        "gate_failures": ["stage_exception"],
        "error_type": type(error).__name__,
        "error": str(error),
    }


def _item_payload(item: object) -> dict[str, object]:
    payload = asdict(item)
    payload["notice_date"] = item.notice_date.isoformat()
    return payload


def _emit(payload: dict[str, object]) -> None:
    """Structured child log line: heartbeat telemetry and durable evidence."""

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _page_receipt(
    config: HiraWorkflowInput,
    root: Path,
    *,
    page: int,
    client: HiraHttpClient,
    pages_done: int,
    pages_total: int | None,
) -> tuple[PageReceipt, bool]:
    """Return a page receipt, re-using a durable one instead of re-fetching.

    Idempotence is what makes "retry only the failed batch" safe and keeps a
    retry from re-hitting HIRA for pages that already landed.
    """

    cached = read_page_receipt(root, page)
    started = time.monotonic()
    if cached is not None:
        _emit(
            {
                "event": "hira_page_cached",
                "page": page,
                "pages_done": pages_done + 1,
                "pages_total": pages_total,
                "items": cached.row_count,
                "page_elapsed_seconds": 0.0,
            }
        )
        return cached, True
    fetched = fetch_page(
        page,
        index_url=config.index_url,
        base_url=config.base_url,
        fetch_form=client.post_form_text,
    )
    receipt = build_page_receipt(
        page=page,
        total_count=fetched.total_count,
        items=fetched.items,
    )
    write_page_receipt(root, receipt)
    _emit(
        {
            "event": "hira_page_fetched",
            "page": page,
            "pages_done": pages_done + 1,
            "pages_total": pages_total if pages_total is not None else fetched.total_pages,
            "items": receipt.row_count,
            "total_count": fetched.total_count,
            "page_sha256": receipt.page_sha256,
            "page_elapsed_seconds": round(time.monotonic() - started, 3),
        }
    )
    return receipt, False


def _run_discover_probe(config: HiraWorkflowInput, root: Path) -> dict[str, object]:
    """Fetch page 1 only, so the workflow learns how many batches to schedule."""

    if config.manifest_path is not None:
        raise RuntimeError("discover_probe does not apply to a backfill chunk")
    client = _client(config, slow_response_seconds=LIST_SLOW_RESPONSE_SECONDS)
    receipt, cached = _page_receipt(
        config,
        root,
        page=1,
        client=client,
        pages_done=0,
        pages_total=None,
    )
    from .pagination import total_pages_for

    total_pages = total_pages_for(receipt.total_count)
    metrics = HiraRunMetrics(0, 0, 0, 0, 0, 0, 0)
    return write_stage_receipt(
        root / "discover_probe.receipt.json",
        stage="discover_probe",
        metrics=metrics,
        gate=validate_run_metrics(metrics),
        detail={
            "total_count": receipt.total_count,
            "total_pages": total_pages,
            "pages_per_batch": config.pages_per_batch,
            "page_cached": cached,
        },
    )


def _run_discover_page_batch(
    config: HiraWorkflowInput,
    root: Path,
    *,
    page_start: int,
    page_end: int,
) -> dict[str, object]:
    """Fetch one contiguous slice of list pages within a fixed activity budget."""

    if config.manifest_path is not None:
        raise RuntimeError("discover_page_batch does not apply to a backfill chunk")
    if page_start < 1 or page_end < page_start:
        raise RuntimeError(
            f"invalid page batch range: {page_start}..{page_end}"
        )
    span = page_end - page_start + 1
    if span > config.pages_per_batch:
        raise RuntimeError(
            f"page batch {page_start}..{page_end} exceeds the budgeted "
            f"{config.pages_per_batch} pages"
        )
    client = _client(config, slow_response_seconds=LIST_SLOW_RESPONSE_SECONDS)
    fetched_pages = 0
    cached_pages = 0
    total_count: int | None = None
    for offset, page in enumerate(range(page_start, page_end + 1)):
        receipt, cached = _page_receipt(
            config,
            root,
            page=page,
            client=client,
            pages_done=offset,
            pages_total=span,
        )
        total_count = receipt.total_count
        if cached:
            cached_pages += 1
        else:
            fetched_pages += 1
    receipt_name = stage_receipt_name(
        "discover_page_batch",
        page_start=page_start,
        page_end=page_end,
    )
    metrics = HiraRunMetrics(0, 0, 0, 0, 0, 0, 0)
    return write_stage_receipt(
        root / f"{receipt_name}.receipt.json",
        stage="discover_page_batch",
        metrics=metrics,
        gate=validate_run_metrics(metrics),
        detail={
            "page_start": page_start,
            "page_end": page_end,
            "pages_fetched": fetched_pages,
            "pages_cached": cached_pages,
            "total_count": total_count,
        },
    )


def _run_discover_reduce(config: HiraWorkflowInput, root: Path) -> dict[str, object]:
    conn = connect_from_env()
    try:
        brands, revision = load_jw_brand_scope(conn)
        state = load_notice_state(conn) if has_crawl_state(conn) else None
    finally:
        conn.rollback()
        conn.close()
    if config.manifest_path is not None:
        manifest = BackfillManifest.from_json(
            Path(config.manifest_path).read_text(encoding="utf-8")
        )
        if manifest.manifest_sha256 != config.manifest_sha256:
            raise RuntimeError("backfill manifest hash mismatch")
        if config.chunk_index is None:
            raise RuntimeError("backfill chunk index is missing")
        if config.chunk_index >= manifest.chunk_count:
            raise RuntimeError("backfill chunk index is out of range")
        chunk = manifest.chunks[config.chunk_index]
        rows = chunk.items
        signature = manifest.manifest_sha256
        manifest_detail = {
            "manifest_sha256": manifest.manifest_sha256,
            "manifest_total_count": manifest.total_count,
            "chunk_index": chunk.index,
            "chunk_count": manifest.chunk_count,
        }
    else:
        # Fail-closed: the reducer refuses to compare a partial page set, so a
        # dropped page can never be laundered into "unchanged".
        index = reduce_page_receipts(load_page_receipts(root))
        rows = index.items
        signature = index.manifest_sha256
        manifest_detail = {
            "manifest_sha256": index.manifest_sha256,
            "manifest_total_count": index.total_count,
            "page_count": index.total_pages,
            "page_receipts_verified": index.page_count,
        }
    plan = plan_discovered_items(rows, config=config, stored=state)
    write_json(
        root / "discovery.json",
        {
            "to_fetch": [_item_payload(item) for item in plan.to_fetch],
            "new": len(plan.new),
            "changed": len(plan.changed),
            "unchanged": len(plan.unchanged),
            "skipped_initial_backfill": plan.skipped_initial_backfill,
            "index_tag_signature_sha256": signature,
            "brand_names": list(brands),
            "mapping_revision": revision,
            **manifest_detail,
        },
    )
    metrics = HiraRunMetrics(0, 0, 0, 0, 0, 0, 0)
    return write_stage_receipt(
        root / "discover_reduce.receipt.json",
        stage="discover_reduce",
        metrics=metrics,
        gate=validate_run_metrics(metrics),
        detail={
            "planned_count": len(plan.to_fetch),
            "new_count": len(plan.new),
            "changed_count": len(plan.changed),
            "unchanged_count": len(plan.unchanged),
        },
    )


def _notice_item(payload: dict[str, object]) -> object:
    from .models import NoticeListItem

    return NoticeListItem(
        source_notice_id=str(payload["source_notice_id"]),
        title=str(payload["title"]),
        notice_date=date.fromisoformat(str(payload["notice_date"])),
        source_url=str(payload["source_url"]),
        listing_fingerprint=str(payload["listing_fingerprint"]),
    )


def _run_collect(config: HiraWorkflowInput, root: Path) -> dict[str, object]:
    discovery = read_json(root / "discovery.json")
    client = _client(config)
    notices, metrics = collect_details(
        tuple(_notice_item(item) for item in discovery["to_fetch"]),
        fetch_text=client.get_text,
        brand_names=tuple(discovery["brand_names"]),
    )
    write_json(root / "collected.json", [notice_to_json(item) for item in notices])
    gate = validate_run_metrics(metrics, failed_alert_ratio=config.failed_alert_ratio)
    return write_stage_receipt(
        root / "collect_details.receipt.json",
        stage="collect_details",
        metrics=metrics,
        gate=gate,
        detail={"collected_count": len(notices)},
    )


def _persistable(payload: dict[str, object]) -> PersistableNotice:
    parsed = payload["parsed"]
    assert isinstance(parsed, dict)
    failed_fields = tuple(str(value) for value in parsed["failed_fields"])

    def field_status(
        status_name: str,
        value_name: str,
    ) -> FieldParseStatus:
        explicit = parsed.get(status_name)
        if explicit is not None:
            return FieldParseStatus(str(explicit))
        if parsed.get(value_name) is not None:
            return FieldParseStatus.EXTRACTED
        if value_name in failed_fields:
            return FieldParseStatus.FAILED
        return FieldParseStatus.NOT_APPLICABLE

    return PersistableNotice(
        parsed=ParsedNotice(
            source_notice_id=str(parsed["source_notice_id"]),
            source_url=str(parsed["source_url"]),
            title=parsed.get("title"),
            notice_no=parsed.get("notice_no"),
            notice_date=(
                date.fromisoformat(str(parsed["notice_date"]))
                if parsed.get("notice_date")
                else None
            ),
            target_condition=parsed.get("target_condition"),
            exclusion_rule=parsed.get("exclusion_rule"),
            dosage_limit=parsed.get("dosage_limit"),
            raw_text=str(parsed["raw_text"]),
            raw_html_sha256=str(parsed["raw_html_sha256"]),
            parse_status=ParseStatus(str(parsed["parse_status"])),
            failed_fields=failed_fields,
            target_status=field_status("target_status", "target_condition"),
            exclusion_status=field_status("exclusion_status", "exclusion_rule"),
            dosage_status=field_status("dosage_status", "dosage_limit"),
        ),
        listing_fingerprint=str(payload["listing_fingerprint"]),
        brand_names=tuple(str(value) for value in payload["brand_names"]),
    )


def collect_metrics_from_receipt(receipt: dict[str, object]) -> HiraRunMetrics:
    """Parse and fail-close the collect receipt before any persistence."""

    if receipt.get("status") != "complete":
        raise RuntimeError("collect receipt is not complete")
    metrics = HiraRunMetrics(
        exit_code=int(receipt["exit_code"]),
        failures=int(receipt["failures"]),
        identity_gap=int(receipt["identity_gap"]),
        pending_gap=int(receipt["pending_gap"]),
        parsed_count=int(receipt["parsed_count"]),
        partial_count=int(receipt["partial_count"]),
        failed_count=int(receipt["failed_count"]),
    )
    gate = validate_run_metrics(metrics)
    if not gate.passed:
        raise RuntimeError(
            "collect receipt failed gate: " + ",".join(gate.failures)
        )
    return metrics


def _run_persist(config: HiraWorkflowInput, root: Path) -> dict[str, object]:
    discovery = read_json(root / "discovery.json")
    notices = tuple(_persistable(item) for item in read_json(root / "collected.json"))
    collect_receipt = read_json(root / "collect_details.receipt.json")
    run_metrics = collect_metrics_from_receipt(collect_receipt)
    conn = connect_from_env()
    try:
        persist_batch(
            conn,
            notices=notices,
            run_id=config.run_id,
            index_tag_signature_sha256=discovery["index_tag_signature_sha256"],
            mapping_revision=discovery["mapping_revision"],
            collected_at=datetime.now(UTC),
            run_metrics=run_metrics,
        )
    finally:
        conn.close()
    return write_stage_receipt(
        root / "persist_results.receipt.json",
        stage="persist_results",
        metrics=run_metrics,
        gate=validate_run_metrics(run_metrics),
        detail={"persisted_count": len(notices)},
    )


def _run_verify(config: HiraWorkflowInput, root: Path) -> dict[str, object]:
    expected = {
        str(item["source_notice_id"]): str(item["listing_fingerprint"])
        for item in read_json(root / "discovery.json")["to_fetch"]
    }
    conn = connect_from_env()
    try:
        actual = load_notice_state(conn)
        recent_counts = (
            load_recent_parse_counts(
                conn,
                window_runs=config.failed_alert_window_runs,
            )
            if config.failed_alert_ratio is not None
            else ()
        )
    finally:
        conn.rollback()
        conn.close()
    missing = sorted(set(expected) - actual.keys())
    mismatched = sorted(
        notice_id
        for notice_id, fingerprint in expected.items()
        if notice_id in actual
        and actual[notice_id].listing_fingerprint != fingerprint
    )
    metrics = HiraRunMetrics(
        exit_code=0,
        failures=0,
        identity_gap=len(mismatched),
        pending_gap=len(missing),
        parsed_count=len(expected),
        partial_count=0,
        failed_count=0,
    )
    gate = validate_run_metrics(metrics)
    alert_detail: dict[str, object] = {}
    if config.failed_alert_ratio is not None:
        evaluation = evaluate_failed_ratio(
            recent_counts,
            threshold=config.failed_alert_ratio,
        )
        alert_detail["failed_ratio_window"] = asdict(evaluation)
        if evaluation.triggered:
            delivery = publish_alert(
                AlertEvent(
                    event="hira_parse_failed_ratio",
                    run_id=config.run_id,
                    failed_count=evaluation.failed_count,
                    parsed_count=evaluation.parsed_count,
                    failed_ratio=evaluation.ratio,
                    threshold=evaluation.threshold,
                    window_runs=evaluation.runs,
                ),
                endpoint=os.environ.get("HIRA_ALERT_WEBHOOK_URL") or None,
            )
            alert_detail["alert_delivery"] = asdict(delivery)
            print(
                "ALERT event=hira_parse_failed_ratio "
                f"run_id={config.run_id} ratio={evaluation.ratio:.4f} "
                f"threshold={evaluation.threshold:.4f} delivery={delivery.status}"
            )
            alert_conn = connect_from_env()
            try:
                record_alert_status(
                    alert_conn,
                    run_id=config.run_id,
                    alert_status=delivery.status,
                )
            finally:
                alert_conn.close()
    return write_stage_receipt(
        root / "verify_run.receipt.json",
        stage="verify_run",
        metrics=metrics,
        gate=gate,
        detail={
            "missing_ids": missing,
            "hash_mismatch_ids": mismatched,
            **alert_detail,
        },
    )


_RUNNERS = {
    "discover_probe": _run_discover_probe,
    "discover_page_batch": _run_discover_page_batch,
    "discover_reduce": _run_discover_reduce,
    "collect_details": _run_collect,
    "persist_results": _run_persist,
    "verify_run": _run_verify,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=tuple(_RUNNERS), required=True)
    parser.add_argument("--config-json", type=Path, required=True)
    parser.add_argument("--page-start", type=int)
    parser.add_argument("--page-end", type=int)
    args = parser.parse_args(argv)
    config = _input(args.config_json)
    root = run_dir(config.state_root, config.run_id)
    receipt_name = stage_receipt_name(
        args.stage,
        page_start=args.page_start,
        page_end=args.page_end,
    )
    try:
        if args.stage == "discover_page_batch":
            if args.page_start is None or args.page_end is None:
                raise RuntimeError("discover_page_batch requires --page-start/--page-end")
            receipt = _run_discover_page_batch(
                config,
                root,
                page_start=args.page_start,
                page_end=args.page_end,
            )
        else:
            receipt = _RUNNERS[args.stage](config, root)
    except Exception as exc:  # noqa: BLE001 - CLI boundary converts failures to nonzero rc.
        if isinstance(exc, CircuitOpenError | DiscoveryReduceError):
            write_json(
                root / f"{receipt_name}.receipt.json",
                build_failure_receipt(args.stage, exc),
            )
        print(f"stage={args.stage} status=failed error={type(exc).__name__}: {exc}")
        return 1
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
