from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, TextIO

import pandas as pd

from pipeline.scripts.api.composers.number_format import format_number

_EXACT_ADDITIVE_CONTAINERS = frozenset(
    {
        "audit_code_matrix",
        "channel_data",
        "channel_specialty_matrix",
        "dimension_channel_data",
        "dimension_data",
        "dimension_specialty_data",
        "market_size_series",
        "specialty_data",
    }
)
_EXACT_ADDITIVE_LEAVES = frozenset({"growth_abs"})
_DERIVED_BOUNDARY_ULPS = 32


def _is_exact_additive_key(key: str) -> bool:
    return (
        key.startswith("raw_")
        or key in _EXACT_ADDITIVE_CONTAINERS
        or key in _EXACT_ADDITIVE_LEAVES
    )


def _canonical_number(value: float | Decimal, *, exact_additive: bool) -> float:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"non-finite canonical number: {value!r}")
        ready = float(value)
    else:
        ready = value
    if not math.isfinite(ready):
        raise ValueError(f"non-finite canonical number: {value!r}")
    if exact_additive:
        return ready
    scaled = ready * 10_000.0
    nearest_boundary = round(scaled)
    # Stabilize only representation noise adjacent to the API's 4-place boundary.
    boundary_window = _DERIVED_BOUNDARY_ULPS * math.ulp(scaled)
    if abs(scaled - nearest_boundary) <= boundary_window:
        return nearest_boundary / 10_000.0
    formatted = format_number(ready)
    if not isinstance(formatted, (int, float)) or isinstance(formatted, bool):
        raise ValueError(f"non-finite canonical number: {value!r}")
    return float(formatted)


def json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_ready(v) for v in value]
    if isinstance(value, tuple):
        return [json_ready(v) for v in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return value
    return value

def dumps(value: Any) -> str:
    return json.dumps(json_ready(value), ensure_ascii=False, separators=(",", ":"))


def canonicalize(
    value: Any,
    *,
    volatile_keys: frozenset[str] = frozenset({"computed_at"}),
    _exact_additive: bool = False,
) -> Any:
    if isinstance(value, Decimal):
        return _canonical_number(value, exact_additive=_exact_additive)
    if isinstance(value, float):
        return _canonical_number(value, exact_additive=_exact_additive)
    if isinstance(value, dict):
        return {
            unicodedata.normalize("NFC", str(key)): canonicalize(
                item,
                volatile_keys=volatile_keys,
                _exact_additive=(
                    _exact_additive or _is_exact_additive_key(str(key))
                ),
            )
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in volatile_keys
        }
    if isinstance(value, (list, tuple)):
        return [
            canonicalize(
                item,
                volatile_keys=volatile_keys,
                _exact_additive=_exact_additive,
            )
            for item in value
        ]
    ready = json_ready(value)
    if isinstance(ready, str):
        return unicodedata.normalize("NFC", ready)
    if isinstance(ready, float):
        return _canonical_number(ready, exact_additive=_exact_additive)
    return ready


def canonical_row_sha256(row: dict[str, Any]) -> str:
    payload = json.dumps(
        canonicalize(row),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def canonical_rows_sha256(
    rows: Iterable[dict[str, Any]],
    *,
    sort_key: tuple[str, ...],
) -> tuple[list[str], str]:
    ordered = sorted(
        rows,
        key=lambda row: tuple(str(row.get(key) or "") for key in sort_key),
    )
    row_hashes = [canonical_row_sha256(row) for row in ordered]
    aggregate = hashlib.sha256(("\n".join(row_hashes) + "\n").encode("ascii")).hexdigest()
    return row_hashes, aggregate


def assert_canonical_parity(
    legacy_rows: Iterable[dict[str, Any]],
    candidate_rows: Iterable[dict[str, Any]],
    *,
    sort_key: tuple[str, ...],
) -> tuple[str, str]:
    legacy_hashes, legacy_aggregate = canonical_rows_sha256(
        legacy_rows,
        sort_key=sort_key,
    )
    candidate_hashes, candidate_aggregate = canonical_rows_sha256(
        candidate_rows,
        sort_key=sort_key,
    )
    if (
        legacy_aggregate != candidate_aggregate
        or legacy_hashes != candidate_hashes
    ):
        mismatch_count = sum(
            left != right
            for left, right in zip(legacy_hashes, candidate_hashes)
        ) + abs(len(legacy_hashes) - len(candidate_hashes))
        raise AssertionError(
            "decimal-additive-v1 normalized parity mismatch: "
            f"rows={mismatch_count} "
            f"legacy={legacy_aggregate} candidate={candidate_aggregate}"
        )
    return legacy_aggregate, candidate_aggregate


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows

def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(dumps(row) + "\n")


class JsonlStreamSink:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: TextIO | None = None

    def __enter__(self) -> JsonlStreamSink:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8")
        return self

    def write(self, rows: Iterable[dict[str, Any]]) -> None:
        if self._handle is None:
            raise RuntimeError("JSONL sink is not open")
        for row in rows:
            self._handle.write(dumps(row) + "\n")

    def flush(self) -> None:
        if self._handle is None:
            raise RuntimeError("JSONL sink is not open")
        self._handle.flush()

    def __exit__(self, *_exc: object) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
