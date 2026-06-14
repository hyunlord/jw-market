from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import duckdb
import pandas as pd

from pipeline.etl.io.enrich.normalize import clean_scalar
from pipeline.etl.io.enrich.schema import ENRICHED_COLUMNS
from pipeline.etl.io.enrich.ubist_bridge import merge_parquet_sources, now_iso


@dataclass(frozen=True, slots=True)
class IqviaNsaStats:
    rows: int
    matched_products: int
    candidate_rows: int
    matched_raw_rows: int

    @property
    def raw_match_rate(self) -> float:
        if self.candidate_rows == 0:
            return 0.0
        return self.matched_raw_rows / self.candidate_rows


def target_iqvia_channels(ml_row: pd.Series) -> list[str]:
    targets: list[str] = []
    for col in ("target_iqvia_1", "target_iqvia_2", "target_iqvia_3"):
        value = clean_scalar(ml_row.get(col))
        if value:
            targets.append(value.upper())
    return targets


def catalog_atc_codes(metadata: dict[str, object], ml_id: str) -> list[str]:
    markets = metadata.get("markets")
    if isinstance(markets, dict):
        market = markets.get(ml_id)
    else:
        market = metadata.get(ml_id)
    if not isinstance(market, dict):
        return []
    values = market.get("atc_codes", [])
    if not isinstance(values, list):
        return []
    return [clean_scalar(v).upper() for v in values if clean_scalar(v)]


def _register_iqvia_products(con: duckdb.DuckDBPyConnection, products: pd.DataFrame) -> None:
    rows: list[dict[str, object]] = []
    for record in products.to_dict("records"):
        product_key = clean_scalar(record.get("iqvia_product_key") or record.get("product_key"))
        brand_key = clean_scalar(record.get("brand_key"))
        if product_key:
            rows.append(
                {
                    "product_id": record["product_id"],
                    "ml_id": record["ml_id"],
                    "match_key": product_key,
                    "brand_key": brand_key,
                    "match_method": "product_name_pack",
                    "match_confidence": "high",
                    "priority": 1,
                }
            )
        if brand_key:
            rows.append(
                {
                    "product_id": record["product_id"],
                    "ml_id": record["ml_id"],
                    "match_key": brand_key,
                    "brand_key": brand_key,
                    "match_method": "brand_only",
                    "match_confidence": "medium",
                    "priority": 3,
                }
            )
    bridge = pd.DataFrame(rows)
    if bridge.empty:
        bridge = pd.DataFrame(
            columns=[
                "product_id",
                "ml_id",
                "match_key",
                "brand_key",
                "match_method",
                "match_confidence",
                "priority",
            ]
        )
    con.register("iqvia_product_bridge", bridge.drop_duplicates())


def _values_csv(values: list[str]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)


def _iqvia_product_name_key_expr() -> str:
    return (
        "lower(regexp_replace("
        "replace(replace(replace(coalesce(cast(product_name as varchar), ''), '㎎', 'mg'), 'ＭＧ', 'mg'), ' ', ''), "
        "'\\\\s+', '', 'g'))"
    )


def _iqvia_product_full_key_expr() -> str:
    return (
        "lower(regexp_replace("
        "replace(replace(replace(concat(coalesce(cast(product_name as varchar), ''), ' ', "
        "coalesce(cast(pack_desc as varchar), '')), '㎎', 'mg'), 'ＭＧ', 'mg'), ' ', ''), "
        "'\\\\s+', '', 'g'))"
    )


def _iqvia_brand_key_expr() -> str:
    return (
        "trim(regexp_replace("
        "regexp_replace("
        "regexp_replace("
        "replace(replace(lower(coalesce(cast(product_name as varchar), '')), '㎎', 'mg'), 'ＭＧ', 'mg'), "
        "'\\\\([^)]*\\\\)', ' ', 'g'), "
        "'\\\\b[0-9]+(?:\\\\.[0-9]+)?\\\\s*/\\\\s*[0-9]+(?:\\\\.[0-9]+)?(?:\\\\s*/\\\\s*[0-9]+(?:\\\\.[0-9]+)?)?\\\\s*(?:mg|g|ml|iu|mcg)?\\\\b', ' ', 'gi'), "
        "'\\\\b[0-9]+(?:\\\\.[0-9]+)?\\\\s*(?:mg|g|ml|iu|mcg)\\\\b|필름코팅정|연질캡슐|장용정|서방정|복합정|프리필드펜|캡슐|정|주사|주|액|시럽', ' ', 'gi'))"
    )


def _candidate_where(targets: list[str], atc_codes: list[str]) -> str:
    clauses: list[str] = []
    if targets:
        clauses.append("regexp_extract(upper(coalesce(audit_code, '')), '^(KHPA|KCPA|KPA)', 1) IN (" + _values_csv(targets) + ")")
    if atc_codes:
        raw_codes = [clean_scalar(value).upper() for value in atc_codes]
        raw_atc = "upper(regexp_extract(coalesce(atc4_code, ''), '^([^_]+)', 1))"
        clauses.append(f"({raw_atc} = '' OR {raw_atc} IN (" + _values_csv(raw_codes) + "))")
    return " AND ".join(clauses) if clauses else "TRUE"


def _iqvia_match_sql(*, nsa_glob: str, targets: list[str], atc_codes: list[str], ingested_at: str | None) -> str:
    product_name_key = _iqvia_product_name_key_expr()
    product_full_key = _iqvia_product_full_key_expr()
    brand_key = _iqvia_brand_key_expr()
    where = _candidate_where(targets, atc_codes)
    ingested = now_iso(ingested_at).replace("'", "''")
    return (
        "WITH candidates AS ("
        "SELECT *, "
        f"{product_name_key} AS nsa_product_name_key, "
        f"{product_full_key} AS nsa_product_full_key, "
        f"{brand_key} AS nsa_brand_key, "
        "regexp_extract(upper(coalesce(audit_code, '')), '^(KHPA|KCPA|KPA)', 1) AS channel_key "
        f"FROM read_parquet('{nsa_glob}') "
        f"WHERE {where}"
        "), exact_matches AS ("
        "SELECT c.*, b.ml_id AS matched_ml_id, b.product_id, b.match_method, b.match_confidence, b.priority "
        "FROM candidates c JOIN iqvia_product_bridge b ON b.match_key IN (c.nsa_product_name_key, c.nsa_product_full_key) "
        "WHERE b.priority = 1"
        "), contains_matches AS ("
        "SELECT c.*, b.ml_id AS matched_ml_id, b.product_id, 'product_name_pack_contains' AS match_method, 'high' AS match_confidence, 2 AS priority "
        "FROM candidates c JOIN iqvia_product_bridge b "
        "ON b.priority = 1 AND length(b.match_key) >= 3 "
        "AND b.match_key NOT IN (c.nsa_product_name_key, c.nsa_product_full_key, '') "
        "AND (contains(c.nsa_product_full_key, b.match_key) OR contains(b.match_key, c.nsa_product_full_key))"
        "), brand_matches AS ("
        "SELECT c.*, b.ml_id AS matched_ml_id, b.product_id, b.match_method, b.match_confidence, b.priority "
        "FROM candidates c JOIN iqvia_product_bridge b "
        "ON b.priority = 3 AND length(b.match_key) >= 2 "
        "AND (b.match_key = c.nsa_product_name_key OR contains(c.nsa_product_name_key, b.match_key) OR "
        "contains(c.nsa_product_full_key, b.match_key) OR contains(c.nsa_brand_key, b.match_key))"
        "), ranked AS ("
        "SELECT *, row_number() OVER (PARTITION BY source_file, sheet_name, source_row_no, audit_code, period_label, product_id "
        "ORDER BY priority, match_method) AS rn "
        "FROM (SELECT * FROM exact_matches UNION ALL SELECT * FROM contains_matches UNION ALL SELECT * FROM brand_matches)"
        ") "
        "SELECT "
        "matched_ml_id AS ml_id, product_id, 'nsa' AS source, period_label AS period_yyyymm, "
        "try_cast(replace(values_lc, ',', '') AS DOUBLE) AS raw_rx_amt, "
        "try_cast(replace(counting_units, ',', '') AS DOUBLE) AS raw_rx_cnt, "
        "coalesce(try_cast(replace(units, ',', '') AS DOUBLE), try_cast(replace(dosage_units, ',', '') AS DOUBLE)) AS raw_rx_qty, "
        "try_cast(replace(values_lc, ',', '') AS DOUBLE) AS canonical_value, "
        "CASE WHEN channel_key = '' THEN 'Unknown' ELSE channel_key END AS channel, "
        "'' AS specialty, match_method, match_confidence, 'nsa_canonical_parquet' AS source_table, "
        "concat('nsa::', coalesce(cast(source_file AS varchar), ''), '::', coalesce(cast(sheet_name AS varchar), ''), "
        "'::', coalesce(cast(source_row_no AS varchar), ''), '::', coalesce(cast(audit_code AS varchar), ''), "
        "'::', coalesce(cast(period_label AS varchar), '')) AS source_row_id, "
        f"'{ingested}' AS ingested_at "
        "FROM ranked WHERE rn = 1"
    )


def write_iqvia_nsa_ml(
    products: pd.DataFrame,
    metadata: dict[str, object],
    ml_id: str,
    ml_row: pd.Series,
    output_path: Path,
    *,
    nsa_glob: str,
    ingested_at: str | None = None,
) -> IqviaNsaStats:
    con = duckdb.connect()
    _register_iqvia_products(con, products)
    targets = target_iqvia_channels(ml_row)
    atc_codes = catalog_atc_codes(metadata, ml_id)
    where = _candidate_where(targets, atc_codes)
    candidate_rows = int(con.execute(f"SELECT COUNT(*) FROM read_parquet('{nsa_glob}') WHERE {where}").fetchone()[0] or 0)
    sql = _iqvia_match_sql(nsa_glob=nsa_glob, targets=targets, atc_codes=atc_codes, ingested_at=ingested_at)
    frame = con.execute(sql).fetchdf()
    con.close()
    merge_parquet_sources(output_path, [frame])
    rows = int(len(frame))
    matched_products = int(frame["product_id"].nunique() if not frame.empty else 0)
    matched_raw_rows = int(frame["source_row_id"].nunique() if not frame.empty else 0)
    return IqviaNsaStats(
        rows=rows,
        matched_products=matched_products,
        candidate_rows=candidate_rows,
        matched_raw_rows=matched_raw_rows,
    )
