from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq


def resolve_ubist_latest(base_dir: Path) -> Path:
    parts = sorted(base_dir.glob("year=*/month=*/data.parquet"))
    if not parts:
        raise FileNotFoundError(f"no UBIST parquet partitions under {base_dir}")
    return parts[-1]


def resolve_iqvia_latest(base_dir: Path) -> Path:
    parts = sorted(base_dir.glob("*.parquet"))
    if not parts:
        raise FileNotFoundError(f"no IQVIA NSA parquet partitions under {base_dir}")
    return parts[-1]


def partition_label_from_path(path: Path) -> str:
    parts = path.parts
    year = next((part.split("=", 1)[1] for part in parts if part.startswith("year=")), None)
    month = next((part.split("=", 1)[1] for part in parts if part.startswith("month=")), None)
    if year and month:
        return f"{year}-{month}"
    return path.stem


def read_parquet_compat(path: Path, columns: list[str], aliases: dict[str, str]) -> pd.DataFrame:
    schema_names = set(pq.read_schema(path).names)
    if set(columns).issubset(schema_names):
        return pd.read_parquet(path, columns=columns)
    source_columns = [aliases.get(column, column) for column in columns]
    missing = [column for column in source_columns if column not in schema_names]
    if missing:
        raise ValueError(f"{path} missing columns for compatibility read: {missing}")
    frame = pd.read_parquet(path, columns=source_columns)
    return frame.rename(columns={source: target for target, source in aliases.items()})


def read_parquet_compat_rows(path: Path, columns: list[str], aliases: dict[str, str]) -> list[dict[str, Any]]:
    return read_parquet_compat(path, columns, aliases).to_dict("records")
