from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
import glob
import json
import math
from pathlib import Path
import shutil
import tempfile

import duckdb
import pandas as pd

from .brand_key_normalize import best_name, extract_brand_base_name, normalize_brand_name
from .general_catalog import _attach_catalog
from .general_config import LOGGER, enriched_glob, ubist_glob
from .general_window import (
    calculation_period_scope,
    filter_frame_to_rolling_window,
)
from .general_rows import build_ubist_additive_partial, reduce_ubist_additive_partials
from .general_utils import deduplicate_ubist_internal_medicine_rows, extract_atc4, ubist_channel_to_raw, ubist_specialty_to_raw


DEFAULT_UBIST_MEMORY_BUDGET_BYTES = 8 * 1024**3
DEFAULT_UBIST_ESTIMATED_ROW_BYTES = 384
DEFAULT_UBIST_PARTITION_FRACTION = 0.08
DEFAULT_UBIST_RAW_EXPANSION_FACTOR = 25
DEFAULT_UBIST_RAW_PARTITION_FRACTION = 1 / 64
DEFAULT_UBIST_ORDERED_VALUE_BYTES = 64
MIN_UBIST_ORDERED_GROUP_ROWS = 1024
DEFAULT_UBIST_DUCKDB_MEMORY_LIMIT = "2GB"
DECIMAL_ADDITIVE_CONTRACT = "decimal-additive-v1"
SOURCE_SCALE_TOLERANCE = 1e-6
MINOR_UNIT_SCALE = 100
MAX_EXACT_DOUBLE_MINOR_UNIT_VALUE = (2**53) / MINOR_UNIT_SCALE


@dataclass(frozen=True)
class UbistAtc4Partition:
    atc4_code: str
    aggregate_rows: int
    estimated_bytes: int
    bucket_count: int
    partition_id: int


@dataclass(frozen=True)
class UbistAtc4Workset:
    """Brand-stable, memory-bounded normalized rows for one ATC4."""

    atc4_code: str
    brand_parts: Path
    bucket_count: int
    aggregate_rows: int
    largest_brand_rows: int
    largest_bucket_rows: int

    def iter_frames(self) -> Iterator[pd.DataFrame]:
        for bucket in sorted(self.brand_parts.glob("__brand_bucket=*")):
            parquet_glob = bucket / "*.parquet"
            with duckdb.connect() as connection:
                frame = connection.execute(
                    f"""
                    SELECT *
                    FROM read_parquet({_sql_literal(str(parquet_glob))})
                    ORDER BY brand_key, product_code, period_yyyymm, channel, specialty
                    """
                ).df()
            _assert_minor_unit_frame(
                frame,
                ("raw_sales_minor", "raw_volume_minor"),
            )
            if not frame.empty:
                yield frame


def _subpartition_count(
    *,
    estimated_bytes: int,
    memory_budget_bytes: int,
    target_fraction: float = DEFAULT_UBIST_PARTITION_FRACTION,
) -> int:
    if memory_budget_bytes < 1:
        raise ValueError("memory_budget_bytes must be positive")
    if not 0 < target_fraction <= 1:
        raise ValueError("target_fraction must be in (0, 1]")
    target_bytes = max(1, int(memory_budget_bytes * target_fraction))
    return max(1, math.ceil(max(0, estimated_bytes) / target_bytes))


def _raw_product_bucket_count(
    *,
    source_bytes: int,
    memory_budget_bytes: int,
) -> int:
    return _subpartition_count(
        estimated_bytes=max(0, source_bytes) * DEFAULT_UBIST_RAW_EXPANSION_FACTOR,
        memory_budget_bytes=memory_budget_bytes,
        target_fraction=DEFAULT_UBIST_RAW_PARTITION_FRACTION,
    )


def _ordered_group_row_limit(memory_budget_bytes: int) -> int:
    if memory_budget_bytes < 1:
        raise ValueError("memory_budget_bytes must be positive")
    target_bytes = int(
        memory_budget_bytes * DEFAULT_UBIST_RAW_PARTITION_FRACTION
    )
    return max(
        MIN_UBIST_ORDERED_GROUP_ROWS,
        target_bytes // DEFAULT_UBIST_ORDERED_VALUE_BYTES,
    )


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def validate_source_scale(
    connection: duckdb.DuckDBPyConnection,
    relation_sql: str,
    *,
    tolerance: float = SOURCE_SCALE_TOLERANCE,
) -> dict[str, int | float]:
    row = connection.execute(
        f"""
        WITH normalized AS (
          SELECT
            rx_amt,
            rx_qty,
            TRY_CAST(rx_amt AS DECIMAL(38,12)) AS sales_input,
            TRY_CAST(rx_qty AS DECIMAL(38,12)) AS volume_input,
            TRY_CAST(rx_amt AS DECIMAL(38,2)) AS sales_normalized,
            TRY_CAST(rx_qty AS DECIMAL(38,2)) AS volume_normalized,
            TRY_CAST(rx_amt AS DOUBLE) AS sales_double,
            TRY_CAST(rx_qty AS DOUBLE) AS volume_double
          FROM ({relation_sql}) AS source_scale
        )
        SELECT
          COUNT(*) AS rows,
          COALESCE(
            MAX(CAST(ABS(sales_input - sales_normalized) AS DOUBLE)),
            0
          ) AS sales_max_residual,
          COALESCE(
            MAX(CAST(ABS(volume_input - volume_normalized) AS DOUBLE)),
            0
          ) AS volume_max_residual,
          COUNT(*) FILTER (
            WHERE ABS(sales_input - sales_normalized) > {tolerance}
          ) AS invalid_sales,
          COUNT(*) FILTER (
            WHERE ABS(volume_input - volume_normalized) > {tolerance}
          ) AS invalid_volume,
          COUNT(*) FILTER (
            WHERE rx_amt IS NOT NULL
              AND (sales_input IS NULL OR sales_normalized IS NULL)
          ) AS sales_cast_failures,
          COUNT(*) FILTER (
            WHERE rx_qty IS NOT NULL
              AND (volume_input IS NULL OR volume_normalized IS NULL)
          ) AS volume_cast_failures,
          COUNT(*) FILTER (
            WHERE ABS(sales_double) > {MAX_EXACT_DOUBLE_MINOR_UNIT_VALUE}
          ) AS sales_unsafe_magnitude,
          COUNT(*) FILTER (
            WHERE ABS(volume_double) > {MAX_EXACT_DOUBLE_MINOR_UNIT_VALUE}
          ) AS volume_unsafe_magnitude
        FROM normalized
        """
    ).fetchone()
    assert row is not None
    if any(int(value) for value in row[3:9]):
        raise ValueError(
            "decimal-additive-v1 source-scale gate failed: "
            f"sales_invalid={int(row[3])} volume_invalid={int(row[4])} "
            f"sales_cast_failures={int(row[5])} "
            f"volume_cast_failures={int(row[6])} "
            f"sales_unsafe_magnitude={int(row[7])} "
            f"volume_unsafe_magnitude={int(row[8])} "
            f"sales_max_residual={float(row[1])} "
            f"volume_max_residual={float(row[2])}"
        )
    return {
        "rows": int(row[0]),
        "sales_max_residual": float(row[1]),
        "volume_max_residual": float(row[2]),
    }


def assert_decimal_spool_schema(
    connection: duckdb.DuckDBPyConnection,
    parquet_path: str,
) -> None:
    schema = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "DESCRIBE SELECT raw_sales, raw_volume "
            f"FROM read_parquet({_sql_literal(parquet_path)})"
        ).fetchall()
    }
    invalid = {
        column: schema.get(column)
        for column in ("raw_sales", "raw_volume")
        if not str(schema.get(column) or "").startswith("DECIMAL(")
    }
    if invalid:
        raise TypeError(
            "decimal-additive-v1 spool columns must be DECIMAL: "
            f"{invalid}"
        )


def fetch_minor_unit_frame(
    connection: duckdb.DuckDBPyConnection,
    relation_sql: str,
) -> pd.DataFrame:
    schema = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            f"DESCRIBE SELECT * FROM ({relation_sql}) AS decimal_source"
        ).fetchall()
    }
    invalid = {
        column: schema.get(column)
        for column in ("raw_sales", "raw_volume")
        if not str(schema.get(column) or "").startswith("DECIMAL(")
    }
    if invalid:
        raise TypeError(
            "decimal-additive-v1 pre-reduce source must be DECIMAL: "
            f"{invalid}"
        )
    frame = connection.execute(
        f"""
        SELECT * EXCLUDE (raw_sales, raw_volume),
               CAST(raw_sales * {MINOR_UNIT_SCALE} AS BIGINT)
                 AS raw_sales_minor,
               CAST(raw_volume * {MINOR_UNIT_SCALE} AS BIGINT)
                 AS raw_volume_minor
        FROM ({relation_sql}) AS decimal_source
        """
    ).df()
    _assert_minor_unit_frame(
        frame,
        ("raw_sales_minor", "raw_volume_minor"),
    )
    return frame


def _assert_minor_unit_frame(
    frame: pd.DataFrame,
    columns: tuple[str, ...],
) -> None:
    invalid = {
        column: str(frame[column].dtype)
        for column in columns
        if column not in frame.columns
        or not pd.api.types.is_integer_dtype(frame[column].dtype)
    }
    if invalid:
        raise TypeError(
            "decimal-additive-v1 pre-reduce float conversion detected: "
            f"{invalid}"
        )


def _configure_spill(
    connection: duckdb.DuckDBPyConnection,
    *,
    temp_dir: Path,
    memory_limit: str,
) -> None:
    temp_dir.mkdir(parents=True, exist_ok=True)
    connection.execute(f"SET memory_limit={_sql_literal(memory_limit)}")
    connection.execute("SET threads=1")
    connection.execute("SET preserve_insertion_order=false")
    connection.execute("SET enable_progress_bar=false")
    connection.execute(f"SET temp_directory={_sql_literal(str(temp_dir))}")


def _extract_atc4_code(value: object) -> str:
    return extract_atc4(value)[0]


def _available_ubist_periods(
    *,
    enriched_pattern: str | None = None,
) -> tuple[str, ...]:
    if enriched_pattern is not None:
        with duckdb.connect() as connection:
            periods = tuple(
                str(row[0])
                for row in connection.execute(
                    f"""
                    SELECT DISTINCT period_yyyymm
                    FROM read_parquet({_sql_literal(enriched_pattern)})
                    WHERE source='ubist' AND period_yyyymm IS NOT NULL
                    ORDER BY period_yyyymm
                    """
                ).fetchall()
            )
        return calculation_period_scope(periods, source="ubist")

    source_periods: list[str] = []
    for path_text in glob.glob(ubist_glob()):
        path = Path(path_text)
        year = path.parent.parent.name.removeprefix("year=")
        month = path.parent.name.removeprefix("month=")
        if year.isdigit() and month.isdigit():
            source_periods.append(f"{year}-{month}")
    if source_periods:
        return calculation_period_scope(source_periods, source="ubist")
    if not glob.glob(enriched_glob()):
        return ()
    with duckdb.connect() as connection:
        periods = tuple(
            str(row[0])
            for row in connection.execute(
                f"""
                SELECT DISTINCT period_yyyymm
                FROM read_parquet({_sql_literal(enriched_glob())})
                WHERE source='ubist' AND period_yyyymm IS NOT NULL
                ORDER BY period_yyyymm
                """
            ).fetchall()
        )
    return calculation_period_scope(periods, source="ubist")


def _ubist_period_filter_sql(periods: tuple[str, ...]) -> str:
    period_values = sorted(
        {
            value
            for period in periods
            for value in (period, period.replace("-", ""))
        }
    )
    if not period_values:
        return ""
    return (
        "CAST(period_yyyymm AS VARCHAR) IN ("
        + ", ".join(_sql_literal(value) for value in period_values)
        + ")"
    )


def _raw_ubist_source_query(max_rows: int | None = None) -> str:
    limit = f"LIMIT {int(max_rows)}" if max_rows else ""
    period_filter = _ubist_period_filter_sql(_available_ubist_periods())
    return f"""
        SELECT * EXCLUDE (filename, file_row_number),
               filename AS __source_file,
               file_row_number AS __source_row
        FROM read_parquet(
          '{ubist_glob()}',
          hive_partitioning=true,
          filename=true,
          file_row_number=true
        )
        {"WHERE " + period_filter if period_filter else ""}
        {limit}
    """


def _raw_ubist_filtered_query(max_rows: int | None = None) -> str:
    return f"""
        SELECT *
        FROM ({_raw_ubist_source_query(max_rows)}) AS source
        WHERE TRY_CAST(rx_amt AS DECIMAL(38,2)) > 0
           OR TRY_CAST(rx_qty AS DECIMAL(38,2)) > 0
    """


def _raw_ubist_aggregate_from(relation_sql: str) -> str:
    return f"""
        SELECT
          CAST("약품코드" AS VARCHAR) AS product_code,
          first("제품" ORDER BY __source_file, __source_row) AS product_name,
          first("브랜드" ORDER BY __source_file, __source_row) AS brand_name,
          first("ATC" ORDER BY __source_file, __source_row) AS atc_text,
          period_yyyymm,
          "종별" AS channel,
          "진료과" AS specialty,
          first("제조사" ORDER BY __source_file, __source_row) AS manufacturer,
          first("판매사" ORDER BY __source_file, __source_row) AS company,
          first("성분" ORDER BY __source_file, __source_row) AS ubist_molecule_raw,
          first("성분용량" ORDER BY __source_file, __source_row) AS ubist_molecule_strength,
          first("제형" ORDER BY __source_file, __source_row) AS ubist_form,
          first("투여경로" ORDER BY __source_file, __source_row) AS ubist_route,
          first("급여구분" ORDER BY __source_file, __source_row) AS ubist_reimbursement,
          SUM(TRY_CAST(rx_amt AS DECIMAL(38,2))) AS raw_sales,
          SUM(TRY_CAST(rx_qty AS DECIMAL(38,2))) AS raw_volume
        FROM ({relation_sql}) AS u
        GROUP BY 1,5,6,7
    """


def _raw_ubist_ordered_aggregate_from(relation_sql: str) -> str:
    return _raw_ubist_aggregate_from(relation_sql)


def _spool_product_stable_aggregate(
    connection: duckdb.DuckDBPyConnection,
    *,
    root: Path,
    max_rows: int | None,
    memory_budget_bytes: int,
) -> tuple[Path, int, int, int]:
    raw_parts = root / "raw-product-parts"
    aggregate_parts = root / "aggregate-parts"
    shutil.rmtree(raw_parts, ignore_errors=True)
    shutil.rmtree(aggregate_parts, ignore_errors=True)
    raw_parts.mkdir(parents=True)
    aggregate_parts.mkdir(parents=True)
    source_paths = [Path(path) for path in glob.glob(ubist_glob())]
    source_bytes = sum(path.stat().st_size for path in source_paths)
    product_bucket_count = _raw_product_bucket_count(
        source_bytes=source_bytes,
        memory_budget_bytes=memory_budget_bytes,
    )
    ordered_group_row_limit = _ordered_group_row_limit(memory_budget_bytes)
    validate_source_scale(connection, _raw_ubist_source_query(max_rows))
    connection.execute(
        f"""
        COPY (
          SELECT raw.*,
                 hash(COALESCE(CAST("약품코드" AS VARCHAR), ''))
                   % {product_bucket_count} AS __product_bucket
          FROM ({_raw_ubist_filtered_query(max_rows)}) AS raw
        ) TO {_sql_literal(str(raw_parts))} (
          FORMAT PARQUET,
          COMPRESSION ZSTD,
          PARTITION_BY (__product_bucket)
        )
        """
    )
    for index, bucket in enumerate(sorted(raw_parts.glob("__product_bucket=*"))):
        relation_sql = (
            "SELECT * EXCLUDE (__product_bucket) FROM read_parquet("
            f"{_sql_literal(str(bucket / '*.parquet'))}, hive_partitioning=true)"
        )
        largest_group_rows = int(
            connection.execute(
                f"""
                SELECT COALESCE(MAX(group_rows), 0)
                FROM (
                  SELECT COUNT(*) AS group_rows
                  FROM ({relation_sql}) AS raw
                  GROUP BY CAST("약품코드" AS VARCHAR),
                           period_yyyymm,
                           "종별",
                           "진료과"
                )
                """
            ).fetchone()[0]
        )
        if largest_group_rows > ordered_group_row_limit:
            raise MemoryError(
                "raw UBIST aggregate group exceeds ordered-list bound: "
                f"rows={largest_group_rows:,} limit={ordered_group_row_limit:,} "
                f"bucket={bucket.name}"
            )
        aggregate_path = aggregate_parts / f"part-{index:05d}.parquet"
        connection.execute(
            f"COPY ({_raw_ubist_ordered_aggregate_from(relation_sql)}) "
            f"TO {_sql_literal(str(aggregate_path))} "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        assert_decimal_spool_schema(connection, str(aggregate_path))
    return (
        aggregate_parts,
        source_bytes,
        product_bucket_count,
        ordered_group_row_limit,
    )


def _build_atc4_spool(
    *,
    root: Path,
    max_rows: int | None,
    memory_budget_bytes: int,
    estimated_row_bytes: int,
    target_fraction: float,
    duckdb_memory_limit: str,
) -> list[UbistAtc4Partition]:
    if estimated_row_bytes < 1:
        raise ValueError("estimated_row_bytes must be positive")
    parts = root / "parts"
    temp = root / "duckdb-temp"
    shutil.rmtree(parts, ignore_errors=True)
    shutil.rmtree(temp, ignore_errors=True)
    parts.mkdir(parents=True)

    with duckdb.connect() as connection:
        _configure_spill(connection, temp_dir=temp, memory_limit=duckdb_memory_limit)
        (
            aggregate_parts,
            source_bytes,
            product_bucket_count,
            ordered_group_row_limit,
        ) = (
            _spool_product_stable_aggregate(
                connection,
                root=root,
                max_rows=max_rows,
                memory_budget_bytes=memory_budget_bytes,
            )
        )
        connection.create_function(
            "extract_atc4_code",
            _extract_atc4_code,
            ["VARCHAR"],
            "VARCHAR",
            null_handling="special",
        )
        aggregate_files = sorted(aggregate_parts.glob("*.parquet"))
        stats = (
            connection.execute(
                f"""
                SELECT extract_atc4_code(atc_text) AS atc4_code,
                       COUNT(*) AS aggregate_rows
                FROM read_parquet({_sql_literal(str(aggregate_parts / '*.parquet'))})
                GROUP BY 1
                ORDER BY 1
                """
            ).fetchall()
            if aggregate_files
            else []
        )
        plans = [
            UbistAtc4Partition(
                atc4_code=str(atc4_code or "UNKNOWN"),
                aggregate_rows=int(row_count),
                estimated_bytes=int(row_count) * estimated_row_bytes,
                bucket_count=_subpartition_count(
                    estimated_bytes=int(row_count) * estimated_row_bytes,
                    memory_budget_bytes=memory_budget_bytes,
                    target_fraction=target_fraction,
                ),
                partition_id=index,
            )
            for index, (atc4_code, row_count) in enumerate(stats)
        ]
        if plans:
            atc_id_sql = "CASE extracted_atc4 " + " ".join(
                f"WHEN {_sql_literal(plan.atc4_code)} THEN {plan.partition_id}"
                for plan in plans
            ) + " ELSE -1 END"
            bucket_sql = "CASE extracted_atc4 " + " ".join(
                (
                    f"WHEN {_sql_literal(plan.atc4_code)} THEN "
                    f"hash(COALESCE(product_code, '')) % {plan.bucket_count}"
                )
                for plan in plans
            ) + " ELSE 0 END"
            connection.execute(
                f"""
                COPY (
                  SELECT staged.* EXCLUDE (extracted_atc4),
                         {atc_id_sql} AS __atc_id,
                         {bucket_sql} AS __bucket
                  FROM (
                    SELECT aggregated.*,
                           extract_atc4_code(atc_text) AS extracted_atc4
                    FROM read_parquet(
                      {_sql_literal(str(aggregate_parts / '*.parquet'))}
                    ) AS aggregated
                  ) AS staged
                ) TO {_sql_literal(str(parts))} (
                  FORMAT PARQUET,
                  COMPRESSION ZSTD,
                  PARTITION_BY (__atc_id, __bucket)
                )
                """
            )
    manifest = {
        "partition_contract": "atc4-product-stable-v1",
        "aggregation_contract": DECIMAL_ADDITIVE_CONTRACT,
        "memory_budget_bytes": memory_budget_bytes,
        "estimated_row_bytes": estimated_row_bytes,
        "target_fraction": target_fraction,
        "source_bytes": source_bytes,
        "product_bucket_count": product_bucket_count,
        "ordered_group_row_limit": ordered_group_row_limit,
        "partitions": [
            {
                "atc4_code": plan.atc4_code,
                "aggregate_rows": plan.aggregate_rows,
                "estimated_bytes": plan.estimated_bytes,
                "bucket_count": plan.bucket_count,
                "partition_id": plan.partition_id,
            }
            for plan in plans
        ],
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return plans


def _build_atc4_workset(
    *,
    root: Path,
    plan: UbistAtc4Partition,
    memory_budget_bytes: int,
    estimated_row_bytes: int,
    target_fraction: float,
    duckdb_memory_limit: str,
) -> UbistAtc4Workset | None:
    atc_root = root / "parts" / f"__atc_id={plan.partition_id}"
    normalized_root = root / "normalized" / f"__atc_id={plan.partition_id}"
    brand_parts = root / "brand-parts" / f"__atc_id={plan.partition_id}"
    shutil.rmtree(normalized_root, ignore_errors=True)
    shutil.rmtree(brand_parts, ignore_errors=True)
    normalized_root.mkdir(parents=True)
    brand_parts.mkdir(parents=True)
    for index, bucket in enumerate(sorted(atc_root.glob("__bucket=*"))):
        parquet_glob = bucket / "*.parquet"
        with duckdb.connect() as connection:
            assert_decimal_spool_schema(connection, str(parquet_glob))
            frame = fetch_minor_unit_frame(
                connection,
                f"SELECT * FROM read_parquet({_sql_literal(str(parquet_glob))})",
            )
        normalized = _normalize_raw_ubist_frame(frame)
        if not normalized.empty:
            partial = build_ubist_additive_partial(normalized)
            partial_path = normalized_root / f"part-{index:05d}.parquet"
            with duckdb.connect() as connection:
                connection.register("partial_state", partial.frame)
                connection.execute(
                    f"COPY partial_state TO {_sql_literal(str(partial_path))} "
                    "(FORMAT PARQUET, COMPRESSION ZSTD)"
                )
        del frame, normalized
    normalized_glob = normalized_root / "*.parquet"
    if not list(normalized_root.glob("*.parquet")):
        return None

    target_bytes = max(1, int(memory_budget_bytes * target_fraction))
    target_rows = max(1, target_bytes // estimated_row_bytes)
    with duckdb.connect() as connection:
        _configure_spill(
            connection,
            temp_dir=root / "duckdb-temp-worksets",
            memory_limit=duckdb_memory_limit,
        )
        largest_brand_rows = int(
            connection.execute(
                f"""
                SELECT COALESCE(MAX(row_count), 0)
                FROM (
                  SELECT brand_key, COUNT(*) AS row_count
                  FROM read_parquet({_sql_literal(str(normalized_glob))})
                  GROUP BY 1
                )
                """
            ).fetchone()[0]
        )
        if largest_brand_rows > target_rows:
            raise MemoryError(
                f"ATC4 {plan.atc4_code} has one brand with "
                f"{largest_brand_rows:,} rows, exceeding bounded target "
                f"{target_rows:,}; refusing unbounded materialization"
            )
        bucket_count = max(1, plan.bucket_count)
        while True:
            largest_bucket_rows = int(
                connection.execute(
                    f"""
                    SELECT COALESCE(MAX(row_count), 0)
                    FROM (
                      SELECT hash(COALESCE(brand_key, '')) % {bucket_count} AS bucket,
                             COUNT(*) AS row_count
                      FROM read_parquet({_sql_literal(str(normalized_glob))})
                      GROUP BY 1
                    )
                    """
                ).fetchone()[0]
            )
            if largest_bucket_rows <= target_rows:
                break
            bucket_count = max(
                bucket_count + 1,
                math.ceil(bucket_count * largest_bucket_rows / target_rows),
            )
        connection.execute(
            f"""
            COPY (
              SELECT normalized.*,
                     hash(COALESCE(brand_key, '')) % {bucket_count} AS __brand_bucket
              FROM read_parquet({_sql_literal(str(normalized_glob))}) AS normalized
            ) TO {_sql_literal(str(brand_parts))} (
              FORMAT PARQUET,
              COMPRESSION ZSTD,
              PARTITION_BY (__brand_bucket)
            )
            """
        )
    workset = UbistAtc4Workset(
        atc4_code=plan.atc4_code,
        brand_parts=brand_parts,
        bucket_count=bucket_count,
        aggregate_rows=plan.aggregate_rows,
        largest_brand_rows=largest_brand_rows,
        largest_bucket_rows=largest_bucket_rows,
    )
    with (root / "worksets.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "atc4_code": workset.atc4_code,
                    "aggregate_rows": workset.aggregate_rows,
                    "brand_bucket_count": workset.bucket_count,
                    "largest_brand_rows": workset.largest_brand_rows,
                    "largest_bucket_rows": workset.largest_bucket_rows,
                    "estimated_largest_bucket_bytes": (
                        workset.largest_bucket_rows * estimated_row_bytes
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )
    return workset


def iter_ubist_atc4_worksets(
    *,
    max_rows: int | None = None,
    spool_dir: Path | None = None,
    limit_atc4: int | None = None,
    memory_budget_bytes: int = DEFAULT_UBIST_MEMORY_BUDGET_BYTES,
    estimated_row_bytes: int = DEFAULT_UBIST_ESTIMATED_ROW_BYTES,
    target_fraction: float = DEFAULT_UBIST_PARTITION_FRACTION,
    duckdb_memory_limit: str = DEFAULT_UBIST_DUCKDB_MEMORY_LIMIT,
    atc4_scope: tuple[str, ...] | None = None,
) -> Iterator[UbistAtc4Workset]:
    """Yield brand-stable bounded worksets from a one-scan raw UBIST spool."""
    owned_spool = spool_dir is None
    root = Path(tempfile.mkdtemp(prefix="ubist-atc4-")) if owned_spool else Path(spool_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "worksets.jsonl").unlink(missing_ok=True)
    plans = _build_atc4_spool(
        root=root,
        max_rows=max_rows,
        memory_budget_bytes=memory_budget_bytes,
        estimated_row_bytes=estimated_row_bytes,
        target_fraction=target_fraction,
        duckdb_memory_limit=duckdb_memory_limit,
    )
    selected = [plan for plan in plans if plan.atc4_code != "UNKNOWN"]
    if atc4_scope:
        requested = {str(value).strip().upper() for value in atc4_scope}
        selected = [plan for plan in selected if plan.atc4_code.upper() in requested]
    if limit_atc4:
        selected = selected[:limit_atc4]
    if not limit_atc4:
        selected = plans
    try:
        for plan in selected:
            workset = _build_atc4_workset(
                root=root,
                plan=plan,
                memory_budget_bytes=memory_budget_bytes,
                estimated_row_bytes=estimated_row_bytes,
                target_fraction=target_fraction,
                duckdb_memory_limit=duckdb_memory_limit,
            )
            if workset is not None:
                yield workset
    finally:
        if owned_spool:
            shutil.rmtree(root, ignore_errors=True)


def iter_ubist_atc4_frames(
    **kwargs,
) -> Iterator[tuple[str, pd.DataFrame]]:
    """Compatibility surface for bounded fixtures; production uses worksets."""
    for workset in iter_ubist_atc4_worksets(**kwargs):
        frames = list(workset.iter_frames())
        if frames:
            yield workset.atc4_code, reduce_ubist_additive_partials(
                workset.atc4_code,
                [build_ubist_additive_partial(frame) for frame in frames],
            )


def _raw_ubist_aggregate_query(max_rows: int | None = None) -> str:
    return _raw_ubist_aggregate_from(_raw_ubist_filtered_query(max_rows))


def _normalize_raw_ubist_frame(frame: pd.DataFrame) -> pd.DataFrame:
    _assert_minor_unit_frame(
        frame,
        ("raw_sales_minor", "raw_volume_minor"),
    )
    frame["source"] = "ubist"
    frame["audit_code"] = frame["product_code"].fillna("").astype(str)
    frame["display_priority_value_minor"] = frame["raw_sales_minor"]
    frame["brand_name"] = frame.apply(
        lambda row: best_name(
            extract_brand_base_name(row.get("product_name")),
            row.get("brand_name"),
            row.get("product_code"),
        ),
        axis=1,
    )
    frame["brand_key"] = frame["brand_name"].map(normalize_brand_name)
    atc = frame["atc_text"].map(extract_atc4)
    frame["atc4_code"] = atc.map(lambda pair: pair[0])
    frame["atc4_desc"] = atc.map(lambda pair: pair[1])
    frame["channel"] = frame["channel"].map(ubist_channel_to_raw)
    frame["specialty"] = frame["specialty"].map(ubist_specialty_to_raw)
    frame = deduplicate_ubist_internal_medicine_rows(frame)
    frame = frame.loc[frame["brand_key"] != ""].copy()
    return filter_frame_to_rolling_window(frame, source="ubist", calculation=True)


def iter_ubist_base_frames(
    *,
    max_rows: int | None = None,
    spool_dir: Path | None = None,
    partition_count: int = 64,
) -> Iterator[pd.DataFrame]:
    """Yield product-stable raw UBIST partitions without materializing the full aggregate."""
    if partition_count < 1:
        raise ValueError("partition_count must be positive")
    owned_spool = spool_dir is None
    root = Path(tempfile.mkdtemp(prefix="ubist-sidecar-")) if owned_spool else spool_dir
    assert root is not None
    parts = root / "parts"
    temp = root / "duckdb-temp"
    shutil.rmtree(parts, ignore_errors=True)
    shutil.rmtree(temp, ignore_errors=True)
    parts.mkdir(parents=True)
    temp.mkdir(parents=True)
    source_query = _raw_ubist_filtered_query(max_rows)
    query = _raw_ubist_aggregate_from(source_query)
    parts_sql = str(parts).replace("'", "''")
    temp_sql = str(temp).replace("'", "''")
    LOGGER.info("[ubist] spooling raw aggregate into %d product-stable partitions", partition_count)
    con = duckdb.connect()
    try:
        con.execute("SET memory_limit='4GB'")
        con.execute("SET threads=2")
        con.execute(f"SET temp_directory='{temp_sql}'")
        validate_source_scale(con, _raw_ubist_source_query(max_rows))
        con.execute(
            f"""
            COPY (
              SELECT aggregated.*,
                     hash(COALESCE(product_code, '')) % {partition_count} AS __bucket
              FROM ({query}) AS aggregated
            ) TO '{parts_sql}' (
              FORMAT PARQUET,
              PARTITION_BY (__bucket)
            )
            """
        )
    finally:
        con.close()

    try:
        for partition in sorted(parts.glob("__bucket=*")):
            parquet_glob = str(partition / "*.parquet").replace("'", "''")
            partition_con = duckdb.connect()
            try:
                assert_decimal_spool_schema(partition_con, parquet_glob)
                frame = fetch_minor_unit_frame(
                    partition_con,
                    f"SELECT * FROM read_parquet('{parquet_glob}')",
                )
            finally:
                partition_con.close()
            yield _normalize_raw_ubist_frame(frame)
    finally:
        if owned_spool:
            shutil.rmtree(root, ignore_errors=True)

def load_ubist_base_frame(max_rows: int | None = None, ml: str | None = None) -> pd.DataFrame:
    if ml is None and os.environ.get("S4_INPUT_MODE", "raw") != "enriched":
        source_query = _raw_ubist_filtered_query(max_rows)
        query = _raw_ubist_aggregate_from(source_query)
        LOGGER.info("[ubist] aggregating raw UBIST parquet for all ATC4")
        with tempfile.TemporaryDirectory(prefix="ubist-aggregate-") as work_dir:
            spill_dir = Path(work_dir) / "spill"
            spill_dir.mkdir()
            con = duckdb.connect()
            try:
                con.execute("SET memory_limit='4GB'")
                con.execute("SET threads=2")
                con.execute("SET temp_directory=?", [str(spill_dir)])
                validate_source_scale(con, _raw_ubist_source_query(max_rows))
                frame = fetch_minor_unit_frame(con, query)
            finally:
                con.close()
        return _normalize_raw_ubist_frame(frame)

    limit = f"LIMIT {int(max_rows)}" if max_rows else ""
    parquet_glob = enriched_glob(ml)
    period_filter = _ubist_period_filter_sql(
        _available_ubist_periods(enriched_pattern=parquet_glob)
    )
    enriched_source = f"""
        SELECT * EXCLUDE (raw_rx_amt, raw_rx_qty),
               raw_rx_amt AS rx_amt,
               raw_rx_qty AS rx_qty
        FROM read_parquet({_sql_literal(parquet_glob)})
        WHERE source='ubist'
          {"AND " + period_filter if period_filter else ""}
        {limit}
    """
    filtered_enriched = f"""
        SELECT *
        FROM ({enriched_source}) AS source
        WHERE TRY_CAST(rx_amt AS DECIMAL(38,2)) > 0
           OR TRY_CAST(rx_qty AS DECIMAL(38,2)) > 0
    """
    query = f"""
        SELECT
          ml_id,
          product_id,
          split_part(source_row_id, '::', 6) AS product_code,
          period_yyyymm,
          channel,
          specialty,
          SUM(TRY_CAST(rx_amt AS DECIMAL(38,2))) AS raw_sales,
          SUM(TRY_CAST(rx_qty AS DECIMAL(38,2))) AS raw_volume
        FROM ({filtered_enriched}) AS e
        GROUP BY 1,2,3,4,5,6
    """
    LOGGER.info("[ubist] aggregating Layer 2 enriched parquet")
    con = duckdb.connect()
    try:
        validate_source_scale(con, enriched_source)
        frame = fetch_minor_unit_frame(con, query)
    finally:
        con.close()
    frame = _attach_catalog(frame)
    frame["source"] = "ubist"
    frame["audit_code"] = frame["product_code"].fillna("").astype(str)
    frame["display_priority_value_minor"] = frame["raw_sales_minor"]
    codes = [code for code in frame["product_code"].dropna().astype(str).unique().tolist() if code]
    atc_map: dict[str, tuple[str, str | None]] = {}
    dimension_map: dict[str, dict[str, object]] = {}
    if codes:
        con = duckdb.connect()
        con.register("codes", pd.DataFrame({"product_code": codes}))
        try:
            mapping = con.execute(
                f"""
                SELECT
                  CAST(u.약품코드 AS VARCHAR) AS product_code,
                  first(u.ATC) AS atc_text,
                  first(u.성분) AS ubist_molecule_raw,
                  first(u.성분용량) AS ubist_molecule_strength,
                  first(u.제형) AS ubist_form,
                  first(u.투여경로) AS ubist_route,
                  first(u.급여구분) AS ubist_reimbursement
                FROM read_parquet('{ubist_glob()}') AS u
                JOIN codes AS c ON CAST(u.약품코드 AS VARCHAR)=c.product_code
                GROUP BY 1
                """
            ).df()
        finally:
            con.close()
        atc_map = {row["product_code"]: extract_atc4(row["atc_text"]) for _, row in mapping.iterrows()}
        dimension_map = mapping.set_index("product_code")[
            ["ubist_molecule_raw", "ubist_molecule_strength", "ubist_form", "ubist_route", "ubist_reimbursement"]
        ].to_dict("index")
    atc = frame.apply(
        lambda row: atc_map.get(str(row.get("product_code")), (row.get("catalog_atc4_code") or "UNKNOWN", None)),
        axis=1,
    )
    frame["atc4_code"] = atc.map(lambda pair: pair[0])
    frame["atc4_desc"] = atc.map(lambda pair: pair[1])
    for column in ("ubist_molecule_raw", "ubist_molecule_strength", "ubist_form", "ubist_route", "ubist_reimbursement"):
        if codes:
            frame[column] = frame["product_code"].map(lambda code: dimension_map.get(str(code), {}).get(column))
        else:
            frame[column] = None
    frame["channel"] = frame["channel"].map(ubist_channel_to_raw)
    frame["specialty"] = frame["specialty"].map(ubist_specialty_to_raw)
    frame = deduplicate_ubist_internal_medicine_rows(frame)
    frame = frame.loc[frame["brand_key"] != ""].copy()
    return filter_frame_to_rolling_window(frame, source="ubist", calculation=True)

def ubist_measure_frame(base: pd.DataFrame, measure: str) -> pd.DataFrame:
    frame = base.copy()
    frame["measure"] = measure
    minor_column = (
        "raw_sales_minor" if measure == "sales" else "raw_volume_minor"
    )
    _assert_minor_unit_frame(frame, (minor_column,))
    frame["raw_value"] = frame[minor_column] / MINOR_UNIT_SCALE
    return frame.loc[frame["raw_value"].notna() & (frame["raw_value"] > 0)].copy()
