from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Mapping

import pymysql


GENERAL_BRAND_EXPECTED: Mapping[tuple[str, str], int] = {
    ("iqvia_nsa", "counting_unit"): 22_041,
    ("iqvia_nsa", "dosage_unit"): 22_041,
    ("iqvia_nsa", "sales"): 22_041,
    ("iqvia_nsa", "unit"): 22_041,
    ("ubist", "sales"): 14_131,
    ("ubist", "volume"): 14_131,
}

GENERAL_MARKET_EXPECTED: Mapping[tuple[str, str], int] = {
    ("iqvia_nsa", "counting_unit"): 539,
    ("iqvia_nsa", "dosage_unit"): 539,
    ("iqvia_nsa", "sales"): 539,
    ("iqvia_nsa", "unit"): 539,
    ("ubist", "sales"): 361,
    ("ubist", "volume"): 361,
}

BRIDGE_EXPECTED: Mapping[tuple[str], int] = {
    ("any",): 10_032,
    ("iqvia_nsa",): 43_659,
    ("ubist",): 4_639,
}

VOLATILE_COLUMNS = frozenset({"id", "computed_at"})


@dataclass(frozen=True, slots=True)
class TableDigest:
    row_count: int
    crc_sum: int
    crc_xor: int


@dataclass(frozen=True, slots=True)
class CanonicalDigest:
    row_count: int
    sha256: str


@dataclass(frozen=True, slots=True)
class VerifySpec:
    table: str
    expected_rows: int | None
    group_columns: tuple[str, ...]
    expected_groups: Mapping[tuple[str, ...], int] | None
    reference_db: str | None


@dataclass(frozen=True, slots=True)
class VerifyResult:
    table: str
    target_digest: TableDigest
    reference_db: str | None
    target_reference_digest: CanonicalDigest | None
    reference_digest: CanonicalDigest | None
    groups: Mapping[tuple[str, ...], int]


CANONICAL_REFERENCE_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "mart_general_brand_metric": (
        "source",
        "measure",
        "atc4_code",
        "brand_key",
        "unit_label",
        "raw_value_history",
    ),
    "mart_general_market_metric": (
        "source",
        "measure",
        "atc4_code",
        "unit_label",
        "market_size_series",
    ),
    "mart_brand_molecule": (
        "mart_source",
        "atc4_code",
        "brand_key",
        "molecule_norm",
        "component_count",
        "is_combo_component",
        "evidence_count",
    ),
    "mart_strategic_ml_market_metric": (
        "source",
        "measure",
        "ml_id",
        "unit_label",
        "market_size_series",
    ),
}

CANONICAL_ORDER_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "mart_general_brand_metric": ("source", "measure", "atc4_code", "brand_key"),
    "mart_general_market_metric": ("source", "measure", "atc4_code"),
    "mart_brand_molecule": ("mart_source", "atc4_code", "brand_key", "molecule_norm"),
    "mart_strategic_ml_market_metric": ("source", "measure", "ml_id"),
}


def quote_id(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def table_exists(conn: pymysql.connections.Connection, db_name: str, table_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS table_count
            FROM information_schema.tables
            WHERE table_schema=%s AND table_name=%s
            """,
            (db_name, table_name),
        )
        row = cur.fetchone()
    return int(row["table_count"]) > 0


def find_bridge_reference_db(conn: pymysql.connections.Connection) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_schema
            FROM information_schema.tables
            WHERE table_name='mart_brand_molecule'
              AND table_schema LIKE 'jw_mart_molecule_bridge_full_%'
            ORDER BY table_schema DESC
            """
        )
        schemas = [str(row["table_schema"]) for row in cur.fetchall()]
    for schema in schemas:
        if table_digest(conn, schema, "mart_brand_molecule").row_count == 58_330:
            return schema
    raise RuntimeError("No local bridge reference schema with 58,330 mart_brand_molecule rows was found")


def table_digest(conn: pymysql.connections.Connection, db_name: str, table_name: str) -> TableDigest:
    expressions = _stable_expressions(conn, db_name, table_name)
    if not expressions:
        raise RuntimeError(f"{db_name}.{table_name} has no stable columns for checksum")
    rendered = ",".join(f"COALESCE(CAST({expression} AS CHAR), '<NULL>')" for expression in expressions)
    row_crc = f"CRC32(CONCAT_WS(CHAR(31), {rendered}))"
    sql = f"""
        SELECT
          COUNT(*) AS row_count,
          COALESCE(SUM(row_crc), 0) AS crc_sum,
          COALESCE(BIT_XOR(row_crc), 0) AS crc_xor
        FROM (
          SELECT {row_crc} AS row_crc
          FROM {quote_id(db_name)}.{quote_id(table_name)}
        ) AS checksummed
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return TableDigest(
        row_count=int(row["row_count"]),
        crc_sum=int(row["crc_sum"]),
        crc_xor=int(row["crc_xor"]),
    )


def canonical_reference_digest(
    conn: pymysql.connections.Connection,
    db_name: str,
    table_name: str,
) -> CanonicalDigest:
    columns = CANONICAL_REFERENCE_COLUMNS.get(table_name)
    order_columns = CANONICAL_ORDER_COLUMNS.get(table_name)
    if not columns or not order_columns:
        exact = table_digest(conn, db_name, table_name)
        payload = json.dumps(exact.__dict__, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return CanonicalDigest(row_count=exact.row_count, sha256=hashlib.sha256(payload).hexdigest())

    rendered_columns = ",".join(quote_id(column) for column in columns)
    rendered_order = ",".join(quote_id(column) for column in order_columns)
    sql = f"""
        SELECT {rendered_columns}
        FROM {quote_id(db_name)}.{quote_id(table_name)}
        ORDER BY {rendered_order}
    """
    digest = hashlib.sha256()
    row_count = 0
    with conn.cursor(pymysql.cursors.SSDictCursor) as cur:
        cur.execute(sql)
        for row in cur:
            normalized = {column: _canonical_value(row[column]) for column in columns}
            digest.update(json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            digest.update(b"\n")
            row_count += 1
    return CanonicalDigest(row_count=row_count, sha256=digest.hexdigest())


def fetch_group_counts(
    conn: pymysql.connections.Connection,
    db_name: str,
    table_name: str,
    columns: tuple[str, ...],
) -> Mapping[tuple[str, ...], int]:
    if not columns:
        return {}
    select_cols = ",".join(quote_id(column) for column in columns)
    sql = f"""
        SELECT {select_cols}, COUNT(*) AS row_count
        FROM {quote_id(db_name)}.{quote_id(table_name)}
        GROUP BY {select_cols}
        ORDER BY {select_cols}
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    return {tuple(str(row[column]) for column in columns): int(row["row_count"]) for row in rows}


def verify_loaded_tables(
    conn: pymysql.connections.Connection,
    *,
    target_db: str,
    source_db: str,
    bridge_reference_db: str,
    include_strategic_ml_market: bool,
) -> tuple[VerifyResult, ...]:
    specs = [
        VerifySpec(
            "mart_general_brand_metric",
            116_426,
            ("source", "measure"),
            GENERAL_BRAND_EXPECTED,
            source_db,
        ),
        VerifySpec(
            "mart_general_market_metric",
            2_878,
            ("source", "measure"),
            GENERAL_MARKET_EXPECTED,
            source_db,
        ),
        VerifySpec(
            "mart_brand_molecule",
            58_330,
            ("mart_source",),
            BRIDGE_EXPECTED,
            bridge_reference_db,
        ),
    ]
    if include_strategic_ml_market and table_exists(conn, target_db, "mart_strategic_ml_market_metric"):
        reference = source_db if table_exists(conn, source_db, "mart_strategic_ml_market_metric") else None
        specs.append(VerifySpec("mart_strategic_ml_market_metric", None, ("source", "measure"), None, reference))
    return tuple(_verify_one(conn, target_db, spec) for spec in specs)


def _verify_one(conn: pymysql.connections.Connection, target_db: str, spec: VerifySpec) -> VerifyResult:
    if not table_exists(conn, target_db, spec.table):
        raise RuntimeError(f"Missing target table after publish: {target_db}.{spec.table}")
    target_digest = table_digest(conn, target_db, spec.table)
    if spec.expected_rows is not None and target_digest.row_count != spec.expected_rows:
        raise RuntimeError(
            f"{target_db}.{spec.table} row count mismatch: "
            f"expected={spec.expected_rows} actual={target_digest.row_count}"
        )
    groups = fetch_group_counts(conn, target_db, spec.table, spec.group_columns)
    if spec.expected_groups is not None and dict(groups) != dict(spec.expected_groups):
        raise RuntimeError(f"{target_db}.{spec.table} group distribution mismatch: {dict(groups)}")
    target_reference_digest = None
    reference_digest = None
    if spec.reference_db:
        if not table_exists(conn, spec.reference_db, spec.table):
            raise RuntimeError(f"Missing reference table: {spec.reference_db}.{spec.table}")
        target_reference_digest = canonical_reference_digest(conn, target_db, spec.table)
        reference_digest = canonical_reference_digest(conn, spec.reference_db, spec.table)
        if target_reference_digest != reference_digest:
            raise RuntimeError(
                f"{target_db}.{spec.table} canonical checksum mismatch against {spec.reference_db}: "
                f"target={target_reference_digest} reference={reference_digest}"
            )
    return VerifyResult(
        table=spec.table,
        target_digest=target_digest,
        reference_db=spec.reference_db,
        target_reference_digest=target_reference_digest,
        reference_digest=reference_digest,
        groups=groups,
    )


def _stable_expressions(conn: pymysql.connections.Connection, db_name: str, table_name: str) -> tuple[str, ...]:
    with conn.cursor() as cur:
        cur.execute(f"SHOW COLUMNS FROM {quote_id(db_name)}.{quote_id(table_name)}")
        rows = cur.fetchall()
    return tuple(_stable_column_expression(str(row["Field"])) for row in rows if str(row["Field"]) not in VOLATILE_COLUMNS)


def _stable_column_expression(column: str) -> str:
    quoted = quote_id(column)
    if column == "payload":
        return f"CASE WHEN JSON_VALID({quoted}) THEN JSON_REMOVE({quoted}, '$.computed_at') ELSE {quoted} END"
    return quoted


def _canonical_value(value: Any) -> Any:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        text = value.strip()
        if text and text[0] in "[{":
            try:
                return _canonical_value(json.loads(text))
            except json.JSONDecodeError:
                return value
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return str(value)
        return round(value, 4)
    if isinstance(value, dict):
        return {str(key): _canonical_value(inner) for key, inner in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, list):
        return [_canonical_value(inner) for inner in value]
    return value
