from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pymysql

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
for candidate in (Path(__file__).resolve(), *Path(__file__).resolve().parents, Path("/app"), Path("/workspace")):
    if (candidate / "pipeline").exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from cache_build_common import mariadb_connect
from pipeline.scripts.utils.brand_name_normalize import compact_brand_name


logger = logging.getLogger(__name__)

TARGET_DATABASE: Final[str] = "jw_mart_d2_stage_20260630_r2"
CACHE_TABLE: Final[str] = "cache_deep_analysis"
GENERAL_BRAND_TABLE: Final[str] = "mart_general_brand_metric"
GENERAL_DIMENSION_TABLE: Final[str] = "mart_general_filter_dimension_metric"
LOAD_BATCH_SIZE: Final[int] = 500
FULL_SCAN_BRAND_THRESHOLD: Final[int] = 1000

UBIST_DIMENSIONS: Final[dict[str, str]] = {
    "seller": "seller",
    "molecule_strength": "molecule_strength",
    "form": "form",
    "route": "route",
    "reimbursement": "reimbursement",
}
IQVIA_DIMENSIONS: Final[dict[str, str]] = {
    "mfr": "mfr_name_kor",
    "molecule_type": "molecule_type",
    "molecule_desc": "molecule_desc",
    "pack": "pack_desc",
    "strength": "strength",
    "nhi": "nhi_type",
}


class CacheBrandFactorsError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BrandFactorSummary:
    brands_seen: int
    brands_with_atc: int
    brands_with_ubist: int
    brands_with_iqvia: int

    def to_json(self) -> dict[str, int]:
        return {
            "brands_seen": self.brands_seen,
            "brands_with_atc": self.brands_with_atc,
            "brands_with_ubist": self.brands_with_ubist,
            "brands_with_iqvia": self.brands_with_iqvia,
        }


def quote_ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", name or ""):
        raise CacheBrandFactorsError(f"unsafe identifier: {name!r}")
    return "`" + name.replace("`", "``") + "`"


def empty_brand_factors() -> dict[str, Any]:
    return {
        "atc": [],
        "ubist": {key: [] for key in UBIST_DIMENSIONS.values()},
        "iqvia": {key: [] for key in IQVIA_DIMENSIONS.values()},
    }


def clean_factor_value(value: object) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text or text.casefold() in {"nan", "none", "null", "<na>", "n/a", "na", "-"}:
        return None
    return text


def add_unique(bucket: list[str], value: object) -> None:
    cleaned = clean_factor_value(value)
    if cleaned and cleaned not in bucket:
        bucket.append(cleaned)


def iter_batches(values: Sequence[str], batch_size: int = LOAD_BATCH_SIZE) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), batch_size):
        yield values[start : start + batch_size]


def build_brand_factor_map(
    *,
    brands: Sequence[str],
    atc_rows: Iterable[Mapping[str, object]],
    dimension_rows: Iterable[Mapping[str, object]],
) -> dict[str, dict[str, Any]]:
    atc_row_list = tuple(atc_rows)
    dimension_row_list = tuple(dimension_rows)
    factors = {brand: empty_brand_factors() for brand in brands}
    compact_to_brands: dict[str, list[str]] = {}
    for brand in brands:
        compact = compact_brand_name(brand)
        if not compact:
            continue
        compact_to_brands.setdefault(compact, []).append(brand)

    source_brand_names = {
        str(row.get("brand_name") or "")
        for row in (*atc_row_list, *dimension_row_list)
        if row.get("brand_name")
    }
    source_compact_to_brand: dict[str, str] = {}
    ambiguous_source_compact_keys: set[str] = set()
    exact_target_brands = set(factors)
    for source_brand in source_brand_names - exact_target_brands:
        compact = compact_brand_name(source_brand)
        if not compact:
            continue
        previous = source_compact_to_brand.get(compact)
        if previous is None:
            source_compact_to_brand[compact] = source_brand
        elif previous != source_brand:
            ambiguous_source_compact_keys.add(compact)

    logged_ambiguous_source_compact_keys: set[str] = set()

    def target_brands_for(row_brand: object) -> tuple[str, ...]:
        brand = str(row_brand or "")
        targets: list[str] = []
        if brand in factors:
            targets.append(brand)
        compact = compact_brand_name(brand)
        if compact in ambiguous_source_compact_keys:
            if compact not in logged_ambiguous_source_compact_keys:
                logger.warning(
                    "ambiguous compact brand factor lookup skipped",
                    extra={"source_brand": brand, "compact_brand": compact},
                )
                logged_ambiguous_source_compact_keys.add(compact)
            return tuple(targets)
        compact_targets = compact_to_brands.get(compact, [])
        if not targets and len(compact_targets) > 1:
            return ()
        for target in compact_targets:
            if target not in targets:
                targets.append(target)
        return tuple(targets)

    for row in atc_row_list:
        for brand in target_brands_for(row.get("brand_name")):
            add_unique(factors[brand]["atc"], row.get("atc4_code"))

    for row in dimension_row_list:
        source = str(row.get("source") or "")
        dimension_type = str(row.get("dimension_type") or "")
        target_key = None
        target_group = None
        if source == "ubist":
            target_key = UBIST_DIMENSIONS.get(dimension_type)
            target_group = "ubist"
        elif source == "iqvia_nsa":
            target_key = IQVIA_DIMENSIONS.get(dimension_type)
            target_group = "iqvia"
        if target_key and target_group:
            for brand in target_brands_for(row.get("brand_name")):
                add_unique(factors[brand][target_group][target_key], row.get("dimension_value"))

    for payload in factors.values():
        payload["atc"].sort()
        for source_key in ("ubist", "iqvia"):
            for values in payload[source_key].values():
                values.sort()
    return factors


def dump_brand_factors(value: Mapping[str, Any] | None) -> str:
    return json.dumps(value or empty_brand_factors(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def load_cached_brands(conn: Any, cache_table: str = CACHE_TABLE) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT brand FROM {quote_ident(cache_table)} ORDER BY brand")
        return [str(row["brand"]) for row in cur.fetchall()]


def load_brand_factor_map(conn: Any, brands: Sequence[str]) -> dict[str, dict[str, Any]]:
    if not brands:
        return {}
    if len(brands) >= FULL_SCAN_BRAND_THRESHOLD:
        return _load_brand_factor_map_full_scan(conn, brands)

    atc_rows: list[Mapping[str, object]] = []
    dimension_rows: list[Mapping[str, object]] = []
    for batch in iter_batches(list(brands)):
        placeholders = ", ".join(["%s"] * len(batch))
        compact_batch = [compact_brand_name(brand) for brand in batch if compact_brand_name(brand)]
        compact_placeholders = ", ".join(["%s"] * len(compact_batch))
        compact_filter = ""
        compact_params: tuple[str, ...] = ()
        if compact_batch:
            compact_filter = f" OR REPLACE(REPLACE(REPLACE(REPLACE(brand_name, ' ', ''), CHAR(9), ''), CHAR(10), ''), CHAR(13), '') IN ({compact_placeholders})"
            compact_params = tuple(compact_batch)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT brand_name, atc4_code
                FROM {quote_ident(GENERAL_BRAND_TABLE)}
                WHERE (brand_name IN ({placeholders}){compact_filter})
                  AND NULLIF(atc4_code, '') IS NOT NULL
                """,
                tuple(batch) + compact_params,
            )
            atc_rows.extend(cur.fetchall())
            cur.execute(
                f"""
                SELECT brand_name, source, dimension_type, dimension_value
                FROM {quote_ident(GENERAL_DIMENSION_TABLE)}
                WHERE (brand_name IN ({placeholders}){compact_filter})
                  AND (
                    (source = 'ubist' AND dimension_type IN ('seller','molecule_strength','form','route','reimbursement'))
                    OR
                    (source = 'iqvia_nsa' AND dimension_type IN ('mfr','molecule_type','molecule_desc','pack','strength','nhi'))
                  )
                """,
                tuple(batch) + compact_params,
            )
            dimension_rows.extend(cur.fetchall())
    return build_brand_factor_map(brands=brands, atc_rows=atc_rows, dimension_rows=dimension_rows)


def _load_brand_factor_map_full_scan(conn: Any, brands: Sequence[str]) -> dict[str, dict[str, Any]]:
    atc_rows: list[Mapping[str, object]] = []
    dimension_rows: list[Mapping[str, object]] = []
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT brand_name, atc4_code
            FROM {quote_ident(GENERAL_BRAND_TABLE)}
            WHERE NULLIF(atc4_code, '') IS NOT NULL
            """
        )
        atc_rows.extend(cur.fetchall())
        cur.execute(
            f"""
            SELECT brand_name, source, dimension_type, dimension_value
            FROM {quote_ident(GENERAL_DIMENSION_TABLE)}
            WHERE (source = 'ubist' AND dimension_type IN ('seller','molecule_strength','form','route','reimbursement'))
               OR (source = 'iqvia_nsa' AND dimension_type IN ('mfr','molecule_type','molecule_desc','pack','strength','nhi'))
            """
        )
        dimension_rows.extend(cur.fetchall())
    return build_brand_factor_map(brands=brands, atc_rows=atc_rows, dimension_rows=dimension_rows)


def summarize_brand_factors(factors_by_brand: Mapping[str, Mapping[str, Any]]) -> BrandFactorSummary:
    brands_with_atc = 0
    brands_with_ubist = 0
    brands_with_iqvia = 0
    for payload in factors_by_brand.values():
        if payload.get("atc"):
            brands_with_atc += 1
        ubist = payload.get("ubist")
        if isinstance(ubist, Mapping) and any(ubist.get(key) for key in UBIST_DIMENSIONS.values()):
            brands_with_ubist += 1
        iqvia = payload.get("iqvia")
        if isinstance(iqvia, Mapping) and any(iqvia.get(key) for key in IQVIA_DIMENSIONS.values()):
            brands_with_iqvia += 1
    return BrandFactorSummary(
        brands_seen=len(factors_by_brand),
        brands_with_atc=brands_with_atc,
        brands_with_ubist=brands_with_ubist,
        brands_with_iqvia=brands_with_iqvia,
    )


def column_exists(conn: Any, table: str, column: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS c
            FROM information_schema.columns
            WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s
            """,
            (table, column),
        )
        return int(cur.fetchone()["c"]) == 1


def ensure_brand_factors_column(conn: Any, cache_table: str = CACHE_TABLE) -> bool:
    if column_exists(conn, cache_table, "brand_factors"):
        return False
    with conn.cursor() as cur:
        cur.execute(
            f"""
            ALTER TABLE {quote_ident(cache_table)}
            ADD COLUMN brand_factors LONGTEXT NULL CHECK (brand_factors IS NULL OR JSON_VALID(brand_factors))
            AFTER payload_size
            """
        )
    return True


def backfill_brand_factors(conn: Any, cache_table: str = CACHE_TABLE) -> dict[str, Any]:
    ensure_brand_factors_column(conn, cache_table)
    brands = load_cached_brands(conn, cache_table)
    factors_by_brand = load_brand_factor_map(conn, brands)
    payloads = [(dump_brand_factors(factors_by_brand.get(brand)), brand) for brand in brands]
    with conn.cursor() as cur:
        sql = f"UPDATE {quote_ident(cache_table)} SET brand_factors = %s WHERE brand = %s"
        for start in range(0, len(payloads), LOAD_BATCH_SIZE):
            cur.executemany(sql, payloads[start : start + LOAD_BATCH_SIZE])
        conn.commit()
    return {
        "updated_rows": len(brands),
        "summary": summarize_brand_factors(factors_by_brand).to_json(),
    }


def verify_brand_factors(conn: Any, cache_table: str = CACHE_TABLE) -> dict[str, Any]:
    if not column_exists(conn, cache_table, "brand_factors"):
        raise CacheBrandFactorsError(f"{cache_table}.brand_factors is missing")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*) AS rows_total,
                   SUM(brand_factors IS NOT NULL) AS rows_with_factors,
                   SUM(brand_factors IS NOT NULL AND JSON_VALID(brand_factors)) AS rows_json_valid,
                   SUM(JSON_LENGTH(brand_factors, '$.atc') > 0) AS rows_with_atc,
                   SUM(JSON_LENGTH(brand_factors, '$.ubist.seller') > 0) AS rows_with_ubist_seller,
                   SUM(JSON_LENGTH(brand_factors, '$.iqvia.mfr_name_kor') > 0) AS rows_with_iqvia_mfr
            FROM {quote_ident(cache_table)}
            """
        )
        return dict(cur.fetchone())


def connect_db() -> Any:
    database = os.environ.get("MARIADB_DATABASE", TARGET_DATABASE)
    if database != TARGET_DATABASE:
        raise CacheBrandFactorsError(f"refusing to write non-d2 database: {database}")
    user = os.environ.get("D2_WRITER_USER") or os.environ.get("MARIADB_USER")
    password = os.environ.get("D2_WRITER_PASSWORD") or os.environ.get("MARIADB_PASSWORD")
    if user and password and os.environ.get("MARIADB_HOST"):
        return pymysql.connect(
            host=os.environ["MARIADB_HOST"],
            port=int(os.environ.get("MARIADB_PORT", "3306")),
            user=user,
            password=password,
            database=database,
            charset="utf8mb4",
            autocommit=False,
            cursorclass=pymysql.cursors.DictCursor,
        )
    return mariadb_connect()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Add and backfill cache_deep_analysis.brand_factors from mart dimensions.")
    parser.add_argument("--cache-table", default=CACHE_TABLE)
    parser.add_argument("--ensure-column", action="store_true")
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not any((args.ensure_column, args.backfill, args.verify, args.dry_run)):
        raise CacheBrandFactorsError("at least one action is required")
    conn = connect_db()
    result: dict[str, Any] = {"database": os.environ.get("MARIADB_DATABASE", TARGET_DATABASE)}
    try:
        if args.dry_run:
            brands = load_cached_brands(conn, args.cache_table)
            result["dry_run"] = summarize_brand_factors(load_brand_factor_map(conn, brands)).to_json()
        if args.ensure_column:
            result["ensure_column"] = {"added": ensure_brand_factors_column(conn, args.cache_table)}
            conn.commit()
        if args.backfill:
            result["backfill"] = backfill_brand_factors(conn, args.cache_table)
        if args.verify:
            result["verify"] = verify_brand_factors(conn, args.cache_table)
        print("CACHE_BRAND_FACTORS_JSON=" + json.dumps(result, ensure_ascii=False, default=str, sort_keys=True))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
