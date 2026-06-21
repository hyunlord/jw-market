from __future__ import annotations

from collections import Counter, defaultdict

import pymysql


def _ordered(counter: Counter[str], limit: int = 8) -> tuple[str, ...]:
    return tuple(value for value, _ in counter.most_common(limit) if value)


def fetch_bridge_molecules(
    conn: pymysql.connections.Connection,
    anchors: set[str],
) -> tuple[str | None, dict[str, tuple[str, ...]]]:
    if not anchors:
        return None, {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_schema
            FROM information_schema.tables
            WHERE table_name='mart_brand_molecule'
              AND table_schema LIKE 'jw_mart_molecule_bridge_full_%'
            ORDER BY table_schema DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
        if row is None:
            return None, {}
        db_name = str(row["table_schema"])
        placeholders = ",".join(["%s"] * len(anchors))
        params = sorted(anchors)
        cur.execute(
            f"""
            SELECT UPPER(brand_name) AS brand_name, UPPER(brand_key) AS brand_key, molecule_display
            FROM `{db_name}`.mart_brand_molecule
            WHERE UPPER(brand_name) IN ({placeholders})
               OR UPPER(brand_key) IN ({placeholders})
            """,
            params + params,
        )
        collected: defaultdict[str, Counter[str]] = defaultdict(Counter)
        for bridge_row in cur.fetchall():
            molecule = str(bridge_row["molecule_display"] or "").strip()
            for key in (str(bridge_row["brand_name"] or ""), str(bridge_row["brand_key"] or "")):
                if key in anchors and molecule:
                    collected[key][molecule] += 1
    return db_name, {anchor: _ordered(counter) for anchor, counter in collected.items()}
