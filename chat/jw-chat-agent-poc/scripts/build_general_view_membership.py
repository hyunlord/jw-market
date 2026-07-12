#!/usr/bin/env python3
from __future__ import annotations

import os
from dataclasses import dataclass

from jw_chat_agent_poc.tools.general_view_membership import normalize_general_brand


TARGET_TABLE = "chat_general_brand_membership"
BUILD_TABLE = f"{TARGET_TABLE}_build"
OLD_TABLE = f"{TARGET_TABLE}_old"


@dataclass(frozen=True, slots=True)
class MembershipRow:
    normalized_brand_name: str
    brand_key: str
    brand_name: str
    atc4_code: str
    atc4_description: str
    source: str


def build_membership_rows(rows: list[dict[str, str | None]]) -> tuple[MembershipRow, ...]:
    memberships: dict[tuple[str, str, str], MembershipRow] = {}
    for row in rows:
        brand_key = str(row.get("brand_key") or "").strip()
        brand_name = str(row.get("brand_name") or brand_key).strip()
        atc4_code = str(row.get("atc4_code") or "").strip().upper()
        source = str(row.get("source") or "").strip().lower()
        normalized = normalize_general_brand(brand_name or brand_key)
        if not (normalized and brand_key and atc4_code and source):
            continue
        membership = MembershipRow(
            normalized_brand_name=normalized,
            brand_key=brand_key,
            brand_name=brand_name,
            atc4_code=atc4_code,
            atc4_description=str(row.get("atc4_desc") or f"ATC4 {atc4_code}").strip(),
            source=source,
        )
        memberships[(brand_key, atc4_code, source)] = membership
    return tuple(memberships[key] for key in sorted(memberships))


def create_table_sql(table: str = TARGET_TABLE) -> str:
    return f"""
    CREATE TABLE IF NOT EXISTS `{table}` (
        normalized_brand_name VARCHAR(255) NOT NULL,
        brand_key VARCHAR(255) NOT NULL,
        brand_name VARCHAR(255) NOT NULL,
        atc4_code VARCHAR(16) NOT NULL,
        atc4_description VARCHAR(255) NOT NULL,
        source VARCHAR(16) NOT NULL,
        loaded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (brand_key, atc4_code, source),
        KEY idx_general_membership_name_source (normalized_brand_name, source),
        KEY idx_general_membership_brand_name (brand_name),
        KEY idx_general_membership_atc4_source (atc4_code, source)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """


def load_memberships() -> tuple[int, int, int]:
    import pymysql

    connection_args = {
        "host": os.environ["CHAT_CACHE_DB_HOST"],
        "port": int(os.environ.get("CHAT_CACHE_DB_PORT", "3306")),
        "user": os.environ["CHAT_CACHE_DB_USER"],
        "password": os.environ["CHAT_CACHE_DB_PASSWORD"],
        "database": os.environ.get("CHAT_CACHE_DB_NAME", "jw_mart"),
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
        "autocommit": False,
    }
    with pymysql.connect(**connection_args) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT brand_key, brand_name, atc4_code, atc4_desc, source
                FROM mart_general_brand_metric
                ORDER BY brand_key, atc4_code, source
                """
            )
            memberships = build_membership_rows(cursor.fetchall())
            cursor.execute(f"DROP TABLE IF EXISTS `{BUILD_TABLE}`")
            cursor.execute(create_table_sql(BUILD_TABLE))
            cursor.executemany(
                f"""
                INSERT INTO `{BUILD_TABLE}`
                    (normalized_brand_name, brand_key, brand_name, atc4_code,
                     atc4_description, source)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        row.normalized_brand_name,
                        row.brand_key,
                        row.brand_name,
                        row.atc4_code,
                        row.atc4_description,
                        row.source,
                    )
                    for row in memberships
                ],
            )
            cursor.execute(
                f"""
                SELECT COUNT(*) AS rows_n,
                       COUNT(DISTINCT brand_key) AS brands_n,
                       COUNT(DISTINCT atc4_code) AS atc4_n
                FROM `{BUILD_TABLE}`
                """
            )
            counts = cursor.fetchone()
            if int(counts["brands_n"]) < 1 or int(counts["atc4_n"]) < 1:
                raise RuntimeError("membership build is empty; refusing to publish")
            cursor.execute(f"DROP TABLE IF EXISTS `{OLD_TABLE}`")
            cursor.execute("SHOW TABLES LIKE %s", (TARGET_TABLE,))
            if cursor.fetchone() is None:
                cursor.execute(f"RENAME TABLE `{BUILD_TABLE}` TO `{TARGET_TABLE}`")
            else:
                cursor.execute(
                    f"""
                    RENAME TABLE `{TARGET_TABLE}` TO `{OLD_TABLE}`,
                                 `{BUILD_TABLE}` TO `{TARGET_TABLE}`
                    """
                )
                cursor.execute(f"DROP TABLE `{OLD_TABLE}`")
        connection.commit()
    return int(counts["rows_n"]), int(counts["brands_n"]), int(counts["atc4_n"])


def main() -> int:
    rows_n, brands_n, atc4_n = load_memberships()
    print(f"membership rows={rows_n} brands={brands_n} atc4={atc4_n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
