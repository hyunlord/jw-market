from __future__ import annotations

import json
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq


def utc_now_datetime() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFC", str(value)).strip()
    if not text or text.lower() == "nan":
        return None
    return text


def clean_market_text(value: Any) -> str | None:
    text = clean_text(value)
    if text is None:
        return None
    return text.replace("위너프A+", "위너프에이플러스")


def read_parquet_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"required parquet not found: {path}")
    return pq.read_table(path).to_pylist()


def parse_json_text(value: Any, fallback: Any) -> Any:
    text = clean_text(value)
    if text is None:
        return fallback
    return json.loads(text)


def source_file_version(
    rows: list[dict[str, Any]],
    *,
    expected: str,
    label: str = "source_file_version",
    cleaner=clean_text,
) -> str:
    versions = {
        cleaner(row.get("source_file_version"))
        for row in rows
        if cleaner(row.get("source_file_version")) is not None
    }
    normalized_expected = unicodedata.normalize("NFC", expected)
    if versions != {normalized_expected}:
        raise ValueError(
            f"{label} mismatch: expected={expected!r}, "
            f"actual={sorted(v for v in versions if v)}"
        )
    return expected
