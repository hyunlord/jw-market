"""Build strategic-view filter dimensions from recoded strategic mart rows.

The general-view dimension sidecar is intentionally not reused here.  General
metrics are raw ATC4/product aggregates, while strategic metrics already carry
MI Master overlays, exclusions, and recodes.  This module reads the strategic
mart rows as the metric source of truth and emits a separate product-level
sidecar for filter UI options and future strategic dynamic aggregation.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import re
from typing import Literal

from pymysql.connections import Connection

from pipeline.etl.io.mart.general_config import mariadb_connect


STRATEGIC_DIMENSION_TABLE = "mart_strategic_filter_dimension_metric"
LOAD_BATCH_SIZE = 200
EMPTY_DIMENSION_VALUES = {"", "nan", "none", "null", "<na>", "n/a", "na", "-"}
UBIST_DIMENSION_FIELDS: dict[str, tuple[str, ...]] = {
    "atc4": ("atc4_code", "atc4"),
    "atc3": ("atc4_code", "atc4"),
    "seller": ("company", "manufacturer", "raw_company", "판매사", "제조사"),
    "molecule_strength": ("strength_pack", "성분용량"),
    "form": ("dosage_form", "제형"),
    "route": ("route", "투여경로"),
    "reimbursement": ("nhi_type", "nhi", "급여구분"),
}
IQVIA_DIMENSION_FIELDS: dict[str, tuple[str, ...]] = {
    "mfr": ("company", "manufacturer", "raw_company", "MFR NAME KOR", "제조사"),
    "molecule_type": ("molecule_type",),
    "molecule_desc": ("molecule", "molecule_desc", "MOLECULE DESC"),
    "strength": ("strength_pack", "strength", "STRENGTH"),
    "nhi": ("nhi_type", "NHI TYPE"),
}


JsonObject = dict[str, object]
MarketKind = Literal["ml", "cd"]


@dataclass(frozen=True, slots=True)
class StrategicMetricSourceRow:
    market_kind: MarketKind
    market_id: str
    brand_id: str
    brand_key: str
    brand_name: str
    source: str
    measure: str
    unit_label: str
    raw_value_history: str
    by_dimension: str
    dimension_data: str
    overlay_data: str
    cd_overlay: str | None


@dataclass(frozen=True, slots=True)
class StrategicDimensionMetricRow:
    market_kind: MarketKind
    market_id: str
    brand_id: str
    brand_key: str
    brand_name: str
    source: str
    measure: str
    unit_label: str
    product_code: str
    product_name: str
    dimension_type: str
    dimension_value: str
    dimension_value_norm: str
    dimension_value_hash: str
    raw_value_history: dict[str, float]


def normalize_dimension_value(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text.casefold()


def clean_dimension_value(value: object) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if normalize_dimension_value(text) in EMPTY_DIMENSION_VALUES:
        return None
    return text


def dimension_hash(value_norm: str) -> str:
    return hashlib.sha256(value_norm.encode("utf-8")).hexdigest()


def parse_json_object(raw: str | None) -> JsonObject:
    if not raw:
        return {}
    payload = json.loads(raw)
    return payload if isinstance(payload, dict) else {}


def history_from_payload(payload: object) -> dict[str, float]:
    if not isinstance(payload, Mapping):
        return {}
    history: dict[str, float] = {}
    for period, item in payload.items():
        value: object = item
        if isinstance(item, Mapping):
            value = item.get("raw_value") or item.get("value") or item.get("sales")
        try:
            history[str(period)] = float(value or 0.0)
        except (TypeError, ValueError):
            history[str(period)] = 0.0
    return history


def extract_dimension_metric_rows(
    row: StrategicMetricSourceRow,
    *,
    molecule_type_by_product: Mapping[str, str],
) -> tuple[StrategicDimensionMetricRow, ...]:
    """Explode one recoded strategic mart row into sidecar rows.

    Product histories are preferred because future filters must not pull in a
    brand's unrelated products.  When a strategic row has multiple labels for a
    dimension but does not retain product-to-label mapping, we emit one
    synthetic product row per dimension label using ``dimension_data``'s recoded
    aggregate history.  That preserves totals without over-assigning every
    product to every label.
    """

    by_dimension = parse_json_object(row.by_dimension)
    dimension_data = parse_json_object(row.dimension_data)
    overlay_data = parse_json_object(row.overlay_data)
    cd_overlay = parse_json_object(row.cd_overlay)
    products = tuple(_product_items(by_dimension, row.raw_value_history))
    rows: list[StrategicDimensionMetricRow] = []
    dimension_fields = IQVIA_DIMENSION_FIELDS if row.source == "iqvia_nsa" else UBIST_DIMENSION_FIELDS
    label_sources = (by_dimension, overlay_data, cd_overlay.get("override_columns") or {}, cd_overlay.get("filter") or {})

    for dimension_type, fields in dimension_fields.items():
        if dimension_type == "molecule_type":
            rows.extend(_molecule_type_rows(row, products, molecule_type_by_product))
            continue
        dimension_history = _dimension_history(dimension_data, fields)
        if len(dimension_history) > 1:
            rows.extend(_synthetic_dimension_rows(row, dimension_type, dimension_history))
            continue
        label = _single_history_label(dimension_history) or _first_label(label_sources, fields)
        if dimension_type == "atc3":
            label = _atc3_from_atc4(label)
        if not label:
            continue
        rows.extend(_product_dimension_rows(row, products, dimension_type, label))
    return tuple(rows)


def _product_items(by_dimension: Mapping[str, object], fallback_history: str) -> tuple[tuple[str, str, dict[str, float]], ...]:
    products = by_dimension.get("products")
    result: list[tuple[str, str, dict[str, float]]] = []
    if isinstance(products, Sequence) and not isinstance(products, (str, bytes)):
        for index, product in enumerate(products, start=1):
            if not isinstance(product, Mapping):
                continue
            code = clean_dimension_value(product.get("product_code")) or f"__product__:{index}"
            name = clean_dimension_value(product.get("product_name")) or code
            history = history_from_payload(product.get("raw_value_history"))
            if history:
                result.append((code, name, history))
    if result:
        return tuple(result)
    return (("__row_total__", "__row_total__", history_from_payload(parse_json_object(fallback_history))),)


def _dimension_history(dimension_data: Mapping[str, object], fields: Iterable[str]) -> dict[str, dict[str, float]]:
    for field in fields:
        bucket = dimension_data.get(field)
        if not isinstance(bucket, Mapping):
            continue
        result: dict[str, dict[str, float]] = {}
        for raw_label, raw_history in bucket.items():
            label = clean_dimension_value(raw_label)
            if not label:
                continue
            history = history_from_payload(raw_history)
            if history:
                result[label] = history
        if result:
            return result
    return {}


def _single_history_label(dimension_history: Mapping[str, dict[str, float]]) -> str | None:
    if len(dimension_history) != 1:
        return None
    return next(iter(dimension_history))


def _first_label(sources: Iterable[object], fields: Iterable[str]) -> str | None:
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for field in fields:
            label = clean_dimension_value(source.get(field))
            if label:
                return label
    return None


def _atc3_from_atc4(value: object) -> str | None:
    label = clean_dimension_value(value)
    if not label:
        return None
    return label[:4].upper()


def _product_dimension_rows(
    row: StrategicMetricSourceRow,
    products: Sequence[tuple[str, str, dict[str, float]]],
    dimension_type: str,
    dimension_value: str,
) -> tuple[StrategicDimensionMetricRow, ...]:
    return tuple(
        _make_row(row, product_code=code, product_name=name, dimension_type=dimension_type, dimension_value=dimension_value, history=history)
        for code, name, history in products
        if history
    )


def _synthetic_dimension_rows(
    row: StrategicMetricSourceRow,
    dimension_type: str,
    dimension_history: Mapping[str, dict[str, float]],
) -> tuple[StrategicDimensionMetricRow, ...]:
    rows: list[StrategicDimensionMetricRow] = []
    for dimension_value, history in dimension_history.items():
        norm = normalize_dimension_value(dimension_value)
        rows.append(
            _make_row(
                row,
                product_code=f"__dimension__:{dimension_type}:{dimension_hash(norm)[:16]}",
                product_name=f"__dimension__:{dimension_type}",
                dimension_type=dimension_type,
                dimension_value=dimension_value,
                history=history,
            )
        )
    return tuple(rows)


def _molecule_type_rows(
    row: StrategicMetricSourceRow,
    products: Sequence[tuple[str, str, dict[str, float]]],
    molecule_type_by_product: Mapping[str, str],
) -> tuple[StrategicDimensionMetricRow, ...]:
    rows: list[StrategicDimensionMetricRow] = []
    for code, name, history in products:
        label = clean_dimension_value(molecule_type_by_product.get(code) or molecule_type_by_product.get(name))
        if label and history:
            rows.append(_make_row(row, product_code=code, product_name=name, dimension_type="molecule_type", dimension_value=label, history=history))
    return tuple(rows)


def _make_row(
    row: StrategicMetricSourceRow,
    *,
    product_code: str,
    product_name: str,
    dimension_type: str,
    dimension_value: str,
    history: dict[str, float],
) -> StrategicDimensionMetricRow:
    norm = normalize_dimension_value(dimension_value)
    return StrategicDimensionMetricRow(
        market_kind=row.market_kind,
        market_id=row.market_id,
        brand_id=row.brand_id,
        brand_key=row.brand_key,
        brand_name=row.brand_name,
        source=row.source,
        measure=row.measure,
        unit_label=row.unit_label,
        product_code=product_code,
        product_name=product_name,
        dimension_type=dimension_type,
        dimension_value=dimension_value,
        dimension_value_norm=norm,
        dimension_value_hash=dimension_hash(norm),
        raw_value_history=history,
    )


def build_strategic_sidecar(
    *,
    source_db: str,
    target_db: str,
    connection: Connection | None = None,
    replace_table: bool = False,
) -> dict[str, object]:
    owns_connection = connection is None
    conn = connection or mariadb_connect()
    try:
        _ensure_target_schema(conn, target_db)
        _ensure_table(conn, target_db, replace_table=replace_table)
        molecule_type_by_product = _load_iqvia_molecule_type_map(conn, source_db)
        inserted = 0
        counts: dict[str, int] = {}
        for source_row in _iter_source_rows(conn, source_db):
            metric_rows = extract_dimension_metric_rows(source_row, molecule_type_by_product=molecule_type_by_product)
            inserted += _insert_rows(conn, target_db, metric_rows)
            key = f"{source_row.market_kind}:{source_row.source}"
            counts[key] = counts.get(key, 0) + len(metric_rows)
        return {
            "source_db": source_db,
            "target_db": target_db,
            "table": STRATEGIC_DIMENSION_TABLE,
            "rows_inserted": inserted,
            "counts": counts,
            "built_at": datetime.now(UTC).isoformat(),
        }
    finally:
        if owns_connection:
            conn.close()


def _ensure_target_schema(conn: Connection, target_db: str) -> None:
    with conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{target_db}` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")


def _ensure_table(conn: Connection, target_db: str, *, replace_table: bool) -> None:
    with conn.cursor() as cur:
        if replace_table:
            cur.execute(f"DROP TABLE IF EXISTS `{target_db}`.`{STRATEGIC_DIMENSION_TABLE}`")
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{target_db}`.`{STRATEGIC_DIMENSION_TABLE}` (
              id BIGINT AUTO_INCREMENT PRIMARY KEY,
              market_kind VARCHAR(8) NOT NULL,
              market_id VARCHAR(32) NOT NULL,
              brand_id VARCHAR(255) NOT NULL,
              brand_key VARCHAR(255) NOT NULL,
              brand_name VARCHAR(255) NOT NULL,
              source VARCHAR(16) NOT NULL,
              measure VARCHAR(32) NOT NULL,
              unit_label VARCHAR(32) NULL,
              product_code VARCHAR(255) NOT NULL,
              product_name VARCHAR(512) NULL,
              dimension_type VARCHAR(64) NOT NULL,
              dimension_value TEXT NOT NULL,
              dimension_value_norm TEXT NOT NULL,
              dimension_value_hash CHAR(64) NOT NULL,
              raw_value_history LONGTEXT NOT NULL,
              computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
              KEY idx_scope (market_kind, market_id, source, measure, dimension_type, dimension_value_hash),
              KEY idx_brand (market_kind, market_id, brand_id, source, measure),
              KEY idx_options (source, dimension_type, dimension_value_hash),
              KEY idx_product (source, measure, product_code)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )


def _iter_source_rows(conn: Connection, source_db: str) -> Iterator[StrategicMetricSourceRow]:
    queries = (
        (
            "ml",
            f"""
            SELECT 'ml' AS market_kind, ml_id AS market_id, brand_id, brand_key, brand_name, source, measure,
                   unit_label, raw_value_history, by_dimension, dimension_data, overlay_data, NULL AS cd_overlay
            FROM `{source_db}`.mart_strategic_ml_brand_metric
            """,
        ),
        (
            "cd",
            f"""
            SELECT 'cd' AS market_kind, cd_market_id AS market_id, cd_brand_id AS brand_id, brand_key, brand_name,
                   source, measure, unit_label, raw_value_history, by_dimension, dimension_data, overlay_data, cd_overlay
            FROM `{source_db}`.mart_strategic_cd_brand_metric
            """,
        ),
    )
    with conn.cursor() as cur:
        for _, sql in queries:
            cur.execute(sql)
            for raw in cur.fetchall():
                yield StrategicMetricSourceRow(
                    market_kind=raw["market_kind"],
                    market_id=str(raw["market_id"]),
                    brand_id=str(raw["brand_id"]),
                    brand_key=str(raw["brand_key"]),
                    brand_name=str(raw["brand_name"]),
                    source=str(raw["source"]),
                    measure=str(raw["measure"]),
                    unit_label=str(raw.get("unit_label") or ""),
                    raw_value_history=str(raw.get("raw_value_history") or "{}"),
                    by_dimension=str(raw.get("by_dimension") or "{}"),
                    dimension_data=str(raw.get("dimension_data") or "{}"),
                    overlay_data=str(raw.get("overlay_data") or "{}"),
                    cd_overlay=str(raw["cd_overlay"]) if raw.get("cd_overlay") else None,
                )


def _load_iqvia_molecule_type_map(conn: Connection, source_db: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    with conn.cursor() as cur:
        cur.execute(f"SELECT payload FROM `{source_db}`.iqvia_nsa_quarterly_raw")
        for row in cur.fetchall():
            payload = parse_json_object(str(row.get("payload") or "{}"))
            static = payload.get("static") if isinstance(payload.get("static"), Mapping) else {}
            label = clean_dimension_value(static.get("MOLECULE TYPE") if isinstance(static, Mapping) else None)
            if not label or not isinstance(static, Mapping):
                continue
            for key in (static.get("PRODUCT NAME"), static.get("PRODUCT NAME KOR")):
                product_key = clean_dimension_value(key)
                if product_key:
                    mapping.setdefault(product_key, label)
    return mapping


def _insert_rows(conn: Connection, target_db: str, rows: Sequence[StrategicDimensionMetricRow]) -> int:
    if not rows:
        return 0
    sql = f"""
        INSERT INTO `{target_db}`.`{STRATEGIC_DIMENSION_TABLE}` (
          market_kind, market_id, brand_id, brand_key, brand_name, source, measure, unit_label,
          product_code, product_name, dimension_type, dimension_value, dimension_value_norm,
          dimension_value_hash, raw_value_history
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    payloads = [
        (
            row.market_kind,
            row.market_id,
            row.brand_id,
            row.brand_key,
            row.brand_name,
            row.source,
            row.measure,
            row.unit_label,
            row.product_code,
            row.product_name,
            row.dimension_type,
            row.dimension_value,
            row.dimension_value_norm,
            row.dimension_value_hash,
            json.dumps(row.raw_value_history, ensure_ascii=False, separators=(",", ":")),
        )
        for row in rows
    ]
    with conn.cursor() as cur:
        for start in range(0, len(payloads), LOAD_BATCH_SIZE):
            cur.executemany(sql, payloads[start : start + LOAD_BATCH_SIZE])
    return len(payloads)
