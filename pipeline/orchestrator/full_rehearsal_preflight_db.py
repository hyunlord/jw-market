"""Read-only database readiness probe for an isolated R-1 rehearsal."""

from __future__ import annotations

import re
from collections.abc import Mapping

from pipeline.orchestrator.full_rehearsal import FullRehearsalConfig


def check_database_readiness(
    environment: Mapping[str, str], config: FullRehearsalConfig
) -> tuple[bool, str]:
    try:
        import pymysql

        connection = pymysql.connect(
            host=environment["DB_HOST"],
            port=int(environment["DB_PORT"]),
            user=environment["DB_USER"],
            password=environment["DB_PASSWORD"],
            connect_timeout=5,
            read_timeout=5,
            write_timeout=5,
        )
        with connection:
            with connection.cursor() as cursor:
                schemas = (config.source_db, config.target_db, config.cache_db)
                marks = ",".join(["%s"] * len(schemas))
                cursor.execute(
                    f"SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
                    f"WHERE SCHEMA_NAME IN ({marks})",
                    schemas,
                )
                visible = {row[0] for row in cursor.fetchall()}
                cursor.execute(
                    f"SELECT TABLE_SCHEMA, COUNT(*) FROM information_schema.TABLES "
                    f"WHERE TABLE_SCHEMA IN ({marks}) GROUP BY TABLE_SCHEMA",
                    schemas,
                )
                counts = dict(cursor.fetchall())
                cursor.execute("SHOW GRANTS FOR CURRENT_USER()")
                grants = " ".join(row[0] for row in cursor.fetchall()).upper()
        failures = []
        if visible != set(schemas):
            failures.append("required database schemas are not all visible")
        if counts.get(config.target_db, 0) or counts.get(config.cache_db, 0):
            failures.append("isolated target/cache schemas are not empty")
        required = {"SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"}
        words = set(re.findall(r"[A-Z]+", grants))
        if "ALL PRIVILEGES" not in grants and not required.issubset(words):
            failures.append("database grants do not cover rehearsal DDL/DML")
        return not failures, "; ".join(failures) if failures else "schemas visible and isolated"
    except Exception as exc:
        return False, f"database probe failed: {type(exc).__name__}"
