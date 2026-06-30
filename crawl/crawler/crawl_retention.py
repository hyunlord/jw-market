"""Retention helper for two-tier crawl tables.

Default mode is read-only dry-run. Pass --apply only from an approved workflow.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

import pymysql


def connect(args: argparse.Namespace) -> Any:
    return pymysql.connect(
        host=args.db_host,
        port=args.db_port,
        user=args.db_user,
        password=args.db_password,
        database=args.db_name,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def count_expired(conn: Any) -> dict[str, int]:
    queries = {
        "event_brand_scores": """
            SELECT COUNT(*) AS n
            FROM event_brand_scores
            WHERE expire_at IS NOT NULL
              AND expire_at < CURRENT_TIMESTAMP
        """,
        "events": """
            SELECT COUNT(*) AS n
            FROM events e
            LEFT JOIN event_brand_scores s ON s.event_id = e.event_id
            WHERE e.expire_at IS NOT NULL
              AND e.expire_at < CURRENT_TIMESTAMP
              AND s.event_id IS NULL
        """,
        "news_raw": """
            SELECT COUNT(*) AS n
            FROM news_raw n
            LEFT JOIN events e ON e.news_id = n.news_id
            WHERE n.expire_at IS NOT NULL
              AND n.expire_at < CURRENT_TIMESTAMP
              AND e.news_id IS NULL
        """,
    }
    with conn.cursor() as cursor:
        counts: dict[str, int] = {}
        for table, sql in queries.items():
            cursor.execute(sql)
            row = cursor.fetchone() or {"n": 0}
            counts[table] = int(row["n"] or 0)
        return counts


def delete_expired(conn: Any) -> dict[str, int]:
    statements = {
        "event_brand_scores": """
            DELETE FROM event_brand_scores
            WHERE expire_at IS NOT NULL
              AND expire_at < CURRENT_TIMESTAMP
        """,
        "events": """
            DELETE e
            FROM events e
            LEFT JOIN event_brand_scores s ON s.event_id = e.event_id
            WHERE e.expire_at IS NOT NULL
              AND e.expire_at < CURRENT_TIMESTAMP
              AND s.event_id IS NULL
        """,
        "news_raw": """
            DELETE n
            FROM news_raw n
            LEFT JOIN events e ON e.news_id = n.news_id
            WHERE n.expire_at IS NOT NULL
              AND n.expire_at < CURRENT_TIMESTAMP
              AND e.news_id IS NULL
        """,
    }
    deleted: dict[str, int] = {}
    with conn.cursor() as cursor:
        for table, sql in statements.items():
            cursor.execute(sql)
            deleted[table] = int(cursor.rowcount or 0)
    conn.commit()
    return deleted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retention-days", type=int, default=365, help="Deprecated; expire_at now carries the TTL.")
    parser.add_argument("--apply", action="store_true", help="Delete expired Tier2 rows. Omit for read-only dry-run.")
    parser.add_argument("--db-host", default=os.getenv("DB_HOST", "127.0.0.1"))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("DB_PORT", "3306")))
    parser.add_argument("--db-name", default=os.getenv("DB_NAME", "jw_mart"))
    parser.add_argument("--db-user", default=os.getenv("DB_USER", "root"))
    parser.add_argument("--db-password", default=os.getenv("DB_PASSWORD", ""))
    args = parser.parse_args()

    conn = connect(args)
    try:
        if args.apply:
            result = {"dry_run": False, "deleted": delete_expired(conn)}
        else:
            result = {"dry_run": True, "expired": count_expired(conn)}
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
