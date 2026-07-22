from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path
import hashlib
import json
import os

import pymysql

from .models import BrandDescription, JsonValue, KeywordRow
from .privacy import estimate_tokens


REPO_ROOT = Path(__file__).resolve().parents[5]
ENV_PATH = REPO_ROOT / "pipeline/docker/.env"
SCHEMA = "jw_brand_activity_stage"
KEYWORD_TABLE = "km_keyword_event_stage"
CSD_TABLE = "csd_channel_dynamics_stage"
PRIMARY_ALIAS_PATH = REPO_ROOT / "docs/research/brand_activity/alias/ALIAS_01_MAPPING.json"
FALLBACK_ALIAS_PATH = REPO_ROOT / "docs/design/brand_activity/alias/ALIAS_01_MAPPING.json"
DICTIONARY_PATH = REPO_ROOT / "docs/research/brand_activity/topic_redesign/REDESIGN_03_DICTIONARY_DRAFT.json"
JW_COMPANY_PREFIX = "JW"


class MissingMariaDbPasswordError(RuntimeError):
    """Raised when neither environment variables nor .env provide a MariaDB password."""


def read_env_file(path: Path = ENV_PATH) -> dict[str, str]:
    """Read local MariaDB credentials without printing or exporting them."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, value = stripped.split("=", 1)
            values[key] = value.strip().strip('"').strip("'")
    return values


def connect_mariadb(env: dict[str, str] | None = None) -> pymysql.connections.Connection:
    """Open the MariaDB connection from env vars first, with optional .env fallback."""
    env_values = env or {}
    return pymysql.connect(
        host=os.environ.get("MARIADB_HOST", "127.0.0.1"),
        port=int(os.environ.get("MARIADB_PORT", env_values.get("HOST_PORT", "3308"))),
        user=os.environ.get("MARIADB_USER", "root"),
        password=_mariadb_password(env_values),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def _mariadb_password(env: dict[str, str]) -> str:
    """Resolve the MariaDB password without requiring a local .env file."""
    password = os.environ.get("MARIADB_ROOT_PASSWORD") or env.get("MARIADB_ROOT_PASSWORD")
    if not password:
        raise MissingMariaDbPasswordError("MARIADB_ROOT_PASSWORD not set in environment or .env")
    return password


def fetch_snapshot(connection: pymysql.connections.Connection, *, schema: str = SCHEMA) -> dict[str, JsonValue]:
    """Fingerprint Keyword stage row count and hashes before or after analysis."""
    with connection.cursor() as cursor:
        cursor.execute("START TRANSACTION READ ONLY")
        cursor.execute(f"SELECT COUNT(*) AS row_count FROM {schema}.{KEYWORD_TABLE}")
        row_count = int(cursor.fetchone()["row_count"])
        cursor.execute(f"SELECT stage_row_sha256 FROM {schema}.{KEYWORD_TABLE} ORDER BY id")
        digest = hashlib.sha256()
        for row in cursor.fetchall():
            digest.update(str(row["stage_row_sha256"]).encode("utf-8"))
            digest.update(b"\n")
        cursor.execute("COMMIT")
    return {"row_count": row_count, "stage_hash_fingerprint": digest.hexdigest()}


def fetch_keyword_rows(connection: pymysql.connections.Connection, markets: Sequence[str], *, schema: str = SCHEMA) -> list[KeywordRow]:
    """Fetch Keyword rows for selected ATCs inside a read-only transaction."""
    placeholders = ",".join(["%s"] * len(markets))
    sql = (
        "SELECT id, period_ym, visit_location, specialty, product_name, therapeutic_class, "
        "keyword_text, interest, prescription_frequency, prescription_evolution, "
        "abstract_lit, patient_lit, promotional_lit, stage_row_sha256, representing_company "
        f"FROM {schema}.{KEYWORD_TABLE} "
        f"WHERE keyword_text <> '' AND therapeutic_class IN ({placeholders}) "
        "ORDER BY therapeutic_class, product_name, period_ym, id"
    )
    rows: list[KeywordRow] = []
    with connection.cursor() as cursor:
        cursor.execute("START TRANSACTION READ ONLY")
        cursor.execute(sql, tuple(markets))
        for record in cursor.fetchall():
            rows.append(_keyword_row(record))
        cursor.execute("COMMIT")
    return rows


def fetch_keyword_atc4(connection: pymysql.connections.Connection, *, schema: str = SCHEMA) -> tuple[str, ...]:
    """Return every ATC4 with non-empty keyword data from the current stage."""
    with connection.cursor() as cursor:
        cursor.execute("START TRANSACTION READ ONLY")
        cursor.execute(
            f"SELECT DISTINCT therapeutic_class AS atc4 FROM {schema}.{KEYWORD_TABLE} "
            "WHERE keyword_text <> '' ORDER BY therapeutic_class"
        )
        markets = tuple(str(row["atc4"]) for row in cursor.fetchall())
        cursor.execute("COMMIT")
    return markets


def fetch_topic_covered_atc4(connection: pymysql.connections.Connection, *, schema: str = SCHEMA) -> tuple[str, ...]:
    """Return ATC4 values already represented by persisted topic scopes."""
    with connection.cursor() as cursor:
        cursor.execute("START TRANSACTION READ ONLY")
        cursor.execute(f"SELECT atc4_values FROM {schema}.mart_brand_activity_topics ORDER BY scope_id")
        records = cursor.fetchall()
        cursor.execute("COMMIT")
    covered: set[str] = set()
    for record in records:
        raw = record["atc4_values"]
        values = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(values, list):
            covered.update(str(value) for value in values if isinstance(value, str) and value)
    return tuple(sorted(covered))


def fetch_csd_market_bridge(connection: pymysql.connections.Connection, markets: Sequence[str], *, schema: str = SCHEMA) -> dict[str, JsonValue]:
    """Derive ATC4 to CSD English market names from exact Keyword/CSD product overlap."""
    placeholders = ",".join(["%s"] * len(markets))
    bridge: dict[str, dict[str, JsonValue]] = {
        market: {
            "atc4": market,
            "keyword_brands": 0,
            "exact_matched_brands": 0,
            "csd_market": None,
            "csd_market_candidates": [],
            "csd_market_missing": True,
            "source": "exact_product_overlap",
        }
        for market in markets
    }
    all_csd_markets: list[str] = []
    with connection.cursor() as cursor:
        cursor.execute("START TRANSACTION READ ONLY")
        cursor.execute(
            (
                "SELECT DISTINCT market "
                f"FROM {schema}.{CSD_TABLE} "
                "WHERE market IS NOT NULL AND market <> '' AND market <> 'LIVALOZET Market2' "
                "ORDER BY market"
            )
        )
        all_csd_markets = [str(row["market"]) for row in cursor.fetchall()]
        cursor.execute(
            (
                "SELECT therapeutic_class AS atc4, COUNT(DISTINCT product_name) AS keyword_brands "
                f"FROM {schema}.{KEYWORD_TABLE} "
                f"WHERE keyword_text <> '' AND therapeutic_class IN ({placeholders}) "
                "GROUP BY therapeutic_class"
            ),
            tuple(markets),
        )
        for row in cursor.fetchall():
            atc4 = str(row["atc4"])
            bridge.setdefault(atc4, {})["keyword_brands"] = int(row["keyword_brands"])
        cursor.execute(
            (
                "SELECT k.therapeutic_class AS atc4, c.market, COUNT(DISTINCT k.product_name) AS exact_brand_overlap "
                f"FROM {schema}.{KEYWORD_TABLE} k "
                f"JOIN {schema}.{CSD_TABLE} c ON c.master_product = k.product_name "
                f"WHERE k.keyword_text <> '' AND k.therapeutic_class IN ({placeholders}) "
                "AND c.market <> 'LIVALOZET Market2' "
                "GROUP BY k.therapeutic_class, c.market "
                "ORDER BY k.therapeutic_class, exact_brand_overlap DESC, c.market"
            ),
            tuple(markets),
        )
        overlaps = cursor.fetchall()
        cursor.execute("COMMIT")
    by_atc4: defaultdict[str, list[dict[str, JsonValue]]] = defaultdict(list)
    for row in overlaps:
        by_atc4[str(row["atc4"])].append({"market": str(row["market"]), "exact_brand_overlap": int(row["exact_brand_overlap"])})
    for atc4, candidates in by_atc4.items():
        item = bridge.setdefault(atc4, {"atc4": atc4})
        best = candidates[0]
        item["csd_market"] = best["market"]
        item["exact_matched_brands"] = sum(int(candidate["exact_brand_overlap"]) for candidate in candidates)
        item["csd_market_candidates"] = candidates
        item["csd_market_missing"] = False
        item["multiple_csd_market_candidates"] = len(candidates) > 1
    missing = sorted(atc4 for atc4, row in bridge.items() if row.get("csd_market_missing"))
    return {
        "source_table": f"{schema}.{CSD_TABLE}",
        "join_rule": "km_keyword_event_stage.product_name = csd_channel_dynamics_stage.master_product exact match",
        "atc4_map": bridge,
        "csd_market_missing_atc4": missing,
        "all_csd_markets": all_csd_markets,
        "excluded_csd_markets": ["LIVALOZET Market2"],
    }


def load_json_file(path: Path) -> dict[str, JsonValue]:
    """Load one JSON object from disk with a defensive object check."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def resolve_alias_source() -> tuple[Path | None, dict[str, JsonValue]]:
    """Resolve the requested alias path, falling back to the existing design artifact if needed."""
    if PRIMARY_ALIAS_PATH.exists():
        return PRIMARY_ALIAS_PATH, {"status": "primary_found", "path": str(PRIMARY_ALIAS_PATH)}
    if FALLBACK_ALIAS_PATH.exists():
        return FALLBACK_ALIAS_PATH, {"status": "fallback_found", "path": str(FALLBACK_ALIAS_PATH), "requested_missing": str(PRIMARY_ALIAS_PATH)}
    return None, {"status": "missing", "path": str(PRIMARY_ALIAS_PATH), "fallback": str(FALLBACK_ALIAS_PATH)}


def resolve_dictionary_source() -> tuple[Path | None, dict[str, JsonValue]]:
    """Resolve the optional REDESIGN dictionary used for baseline cross-checks."""
    if DICTIONARY_PATH.exists():
        return DICTIONARY_PATH, {"status": "found", "path": str(DICTIONARY_PATH)}
    return None, {"status": "missing", "path": str(DICTIONARY_PATH)}


def market_stats(rows: Sequence[KeywordRow]) -> dict[str, dict[str, JsonValue]]:
    """Summarize measured Keyword rows by ATC4 for audit and reports."""
    grouped: defaultdict[str, list[KeywordRow]] = defaultdict(list)
    for row in rows:
        grouped[row.atc4].append(row)
    return {
        atc4: {
            "row_count": len(items),
            "brand_count": len({item.brand for item in items}),
            "estimated_tokens": sum(estimate_tokens(item.keyword_text) for item in items),
            "brands": sorted({item.brand for item in items}),
        }
        for atc4, items in sorted(grouped.items(), key=lambda entry: (-len(entry[1]), entry[0]))
    }


def brand_stats(rows: Sequence[KeywordRow]) -> list[dict[str, JsonValue]]:
    """Summarize measured Keyword rows by ATC4 and brand."""
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
        for (atc4, brand), items in sorted(grouped.items(), key=lambda entry: (-len(entry[1]), entry[0][0], entry[0][1]))
    ]


def rows_for_market(rows: Sequence[KeywordRow], atc4: str) -> list[KeywordRow]:
    """Filter source rows to one ATC4 market."""
    return [row for row in rows if row.atc4 == atc4]


def rows_for_brand(rows: Sequence[KeywordRow], atc4: str, brand: str) -> list[KeywordRow]:
    """Filter source rows to one ATC4 and product name."""
    return [row for row in rows if row.atc4 == atc4 and row.brand == brand]


def load_alias_descriptions(alias_payload: dict[str, JsonValue], rows: Iterable[KeywordRow]) -> dict[str, BrandDescription]:
    """Map sampled brands to alias and source-company metadata."""
    records = alias_payload.get("records")
    by_brand: dict[str, dict[str, JsonValue]] = {}
    if isinstance(records, list):
        for item in records:
            if isinstance(item, dict) and isinstance(item.get("iqvia_en"), str):
                by_brand[str(item["iqvia_en"])] = item
    result: dict[str, BrandDescription] = {}
    source_companies = _source_companies_by_brand(rows)
    for atc4, brand in sorted(source_companies):
        record = by_brand.get(brand, {})
        companies = source_companies[(atc4, brand)]
        alias_companies = _string_tuple(record.get("representing_company"))
        result[f"{atc4}:{brand}"] = BrandDescription(
            brand=brand,
            atc4=atc4,
            kr_canonical=_optional_str(record.get("kr_canonical")),
            is_jw=bool(record.get("is_jw")) or any(is_jw_representing_company(company) for company in companies),
            molecule=_string_tuple(record.get("molecule")),
            manufacturer=_string_tuple(record.get("manufacturer")),
            representing_company=alias_companies or companies,
        )
    return result


def is_jw_representing_company(value: str) -> bool:
    """Return whether the source company column marks a JW-owned product."""
    normalized = " ".join(value.upper().split())
    return normalized == JW_COMPANY_PREFIX or normalized.startswith(f"{JW_COMPANY_PREFIX} ")


def _keyword_row(record: dict[str, JsonValue]) -> KeywordRow:
    """Convert a MariaDB record into the internal KeywordRow model."""
    return KeywordRow(
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
        representing_company=str(record["representing_company"] or ""),
    )


def _source_companies_by_brand(rows: Iterable[KeywordRow]) -> dict[tuple[str, str], tuple[str, ...]]:
    """Group source representing-company values by ATC4/product."""
    grouped: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        if row.representing_company:
            grouped[(row.atc4, row.brand)].add(row.representing_company)
        else:
            grouped.setdefault((row.atc4, row.brand), set())
    return {key: tuple(sorted(values)) for key, values in grouped.items()}


def _string_tuple(value: JsonValue) -> tuple[str, ...]:
    """Convert a JSON string-list field into a tuple."""
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if isinstance(item, str))


def _optional_str(value: JsonValue) -> str | None:
    """Return a non-empty string or None for sparse alias metadata."""
    return value if isinstance(value, str) and value else None
