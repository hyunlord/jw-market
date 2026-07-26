from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Protocol, Self

from pipeline.etl.io.mart.brand_key_normalize import normalize_brand_name

from .change_detection import StoredNoticeState
from .contract import HiraRunMetrics
from .models import ParsedNotice
from .scope import BrandMatch, BrandScopeEntry, MoleculeScopeEntry


class Cursor(Protocol):
    def execute(self, sql: str, params: object = None) -> Any: ...
    def fetchone(self) -> dict[str, Any] | None: ...
    def fetchall(self) -> Sequence[dict[str, Any]]: ...
    def __enter__(self) -> Self: ...
    def __exit__(self, *args: object) -> None: ...


class Connection(Protocol):
    def cursor(self) -> Cursor: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PersistableNotice:
    parsed: ParsedNotice
    listing_fingerprint: str
    brand_matches: tuple[BrandMatch, ...]

    @property
    def brand_names(self) -> tuple[str, ...]:
        return tuple(match.brand_name for match in self.brand_matches)


def latest_notice_id(notice_ids: Sequence[str]) -> str | None:
    """Return a deterministic receipt marker without treating it as a watermark."""

    if not notice_ids:
        return None
    if all(notice_id.isdecimal() for notice_id in notice_ids):
        return max(notice_ids, key=int)
    return max(notice_ids)


def connect_from_env() -> Connection:
    import pymysql

    return pymysql.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("DB_USER", "root"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", ""),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def load_serving_brand_scope(
    conn: Connection,
    *,
    minimum_brand_count: int = 10_000,
) -> tuple[
    tuple[BrandScopeEntry, ...],
    tuple[MoleculeScopeEntry, ...],
    tuple[str, ...],
    str,
]:
    """Load the chat-serving brand universe and canonical molecule bridge."""

    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT brand_key, brand_name, atc4_code
            FROM mart_general_brand_metric
            WHERE brand_key <> '' AND brand_name <> ''
            UNION ALL
            SELECT '' AS brand_key, brand_name, '' AS atc4_code
            FROM mart_strategic_ml_brand_metric
            WHERE brand_name <> ''
            UNION ALL
            SELECT '' AS brand_key, brand_name, '' AS atc4_code
            FROM mart_strategic_cd_brand_metric
            WHERE brand_name <> ''
            """
        )
        universe_rows = cursor.fetchall()
        cursor.execute(
            """
            SELECT alias_name, brand_key
            FROM brand_alias
            WHERE alias_name <> '' AND brand_key <> ''
            """
        )
        alias_rows = cursor.fetchall()
        cursor.execute(
            """
            SELECT molecule_norm, brand_key, brand_name, atc4_code
            FROM mart_brand_molecule
            WHERE molecule_norm <> '' AND brand_key <> ''
            """
        )
        molecule_rows = cursor.fetchall()
        cursor.execute(
            "SELECT raw_text FROM hira_benefit_notice WHERE raw_text <> ''"
        )
        notice_rows = cursor.fetchall()

    atc4_by_identity: dict[tuple[str, str], set[str]] = {}
    canonical_name_by_key: dict[str, str] = {}
    alias_key_by_name = {
        str(row["alias_name"]).strip(): str(row["brand_key"]).strip()
        for row in alias_rows
        if str(row["alias_name"]).strip() and str(row["brand_key"]).strip()
    }
    for row in universe_rows:
        brand_name = str(row["brand_name"]).strip()
        brand_key = (
            str(row["brand_key"]).strip()
            or alias_key_by_name.get(brand_name)
            or normalize_brand_name(brand_name)
        )
        if not brand_key or not brand_name:
            continue
        canonical_name_by_key.setdefault(brand_key, brand_name)
        identity = (brand_key, brand_name)
        atc4_by_identity.setdefault(identity, set())
        atc4_code = str(row.get("atc4_code") or "").strip()
        if atc4_code:
            atc4_by_identity[identity].add(atc4_code)
    if len(canonical_name_by_key) < minimum_brand_count:
        raise RuntimeError(
            "serving brand universe is unexpectedly small: "
            f"{len(canonical_name_by_key)} < {minimum_brand_count}"
        )

    for row in molecule_rows:
        brand_key = str(row["brand_key"]).strip()
        brand_name = str(row["brand_name"]).strip()
        atc4_code = str(row.get("atc4_code") or "").strip()
        identity = (brand_key, brand_name)
        if identity in atc4_by_identity and atc4_code:
            atc4_by_identity[identity].add(atc4_code)

    for row in alias_rows:
        alias_name = str(row["alias_name"]).strip()
        brand_key = str(row["brand_key"]).strip()
        identity = (brand_key, alias_name)
        if alias_name and brand_key in canonical_name_by_key:
            canonical_identity = (brand_key, canonical_name_by_key[brand_key])
            atc4_by_identity.setdefault(identity, set()).update(
                atc4_by_identity.get(canonical_identity, set())
            )

    entries = [
        BrandScopeEntry(
            brand_key=brand_key,
            brand_name=brand_name,
            atc4_codes=tuple(sorted(atc4_codes)),
        )
        for (brand_key, brand_name), atc4_codes in atc4_by_identity.items()
    ]
    brands = tuple(
        sorted(entries, key=lambda row: (row.brand_key, row.brand_name))
    )
    molecules = tuple(
        sorted(
            (
                MoleculeScopeEntry(
                    molecule_norm=str(row["molecule_norm"]).strip(),
                    brand_key=str(row["brand_key"]).strip(),
                    brand_name=str(row["brand_name"]).strip(),
                    atc4_code=str(row.get("atc4_code") or "").strip(),
                )
                for row in molecule_rows
                if str(row["molecule_norm"]).strip()
                and str(row["brand_key"]).strip() in canonical_name_by_key
            ),
            key=lambda row: (row.molecule_norm, row.brand_key, row.atc4_code),
        )
    )
    raw_texts = tuple(str(row["raw_text"]) for row in notice_rows)
    revision_payload = {
        "brands": [
            (row.brand_key, row.brand_name, row.atc4_codes) for row in brands
        ],
        "molecules": [
            (row.molecule_norm, row.brand_key, row.atc4_code) for row in molecules
        ],
    }
    revision = hashlib.sha256(
        json.dumps(
            revision_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return brands, molecules, raw_texts, f"serving_mart:{revision}"


def load_notice_state(conn: Connection) -> dict[str, StoredNoticeState]:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT source_notice_id, listing_fingerprint FROM hira_benefit_notice"
        )
        rows = cursor.fetchall()
    return {
        str(row["source_notice_id"]): StoredNoticeState(
            source_notice_id=str(row["source_notice_id"]),
            listing_fingerprint=str(row["listing_fingerprint"]),
        )
        for row in rows
    }


def has_crawl_state(conn: Connection, *, source_key: str = "hira_insurance_criteria") -> bool:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT 1 AS found FROM hira_benefit_crawl_state WHERE source_key=%s",
            (source_key,),
        )
        return cursor.fetchone() is not None


def persist_batch(
    conn: Connection,
    *,
    notices: Sequence[PersistableNotice],
    run_id: str,
    index_tag_signature_sha256: str,
    mapping_revision: str,
    collected_at: datetime,
    run_metrics: HiraRunMetrics | None = None,
    source_key: str = "hira_insurance_criteria",
) -> None:
    """Commit notice rows, brand links, and the success watermark atomically."""

    timestamp = collected_at.replace(tzinfo=None)
    metrics = run_metrics or HiraRunMetrics(0, 0, 0, 0, len(notices), 0, 0)
    try:
        with conn.cursor() as cursor:
            for item in notices:
                parsed = item.parsed
                cursor.execute(
                    """
                    INSERT INTO hira_benefit_notice (
                      source_notice_id, source_url, title, notice_no, notice_date,
                      target_condition, exclusion_rule, dosage_limit, raw_text,
                      raw_html_sha256, listing_fingerprint, parse_status,
                      parse_failed_fields_json, collected_at, updated_at
                    ) VALUES (
                      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON DUPLICATE KEY UPDATE
                      source_url=VALUES(source_url),
                      title=VALUES(title),
                      notice_no=VALUES(notice_no),
                      notice_date=VALUES(notice_date),
                      target_condition=VALUES(target_condition),
                      exclusion_rule=VALUES(exclusion_rule),
                      dosage_limit=VALUES(dosage_limit),
                      raw_text=VALUES(raw_text),
                      raw_html_sha256=VALUES(raw_html_sha256),
                      listing_fingerprint=VALUES(listing_fingerprint),
                      parse_status=VALUES(parse_status),
                      parse_failed_fields_json=VALUES(parse_failed_fields_json),
                      collected_at=VALUES(collected_at),
                      updated_at=VALUES(updated_at)
                    """,
                    (
                        parsed.source_notice_id,
                        parsed.source_url,
                        parsed.title,
                        parsed.notice_no,
                        parsed.notice_date,
                        parsed.target_condition,
                        parsed.exclusion_rule,
                        parsed.dosage_limit,
                        parsed.raw_text,
                        parsed.raw_html_sha256,
                        item.listing_fingerprint,
                        parsed.parse_status.value,
                        json.dumps(parsed.failed_fields, ensure_ascii=False),
                        timestamp,
                        timestamp,
                    ),
                )
                cursor.execute(
                    "DELETE FROM hira_benefit_notice_brand WHERE source_notice_id=%s",
                    (parsed.source_notice_id,),
                )
                for match in item.brand_matches:
                    cursor.execute(
                        """
                        INSERT INTO hira_benefit_notice_brand (
                          source_notice_id, brand_name, brand_key, match_method, created_at
                        ) VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            parsed.source_notice_id,
                            match.brand_name,
                            match.brand_key,
                            match.match_method,
                            timestamp,
                        ),
                    )
            last_seen_notice_id = latest_notice_id(
                tuple(item.parsed.source_notice_id for item in notices)
            )
            receipt = {
                "run_id": run_id,
                "notice_count": len(notices),
                "mapping_revision": mapping_revision,
                "brand_match_provenance": [
                    {
                        "source_notice_id": item.parsed.source_notice_id,
                        **asdict(match),
                    }
                    for item in notices
                    for match in item.brand_matches
                ],
            }
            cursor.execute(
                """
                INSERT INTO hira_benefit_crawl_run (
                  run_id, started_at, finished_at, exit_code, failures,
                  identity_gap, pending_gap, parsed_count, partial_count,
                  failed_count, status, alert_status, receipt_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'complete', NULL, %s)
                ON DUPLICATE KEY UPDATE
                  finished_at=VALUES(finished_at),
                  exit_code=VALUES(exit_code),
                  failures=VALUES(failures),
                  identity_gap=VALUES(identity_gap),
                  pending_gap=VALUES(pending_gap),
                  parsed_count=VALUES(parsed_count),
                  partial_count=VALUES(partial_count),
                  failed_count=VALUES(failed_count),
                  status=VALUES(status),
                  receipt_json=VALUES(receipt_json)
                """,
                (
                    run_id,
                    timestamp,
                    timestamp,
                    metrics.exit_code,
                    metrics.failures,
                    metrics.identity_gap,
                    metrics.pending_gap,
                    metrics.parsed_count,
                    metrics.partial_count,
                    metrics.failed_count,
                    json.dumps(receipt, ensure_ascii=False, sort_keys=True),
                ),
            )
            cursor.execute(
                """
                INSERT INTO hira_benefit_crawl_state (
                  source_key, last_success_run_id, last_success_at,
                  last_seen_notice_id, index_tag_signature_sha256,
                  mapping_revision, receipt_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                  last_success_run_id=VALUES(last_success_run_id),
                  last_success_at=VALUES(last_success_at),
                  last_seen_notice_id=VALUES(last_seen_notice_id),
                  index_tag_signature_sha256=VALUES(index_tag_signature_sha256),
                  mapping_revision=VALUES(mapping_revision),
                  receipt_json=VALUES(receipt_json)
                """,
                (
                    source_key,
                    run_id,
                    timestamp,
                    last_seen_notice_id,
                    index_tag_signature_sha256,
                    mapping_revision,
                    json.dumps(receipt, ensure_ascii=False, sort_keys=True),
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
