"""Loss sweep — daily watchdog, NOT a load batch (normal day = no-op).

Scans the submission root for complete manifests, compares them to the
ledger, and re-kicks anything unrecorded or failed-stale. A submission whose
webhook was lost is therefore picked up at most one sweep later. Runs from the
suspended CronJob deploy/k8s/ingest-hook/ingest-sweep-cronjob.yaml.

Rehearsal mode (--rehearsal-root) executes the runner inline instead of
creating Jobs, so the watchdog path itself is testable with zero cluster or
production contact (gate G-4).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pipeline.scripts.ingest_hook import config, job_runner
from pipeline.scripts.ingest_hook.app import IngestService
from pipeline.scripts.ingest_hook.contract import ContractError, load_manifest, parse_manifest_bytes
from pipeline.scripts.ingest_hook.ledger import STATUS_COMPLETE, STATUS_RUNNING, Ledger

MANIFEST_GLOB = "**/manifest*.json"
S3_MANIFEST_PREFIX = "_manifests/"


def _iter_manifests(input_root: Path | None, s3):
    if s3 is not None:
        for key in sorted(s3.list_keys(S3_MANIFEST_PREFIX)):
            if key.endswith(".json"):
                yield key, lambda key=key: parse_manifest_bytes(s3.read(key), manifest_path=key)
        return
    for path in sorted(input_root.glob(MANIFEST_GLOB)):
        yield path, lambda path=path: load_manifest(path)


def sweep(
    ledger: Ledger,
    input_root: Path | None,
    *,
    transport=None,
    rehearsal_root: Path | None = None,
    s3=None,
) -> dict:
    """Return {found, kicked, skipped} counts plus per-manifest actions."""
    actions: list[dict] = []
    kicked = 0
    service = IngestService(ledger, input_root, transport=transport, s3=s3)

    for manifest_path, loader in _iter_manifests(input_root, s3):
        try:
            manifest = loader()
        except ContractError as exc:
            actions.append({"path": str(manifest_path), "action": "invalid", "reason": str(exc)})
            continue
        if not manifest.complete:
            actions.append({"path": str(manifest_path), "action": "skip", "reason": "not complete"})
            continue

        entry = ledger.status(manifest.epoch, manifest.category, manifest.manifest_sha)
        if entry is not None and entry.status in (STATUS_COMPLETE, STATUS_RUNNING):
            actions.append({"path": str(manifest_path), "action": "skip", "reason": entry.status})
            continue

        # Unrecorded (lost webhook) or failed: same idempotent path as the webhook.
        ledger.receive(
            manifest.epoch,
            manifest.category,
            manifest.manifest_sha,
            manifest_path=str(manifest_path),
            uploaded_by=manifest.uploaded_by,
        )
        if rehearsal_root is not None and s3 is None:
            rc = job_runner.run(
                manifest_path, input_root=input_root, ledger=ledger, rehearsal_root=rehearsal_root
            )
            actions.append({"path": str(manifest_path), "action": "ran-inline", "rc": rc})
        else:
            name = service.promote(manifest.category)
            actions.append({"path": str(manifest_path), "action": "kicked", "job_name": name})
        kicked += 1

    return {"found": len(actions), "kicked": kicked, "actions": actions}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.scripts.ingest_hook.sweep")
    parser.add_argument("--input-root", type=Path, default=None)
    parser.add_argument("--rehearsal-root", type=Path, default=None)
    args = parser.parse_args(argv)

    s3 = config.open_input_source()
    input_root = None if s3 is not None else (args.input_root or config.input_root())
    ledger = config.open_configured_ledger()
    result = sweep(ledger, input_root, rehearsal_root=args.rehearsal_root, s3=s3)
    print(f"sweep found={result['found']} kicked={result['kicked']}")
    for action in result["actions"]:
        print(f"  {action}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
