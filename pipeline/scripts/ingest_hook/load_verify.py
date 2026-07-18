"""M-2 gate: prove the uploaded epoch actually landed in the load output.

The silent-failure hole (S0_j5_wiring.txt S-4): G3 validates the uploaded file
in the S3 workdir, but the real load reads its own default source. A load can
therefore exit 0 having ingested nothing from the upload, and the Job is
recorded complete. This gate closes it — after the load, the epoch the manifest
claims must be present in the loader's own output with a positive row count, or
the Job fails.

For UBIST the loader writes ``<target>/_manifest.json`` with
``partitions[].{period_yyyymm, row_count}`` and parquet at
``<target>/year=YYYY/month=MM/data.parquet``. Both "just added" (first load)
and "already present" (idempotent re-submission) satisfy the gate; only an
absent partition or a zero/negative row count fails it.
"""
from __future__ import annotations

import json
from pathlib import Path


class LoadVerifyError(RuntimeError):
    """The uploaded epoch is not present in the load output (silent-failure guard)."""


def _verify_ubist_parquet_manifest(target_dir: Path, epoch: str) -> int:
    manifest_path = target_dir / "_manifest.json"
    if not manifest_path.is_file():
        raise LoadVerifyError(
            f"load produced no manifest at {manifest_path} — the upload never reached the loader"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise LoadVerifyError(f"load manifest unreadable ({manifest_path}): {exc}") from exc

    partitions = manifest.get("partitions", []) if isinstance(manifest, dict) else []
    for entry in partitions:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("period_yyyymm")) == epoch:
            rows = entry.get("row_count")
            if not isinstance(rows, int) or rows <= 0:
                raise LoadVerifyError(
                    f"epoch {epoch} partition present but row_count={rows!r} (<= 0): nothing loaded"
                )
            partition_file = target_dir / str(entry.get("path", ""))
            if entry.get("path") and not partition_file.is_file():
                raise LoadVerifyError(
                    f"epoch {epoch} manifest claims {entry.get('path')} but the parquet is missing"
                )
            return rows

    present = sorted(
        str(e.get("period_yyyymm")) for e in partitions if isinstance(e, dict) and e.get("period_yyyymm")
    )
    raise LoadVerifyError(
        f"epoch {epoch} absent from load output (present periods: {present or 'none'}); "
        "the uploaded submission was not loaded"
    )


_VERIFIERS = {
    "ubist_parquet_manifest": _verify_ubist_parquet_manifest,
}


def verify_epoch_loaded(kind: str, target_dir: Path, epoch: str) -> int:
    """Return the loaded row_count for ``epoch``; raise LoadVerifyError otherwise."""
    verifier = _VERIFIERS.get(kind)
    if verifier is None:
        raise LoadVerifyError(f"unknown load-verify kind {kind!r} (fail-closed)")
    return verifier(target_dir, epoch)
