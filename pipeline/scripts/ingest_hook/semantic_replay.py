"""Compare Parquet business data with bounded, order-independent aggregates."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import duckdb


class ReplayVerificationError(RuntimeError):
    """The replay comparison cannot prove business-data equality."""


@dataclass(frozen=True, slots=True)
class ReplayConfig:
    """Explicit resource and business-column contract for a replay comparison."""

    business_columns: tuple[str, ...]
    memory_limit: str
    threads: int
    temp_directory: Path

    def __post_init__(self) -> None:
        columns = tuple(column.strip() for column in self.business_columns)
        if not columns or any(not column for column in columns):
            raise ReplayVerificationError("at least one business column is required")
        if len(set(columns)) != len(columns):
            raise ReplayVerificationError("business columns must be unique")
        if self.threads < 1:
            raise ReplayVerificationError("threads must be at least 1")
        if not self.memory_limit.strip():
            raise ReplayVerificationError("memory limit is required")
        object.__setattr__(self, "business_columns", columns)


@dataclass(frozen=True, slots=True)
class RootFingerprint:
    """Constant-size semantic summary for one Parquet root."""

    schema_sha256: str
    row_count: int
    hash_sum: int
    hash_xor: int
    hash_min: int
    hash_max: int
    file_count: int
    fingerprint: str

    def public_dict(self) -> dict[str, str | int]:
        return {
            "schema_sha256": self.schema_sha256,
            "row_count": self.row_count,
            "hash_sum": str(self.hash_sum),
            "hash_xor": str(self.hash_xor),
            "hash_min": str(self.hash_min),
            "hash_max": str(self.hash_max),
            "file_count": self.file_count,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True, slots=True)
class ReplayComparison:
    """Comparison verdict and both independently computed root fingerprints."""

    matches: bool
    expected: RootFingerprint
    actual: RootFingerprint
    config: ReplayConfig

    def public_dict(self) -> dict[str, bool | dict[str, str | int]]:
        return {
            "matches": self.matches,
            "expected": self.expected.public_dict(),
            "actual": self.actual.public_dict(),
            "execution": {
                "mode": "standalone_bounded_job",
                "memory_limit": self.config.memory_limit,
                "threads": self.config.threads,
                "materialization": "aggregate_per_parquet_file",
            },
        }


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def duckdb_session_statements(config: ReplayConfig) -> tuple[str, ...]:
    """Render resource controls applied before any Parquet scan."""
    return (
        f"SET memory_limit = {_sql_literal(config.memory_limit)}",
        f"SET threads = {config.threads}",
        f"SET temp_directory = {_sql_literal(config.temp_directory.as_posix())}",
        "SET preserve_insertion_order = false",
    )


def _parquet_files(root: Path) -> tuple[Path, ...]:
    if not root.is_dir():
        raise ReplayVerificationError(f"Parquet root is not a directory: {root}")
    files = tuple(
        sorted(
            path
            for path in root.rglob("*.parquet")
            if path.is_file() and not path.is_symlink()
        )
    )
    if not files:
        raise ReplayVerificationError(f"Parquet root has no readable files: {root}")
    return files


def _selected_schema(
    connection: duckdb.DuckDBPyConnection,
    path: Path,
    columns: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    described = connection.execute(
        "DESCRIBE SELECT * FROM read_parquet("
        "?, hive_partitioning=true, hive_types_autocast=false"
        ")",
        [str(path)],
    ).fetchall()
    type_by_name = {str(row[0]): str(row[1]) for row in described}
    missing = [column for column in columns if column not in type_by_name]
    if missing:
        raise ReplayVerificationError(
            f"{path} is missing business columns: {', '.join(missing)}"
        )
    return tuple((column, type_by_name[column]) for column in columns)


def _aggregate_file(
    connection: duckdb.DuckDBPyConnection,
    path: Path,
    columns: tuple[str, ...],
) -> tuple[int, int, int, int, int]:
    arguments = ", ".join(_quoted_identifier(column) for column in columns)
    row_hash = f"hash({arguments})"
    row = connection.execute(
        "SELECT "
        "count(*)::UBIGINT, "
        f"coalesce(sum(cast({row_hash} AS HUGEINT)), 0)::HUGEINT, "
        f"coalesce(bit_xor({row_hash}), 0)::UBIGINT, "
        f"coalesce(min({row_hash}), 0)::UBIGINT, "
        f"coalesce(max({row_hash}), 0)::UBIGINT "
        "FROM read_parquet(?, hive_partitioning=true, hive_types_autocast=false)",
        [str(path)],
    ).fetchone()
    if row is None:
        raise ReplayVerificationError(f"DuckDB returned no aggregate for {path}")
    return tuple(int(value) for value in row)


def fingerprint_parquet_root(root: Path, config: ReplayConfig) -> RootFingerprint:
    """Fingerprint one root while retaining only constant-size aggregate state."""
    files = _parquet_files(root)
    config.temp_directory.mkdir(parents=True, exist_ok=True)
    row_count = hash_sum = hash_xor = 0
    hash_min: int | None = None
    hash_max: int | None = None
    schema: tuple[tuple[str, str], ...] | None = None
    with duckdb.connect(database=":memory:") as connection:
        for statement in duckdb_session_statements(config):
            connection.execute(statement)
        for path in files:
            current_schema = _selected_schema(
                connection,
                path,
                config.business_columns,
            )
            if schema is None:
                schema = current_schema
            elif current_schema != schema:
                raise ReplayVerificationError(
                    f"business schema differs within Parquet root at {path}"
                )
            count, total, xor_value, minimum, maximum = _aggregate_file(
                connection,
                path,
                config.business_columns,
            )
            row_count += count
            hash_sum += total
            hash_xor ^= xor_value
            if count:
                hash_min = minimum if hash_min is None else min(hash_min, minimum)
                hash_max = maximum if hash_max is None else max(hash_max, maximum)
    if schema is None:
        raise ReplayVerificationError(f"Parquet schema is unavailable: {root}")
    schema_payload = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    schema_sha256 = hashlib.sha256(schema_payload.encode("utf-8")).hexdigest()
    identity = {
        "schema_sha256": schema_sha256,
        "row_count": row_count,
        "hash_sum": str(hash_sum),
        "hash_xor": str(hash_xor),
        "hash_min": str(hash_min or 0),
        "hash_max": str(hash_max or 0),
    }
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return RootFingerprint(
        schema_sha256=schema_sha256,
        row_count=row_count,
        hash_sum=hash_sum,
        hash_xor=hash_xor,
        hash_min=hash_min or 0,
        hash_max=hash_max or 0,
        file_count=len(files),
        fingerprint=fingerprint,
    )


def compare_parquet_roots(
    expected_root: Path,
    actual_root: Path,
    config: ReplayConfig,
) -> ReplayComparison:
    """Compare two roots without loading business rows into Python memory."""
    expected = fingerprint_parquet_root(expected_root, config)
    actual = fingerprint_parquet_root(actual_root, config)
    return ReplayComparison(
        matches=expected.fingerprint == actual.fingerprint,
        expected=expected,
        actual=actual,
        config=config,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.scripts.ingest_hook.semantic_replay"
    )
    parser.add_argument("--expected-root", type=Path, required=True)
    parser.add_argument("--actual-root", type=Path, required=True)
    parser.add_argument("--business-column", action="append", required=True)
    parser.add_argument("--memory-limit", default="512MB")
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--temp-directory", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the standalone verifier and emit a machine-readable verdict."""
    args = _parser().parse_args(argv)
    try:
        comparison = compare_parquet_roots(
            args.expected_root,
            args.actual_root,
            ReplayConfig(
                business_columns=tuple(args.business_column),
                memory_limit=args.memory_limit,
                threads=args.threads,
                temp_directory=args.temp_directory,
            ),
        )
        print(json.dumps(comparison.public_dict(), ensure_ascii=False, sort_keys=True))
        return 0 if comparison.matches else 3
    except (ReplayVerificationError, duckdb.Error, OSError) as exc:
        print(
            json.dumps(
                {
                    "matches": False,
                    "reason": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
