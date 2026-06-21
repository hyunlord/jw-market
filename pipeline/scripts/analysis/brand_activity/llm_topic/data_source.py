from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path
import hashlib
import os

import pymysql

from .models import BrandDescription, JsonValue, KeywordRow
from .privacy import estimate_tokens


REPO_ROOT = Path(__file__).resolve().parents[5]
ENV_PATH = REPO_ROOT / "pipeline/docker/.env"
ALIAS_PATH = REPO_ROOT / "docs/design/brand_activity/alias/ALIAS_01_MAPPING.json"
DICTIONARY_PATH = REPO_ROOT / "docs/research/brand_activity/topic_redesign/REDESIGN_03_DICTIONARY_DRAFT.json"
SCHEMA = "jw_brand_activity_stage"
KEYWORD_TABLE = "km_keyword_event_stage"


def read_env_file(path: Path = ENV_PATH) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        values[key] = raw_value.strip().strip('"').strip("'")
    return values


def connect_mariadb(env: dict[str, str]) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=os.environ.get("MARIADB_HOST", "127.0.0.1"),
        port=int(os.environ.get("MARIADB_PORT", env.get("HOST_PORT", "3308"))),
        user=os.environ.get("MARIADB_USER", "root"),
        password=os.environ.get("MARIADB_ROOT_PASSWORD", env["MARIADB_ROOT_PASSWORD"]),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def fetch_snapshot(connection: pymysql.connections.Connection) -> dict[str, JsonValue]:
    with connection.cursor() as cursor:
        cursor.execute("START TRANSACTION READ ONLY")
        cursor.execute(f"SELECT COUNT(*) AS row_count FROM {SCHEMA}.{KEYWORD_TABLE}")
        row_count = int(cursor.fetchone()["row_count"])
        cursor.execute(f"SELECT stage_row_sha256 FROM {SCHEMA}.{KEYWORD_TABLE} ORDER BY id")
        digest = hashlib.sha256()
        for row in cursor.fetchall():
            digest.update(str(row["stage_row_sha256"]).encode("utf-8"))
            digest.update(b"\n")
        cursor.execute("COMMIT")
    return {"row_count": row_count, "stage_hash_fingerprint": digest.hexdigest()}


def fetch_keyword_rows(connection: pymysql.connections.Connection, markets: Sequence[str]) -> list[KeywordRow]:
    placeholders = ",".join(["%s"] * len(markets))
    sql = (
        "SELECT id, period_ym, visit_location, specialty, product_name, therapeutic_class, "
        "keyword_text, interest, prescription_frequency, prescription_evolution, "
        "abstract_lit, patient_lit, promotional_lit, stage_row_sha256 "
        f"FROM {SCHEMA}.{KEYWORD_TABLE} "
        f"WHERE keyword_text <> '' AND therapeutic_class IN ({placeholders}) "
        "ORDER BY therapeutic_class, product_name, period_ym, id"
    )
    rows: list[KeywordRow] = []
    with connection.cursor() as cursor:
        cursor.execute("START TRANSACTION READ ONLY")
        cursor.execute(sql, tuple(markets))
        for record in cursor.fetchall():
            rows.append(
                KeywordRow(
                    row_id=int(record["id"]),
                    period_ym=str(record["period_ym"] or ""),
                    atc4=str(record["therapeutic_class"] or ""),
                    brand=str(record["product_name"] or ""),
                    keyword_text=" ".join(str(record["keyword_text"] or "").split()),
                    interest=str(record["interest"] or ""),
                    prescription_frequency=str(record["prescription_frequency"] or ""),
                    prescription_evolution=str(record["prescription_evolution"] or ""),
                    promotional_lit=str(record["promotional_lit"] or ""),
                    abstract_lit=str(record["abstract_lit"] or ""),
                    patient_lit=str(record["patient_lit"] or ""),
                    specialty=str(record["specialty"] or ""),
                    visit_location=str(record["visit_location"] or ""),
                    stage_row_sha256=str(record["stage_row_sha256"] or ""),
                )
            )
        cursor.execute("COMMIT")
    return rows


def market_stats(rows: Sequence[KeywordRow]) -> dict[str, dict[str, JsonValue]]:
    grouped: defaultdict[str, list[KeywordRow]] = defaultdict(list)
    for row in rows:
        grouped[row.atc4].append(row)
    return {
        market: {
            "row_count": len(items),
            "brand_count": len({item.brand for item in items}),
            "estimated_tokens": sum(estimate_tokens(item.keyword_text) for item in items),
            "brands": sorted({item.brand for item in items}),
        }
        for market, items in sorted(grouped.items())
    }


def brand_stats(rows: Sequence[KeywordRow]) -> list[dict[str, JsonValue]]:
    grouped: defaultdict[tuple[str, str], list[KeywordRow]] = defaultdict(list)
    for row in rows:
        grouped[(row.atc4, row.brand)].append(row)
    return [
        {
            "atc4": atc4,
            "brand": brand,
            "row_count": len(items),
            "estimated_tokens": sum(estimate_tokens(item.keyword_text) for item in items),
        }
        for (atc4, brand), items in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))
    ]


def rows_for_market(rows: Sequence[KeywordRow], market: str) -> list[KeywordRow]:
    return [row for row in rows if row.atc4 == market]


def rows_for_brand(rows: Sequence[KeywordRow], market: str, brand: str) -> list[KeywordRow]:
    return [row for row in rows if row.atc4 == market and row.brand == brand]


def deterministic_sample(rows: Sequence[KeywordRow], *, limit: int, seed: str) -> list[KeywordRow]:
    ranked = sorted(rows, key=lambda row: hashlib.sha256(f"{seed}:{row.stage_row_sha256}".encode("utf-8")).hexdigest())
    return sorted(ranked[:limit], key=lambda row: row.row_id)


def stratified_market_sample(rows: Sequence[KeywordRow], *, brands: Sequence[str], per_brand: int, seed: str) -> list[KeywordRow]:
    sampled: list[KeywordRow] = []
    for brand in brands:
        sampled.extend(deterministic_sample([row for row in rows if row.brand == brand], limit=per_brand, seed=f"{seed}:{brand}"))
    return sorted(sampled, key=lambda row: (row.brand, row.row_id))


def load_alias_descriptions(alias_payload: dict[str, JsonValue], brands: Iterable[tuple[str, str]]) -> dict[str, BrandDescription]:
    records = alias_payload.get("records")
    if not isinstance(records, list):
        return {}
    by_brand: dict[str, dict[str, JsonValue]] = {}
    for item in records:
        if isinstance(item, dict) and isinstance(item.get("iqvia_en"), str):
            by_brand[str(item["iqvia_en"])] = item
    result: dict[str, BrandDescription] = {}
    for atc4, brand in brands:
        record = by_brand.get(brand, {})
        result[f"{atc4}:{brand}"] = BrandDescription(
            brand=brand,
            atc4=atc4,
            kr_canonical=_optional_str(record.get("kr_canonical")),
            molecule=_string_tuple(record.get("molecule")),
            is_jw=bool(record.get("is_jw")),
            manufacturer=_string_tuple(record.get("manufacturer")),
            representing_company=_string_tuple(record.get("representing_company")),
        )
    return result


def _string_tuple(value: JsonValue) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if isinstance(item, str))


def _optional_str(value: JsonValue) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None
