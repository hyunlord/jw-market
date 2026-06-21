from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Sequence

from pipeline.scripts.analysis.brand_activity.recheck.safety import require_stage_schema
from pipeline.scripts.analysis.brand_activity.recheck.summaries import write_json


ROOT = Path(__file__).resolve().parents[5]
JsonObject = dict[str, Any]


def env_values(path: Path) -> dict[str, str]:
    """Read local Docker dotenv values without emitting secrets to artifacts."""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def loader_environment() -> dict[str, str]:
    """Build an environment that lets legacy loaders read DB credentials safely."""
    env = os.environ.copy()
    env.update(env_values(ROOT / "pipeline" / "docker" / ".env"))
    return env


def run_command(command: Sequence[str], log_dir: Path, env: dict[str, str]) -> JsonObject:
    """Run a child process and persist stdout/stderr as audit logs."""
    log_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(command[1]).stem if len(command) > 1 else "command"
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, env=env, check=False)
    (log_dir / f"{stem}.stdout.log").write_text(result.stdout, encoding="utf-8")
    (log_dir / f"{stem}.stderr.log").write_text(result.stderr, encoding="utf-8")
    payload = {"command": list(command), "returncode": result.returncode}
    write_json(log_dir / f"{stem}.command.json", payload)
    if result.returncode != 0:
        raise SystemExit(f"{stem} failed with code {result.returncode}; see {log_dir}")
    return payload


def stage_snapshot(host: str, port: int, user: str, password: str, schema: str) -> JsonObject:
    """Capture exact stage table row counts and ranges before or after reload."""
    import pymysql

    safe_schema = require_stage_schema(schema)
    tables = {
        "csd": "csd_channel_dynamics_stage",
        "keyword": "km_keyword_event_stage",
        "meeting": "km_meeting_event_stage",
    }
    connection = pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        charset="utf8mb4",
        connect_timeout=8,
    )
    try:
        snapshot: JsonObject = {}
        with connection.cursor() as cursor:
            for kind, table in tables.items():
                cursor.execute(
                    "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                    (safe_schema, table),
                )
                if int(cursor.fetchone()[0]) == 0:
                    snapshot[kind] = {"table": table, "exists": False, "rows": 0}
                    continue
                cursor.execute(f"SELECT COUNT(*), MIN(period_ym), MAX(period_ym) FROM `{safe_schema}`.`{table}`")
                rows, period_min, period_max = cursor.fetchone()
                snapshot[kind] = {
                    "table": table,
                    "exists": True,
                    "rows": int(rows),
                    "period_min": str(period_min),
                    "period_max": str(period_max),
                }
            cursor.execute(
                f"""
                SELECT DISTINCT product_name FROM `{safe_schema}`.`km_keyword_event_stage`
                WHERE REPLACE(UPPER(product_name), ' ', '') LIKE '%%LOWOSMOPERI%%'
                UNION
                SELECT DISTINCT product_name FROM `{safe_schema}`.`km_meeting_event_stage`
                WHERE REPLACE(UPPER(product_name), ' ', '') LIKE '%%LOWOSMOPERI%%'
                ORDER BY product_name
                """
            )
            snapshot["low_osmo_peri_variants"] = [str(row[0]) for row in cursor.fetchall()]
        return snapshot
    finally:
        connection.close()


def run_legacy_loaders(
    audit_dir: Path,
    output_dir: Path,
    roots: dict[str, str],
    coverage: dict[str, list[str]],
    schema: str,
    env: dict[str, str],
) -> None:
    """Run existing isolated ETL loaders against discovered full-month coverage."""
    require_stage_schema(schema)
    csd_months = coverage.get("csd", [])
    km_months = sorted(set(coverage.get("keyword", [])) | set(coverage.get("meeting", [])))
    run_command(
        [
            sys.executable,
            "pipeline/scripts/etl/brand_activity/ingest_csd.py",
            "--source-root",
            roots["csd"],
            "--audit-dir",
            str(audit_dir / "load_csd"),
            "--output-dir",
            str(output_dir / "csd"),
            "--expected-months",
            *csd_months,
            "--db-load",
            "--stage-schema",
            schema,
        ],
        audit_dir / "logs",
        env,
    )
    run_command(
        [
            sys.executable,
            "pipeline/scripts/etl/brand_activity/ingest_keyword_meeting.py",
            "--keyword-root",
            roots["keyword"],
            "--meeting-root",
            roots["meeting"],
            "--audit-dir",
            str(audit_dir / "load_km"),
            "--output-dir",
            str(output_dir / "km"),
            "--expected-months",
            *km_months,
            "--db-load",
            "--stage-schema",
            schema,
        ],
        audit_dir / "logs",
        env,
    )
