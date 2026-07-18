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


def _kr_period(period: str) -> str:
    year, month = period.split("-")
    return f"{year}년 {int(month)}월"


def write_ubist_workbook_submission(
    root: Path,
    *,
    category: str = CATEGORY,
    epoch: str = EPOCH,
    periods: tuple[str, ...] = ("2026-07",),
    metric_header: str = "처방조제액(원)",
    include_metric: bool = True,
    period_labels: list[str] | None = None,
    declared_rows: int | None = 3,
    complete: bool = True,
    period_start: str = "2026-06",
) -> Path:
    """Write a wide UBIST .xlsx (2-row header: metrics row1, periods row2) + a
    manifest declaring it. Mirrors the real workbook layout the loader parses so
    G4 exercises the loader's own contract. Returns the manifest path."""
    import openpyxl

    data_dir = root / category / epoch
    data_dir.mkdir(parents=True, exist_ok=True)
    xlsx_path = data_dir / "ubist.xlsx"

    labels = period_labels if period_labels is not None else [_kr_period(p) for p in periods]
    metric_slot = metric_header if include_metric else "값"
    header1 = ["브랜드"] + [metric_slot] * len(labels)
    header2 = ["브랜드"] + list(labels)

    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(header1)
    worksheet.append(header2)
    rows = declared_rows if declared_rows is not None else 3
    for index in range(rows):
        worksheet.append([f"브랜드{index}"] + [float(index + 1)] * len(labels))
    workbook.save(xlsx_path)

    sha = hashlib.sha256(xlsx_path.read_bytes()).hexdigest()
    manifest = {
        "contract_version": "v2",
        "epoch": epoch,
        "category": category,
        "complete": complete,
        "submitted_at": "2026-07-17T09:00:00+09:00",
        "files": [
            {
                "path": xlsx_path.relative_to(root).as_posix(),
                "sha256": sha,
                "rows": declared_rows,
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
