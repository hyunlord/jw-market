"""Shared helpers for ingest_hook tests (fake submissions + fake k8s transport)."""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

EPOCH = "2026-07"
CATEGORY = "ubist"

# (period, level, brand, value) — each period reconciles Σparts == whole.
GOOD_ROWS = [
    ("2026-06", "Class", "리바로", 5.0),
    ("2026-06", "Class", "리바로젯", 5.0),
    ("2026-06", "전체", "-", 10.0),
    ("2026-07", "Class", "리바로", 10.0),
    ("2026-07", "Class", "리바로젯", 20.0),
    ("2026-07", "전체", "-", 30.0),
]


def write_submission(
    root: Path,
    *,
    category: str = CATEGORY,
    epoch: str = EPOCH,
    rows: list[tuple] | None = None,
    declared_rows: int | None = None,
    sha_override: str | None = None,
    complete: bool = True,
    header: tuple[str, ...] = ("period", "level", "brand", "value"),
    uploaded_by: str | None = None,
    period_start: str = "2026-06",
) -> Path:
    """Write a fake submission set + manifest; return the manifest path."""
    rows = GOOD_ROWS if rows is None else rows
    data_dir = root / category / epoch
    data_dir.mkdir(parents=True, exist_ok=True)
    data_path = data_dir / "data.csv"
    with data_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)

    sha = sha_override or hashlib.sha256(data_path.read_bytes()).hexdigest()
    manifest = {
        "contract_version": "v2",
        **({"uploaded_by": uploaded_by} if uploaded_by is not None else {}),
        "epoch": epoch,
        "category": category,
        "complete": complete,
        "submitted_at": "2026-07-17T09:00:00+09:00",
        "files": [
            {
                "path": data_path.relative_to(root).as_posix(),
                "sha256": sha,
                "rows": len(rows) if declared_rows is None else declared_rows,
                "period_start": period_start,
                "period_end": epoch,
            }
        ],
    }
    manifest_path = data_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


class FakeTransport:
    """Records rendered Job bodies instead of calling a k8s API server."""

    def __init__(self):
        self.submitted: list[tuple[str, dict]] = []

    def __call__(self, url_path: str, body: dict) -> dict:
        self.submitted.append((url_path, body))
        return {"status": "created", "metadata": body.get("metadata", {})}
