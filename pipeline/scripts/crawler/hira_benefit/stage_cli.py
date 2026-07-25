from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from .alert_repository import load_recent_parse_counts, record_alert_status
from .alerts import AlertEvent, evaluate_failed_ratio, publish_alert
from .backfill import BackfillManifest
from .contract import HiraRunMetrics, HiraWorkflowInput, validate_run_metrics
from .http_client import (
    LIST_SLOW_RESPONSE_SECONDS,
    CircuitOpenError,
    HiraHttpClient,
    HiraRequestPolicy,
)
from .models import ParsedNotice, ParseStatus
from .pagination import fetch_notice_index
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


def _run_discover(config: HiraWorkflowInput, root: Path) -> dict[str, object]:
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
        client = _client(
            config,
            slow_response_seconds=LIST_SLOW_RESPONSE_SECONDS,
        )
        index = fetch_notice_index(
            index_url=config.index_url,
            base_url=config.base_url,
            fetch_form=client.post_form_text,
        )
        rows = index.items
        signature = index.manifest_sha256
        manifest_detail = {
            "manifest_sha256": index.manifest_sha256,
            "manifest_total_count": index.total_count,
            "page_count": index.total_pages,
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
        root / "discover_changes.receipt.json",
        stage="discover_changes",
        metrics=metrics,
        gate=validate_run_metrics(metrics),
        detail={"planned_count": len(plan.to_fetch)},
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
            failed_fields=tuple(str(value) for value in parsed["failed_fields"]),
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
    "discover_changes": _run_discover,
    "collect_details": _run_collect,
    "persist_results": _run_persist,
    "verify_run": _run_verify,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=tuple(_RUNNERS), required=True)
    parser.add_argument("--config-json", type=Path, required=True)
    args = parser.parse_args(argv)
    config = _input(args.config_json)
    root = run_dir(config.state_root, config.run_id)
    try:
        receipt = _RUNNERS[args.stage](config, root)
    except Exception as exc:  # noqa: BLE001 - CLI boundary converts failures to nonzero rc.
        if isinstance(exc, CircuitOpenError):
            write_json(
                root / f"{args.stage}.receipt.json",
                build_failure_receipt(args.stage, exc),
            )
        print(f"stage={args.stage} status=failed error={type(exc).__name__}: {exc}")
        return 1
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
