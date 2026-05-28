#!/usr/bin/env python3
"""Phase G local catalog DB sync probe.

The current local jw_mart database does not contain catalog_strategic_brand
or catalog_cd_brand tables. Layer3 scripts read catalog parquet directly, so
there is no catalog DB table to update in this environment.
"""

from __future__ import annotations

import os

import pymysql


def main() -> None:
    password = os.environ.get("MARIADB_PASSWORD") or os.environ.get("DB_PASS") or ""
    user = os.environ.get("MARIADB_USER") or "llmops"
    database = os.environ.get("MARIADB_DATABASE") or "jw_mart"
    conn = pymysql.connect(host="127.0.0.1", port=3308, user=user, password=password, database=database)
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES LIKE 'catalog_strategic_brand'")
            has_sb = cur.fetchone() is not None
            cur.execute("SHOW TABLES LIKE 'catalog_cd_brand'")
            has_cb = cur.fetchone() is not None
    finally:
        conn.close()

    print("catalog_strategic_brand exists:", has_sb)
    print("catalog_cd_brand exists:", has_cb)
    if has_sb or has_cb:
        raise RuntimeError("Unexpected catalog DB tables exist; this no-op sync script refuses to mutate them")
    print("NOOP: Local catalog DB tables are absent; parquet is the Layer3 source of truth.")


if __name__ == "__main__":
    main()
