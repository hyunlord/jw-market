#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import zipfile
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pymysql


JW25_BRANDS = [
    "가드렛",
    "가드메트",
    "뉴트로진",
    "라베칸",
    "라베칸듀오",
    "리바로",
    "리바로브이",
    "리바로젯",
    "리바로페노",
    "리바로하이",
    "모빌리아",
    "베노훼럼",
    "시그마트",
    "악템라",
    "엔커버",
    "위너프",
    "위너프A+",
    "제이다트",
    "제이클",
    "타발리스",
    "트루패스",
    "페린젝트",
    "플라주오피",
    "피나스타",
    "헴리브라",
]

STAGES = ["phenomenon", "cause", "prediction", "recommendation"]
WEAK_NARRATIVE_BRANDS = {"플라주오피", "위너프", "위너프A+", "피나스타"}
RELOAD_STAGE = "stage3a6"
BACKUP_TABLE = "cache_deep_analysis_backup_stage3a6_before"


@dataclass
class SelectedRun:
    brand: str
    run_id: int
    status: str
    model_version: str
    created_at: Any
    bundle_hash: str


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    return str(value)


def _json_dumps(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(value, ensure_ascii=False, indent=indent, default=_json_default)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json_dumps(value, indent=2) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = []
        for row in rows:
            for key in row:
                if key not in fieldnames:
                    fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _json_default(row.get(key)) if row.get(key) is not None else "" for key in fieldnames})


def connect(args: argparse.Namespace) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=args.db_host,
        port=args.db_port,
        user=args.db_user,
        password=os.environ.get("DB_ROOT_PASSWORD", args.db_password),
        database=args.db_name,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def select_latest_runs(conn: pymysql.connections.Connection) -> dict[str, SelectedRun]:
    placeholders = ",".join(["%s"] * len(JW25_BRANDS))
    sql = f"""
    SELECT run_id, brand, status, model_version, created_at, bundle_hash
    FROM zeta_analysis_runs
    WHERE brand IN ({placeholders})
      AND model_version = 'genos_workflow_217'
      AND created_at >= '2026-05-25 00:00:00'
      AND status IN ('ok', 'partial')
    ORDER BY brand, (status = 'ok') DESC, run_id DESC
    """
    with conn.cursor() as cursor:
        cursor.execute(sql, JW25_BRANDS)
        rows = cursor.fetchall()

    selected: dict[str, SelectedRun] = {}
    for row in rows:
        brand = str(row["brand"])
        if brand in selected:
            continue
        selected[brand] = SelectedRun(
            brand=brand,
            run_id=int(row["run_id"]),
            status=str(row["status"]),
            model_version=str(row["model_version"]),
            created_at=row["created_at"],
            bundle_hash=str(row.get("bundle_hash") or ""),
        )
    return selected


def load_parsed_output(conn: pymysql.connections.Connection, run: SelectedRun) -> dict[str, Any]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT stage, title, body, bullets, raw_response, validated
            FROM zeta_analysis_outputs
            WHERE run_id = %s
            ORDER BY FIELD(stage, 'phenomenon', 'cause', 'prediction', 'recommendation')
            """,
            (run.run_id,),
        )
        rows = cursor.fetchall()

    parsed: dict[str, Any] = {}
    for row in rows:
        stage = str(row["stage"])
        if stage not in STAGES:
            continue
        bullets_raw = row.get("bullets") or "[]"
        try:
            bullets = json.loads(bullets_raw) if isinstance(bullets_raw, str) else bullets_raw
        except json.JSONDecodeError:
            bullets = [str(bullets_raw)]
        parsed[stage] = {
            "title": row.get("title") or "",
            "body": row.get("body") or "",
            "bullets": bullets if isinstance(bullets, list) else [str(bullets)],
        }
    return parsed


def build_ai_analysis(run: SelectedRun, parsed: dict[str, Any]) -> dict[str, Any]:
    ai_analysis = {
        "generated_at": datetime.now().isoformat(),
        "model_version": run.model_version,
        "phase_zeta_stage": RELOAD_STAGE,
        "run_id_phase_zeta": run.run_id,
        "reload_reason": (
            "Stage 3-E + 3-A.5 narratives were reset by jw_mart cache rebuild at "
            "2026-05-26 00:04. Reloaded from zeta_analysis_outputs."
        ),
        "phenomenon": parsed["phenomenon"],
        "cause": parsed["cause"],
        "prediction": parsed["prediction"],
        "recommendation": parsed["recommendation"],
    }
    if run.brand in WEAK_NARRATIVE_BRANDS:
        ai_analysis["note"] = "events 부족으로 phenomenon 정밀도 제한적"
    return ai_analysis


def ensure_backup(conn: pymysql.connections.Connection) -> dict[str, Any]:
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {BACKUP_TABLE} AS
            SELECT *, NOW() AS backup_at
            FROM cache_deep_analysis
            """
        )
        cursor.execute(
            f"""
            SELECT COUNT(*) AS row_count,
                   COUNT(DISTINCT brand) AS distinct_brand_count,
                   MIN(updated_at) AS earliest_updated_at,
                   MAX(updated_at) AS latest_updated_at,
                   MIN(backup_at) AS backup_at
            FROM {BACKUP_TABLE}
            """
        )
        info = cursor.fetchone()
    conn.commit()
    return dict(info)


def current_state(conn: pymysql.connections.Connection) -> list[dict[str, Any]]:
    placeholders = ",".join(["%s"] * len(JW25_BRANDS))
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT brand,
                   market_id,
                   JSON_CONTAINS_PATH(response_json, 'one', '$.data.ai_analysis') AS has_ai_analysis_path,
                   COALESCE(JSON_LENGTH(JSON_EXTRACT(response_json, '$.data.ai_analysis')), -1) AS ai_analysis_field_count,
                   JSON_UNQUOTE(JSON_EXTRACT(response_json, '$.data.ai_analysis.phase_zeta_stage')) AS phase_zeta_stage,
                   JSON_UNQUOTE(JSON_EXTRACT(response_json, '$.data.ai_analysis.phenomenon.title')) AS phenomenon_title,
                   updated_at
            FROM cache_deep_analysis
            WHERE brand IN ({placeholders})
            ORDER BY brand
            """,
            JW25_BRANDS,
        )
        return list(cursor.fetchall())


def reload_cache(
    conn: pymysql.connections.Connection,
    payloads: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    log_rows: list[dict[str, Any]] = []
    sql = """
    UPDATE cache_deep_analysis
    SET response_json = JSON_MERGE_PATCH(
            response_json,
            JSON_OBJECT('data', JSON_OBJECT('ai_analysis', JSON_EXTRACT(%s, '$')))
        ),
        updated_at = NOW()
    WHERE brand = %s
    """
    with conn.cursor() as cursor:
        for brand in JW25_BRANDS:
            ai_analysis = payloads[brand]
            cursor.execute(sql, (_json_dumps(ai_analysis), brand))
            log_rows.append(
                {
                    "brand": brand,
                    "run_id": ai_analysis["run_id_phase_zeta"],
                    "phase_zeta_stage": ai_analysis["phase_zeta_stage"],
                    "rows_updated": int(cursor.rowcount),
                }
            )
    conn.commit()
    return log_rows


def cache_batch_pattern(conn: pymysql.connections.Connection) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT DATE_FORMAT(updated_at, '%%Y-%%m-%%d %%H:%%i') AS minute_bucket,
                   COUNT(*) AS rows_updated
            FROM cache_deep_analysis
            WHERE updated_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
            GROUP BY minute_bucket
            ORDER BY minute_bucket
            """
        )
        by_minute = list(cursor.fetchall())
        cursor.execute(
            """
            SELECT DATE(updated_at) AS day,
                   HOUR(updated_at) AS hour,
                   COUNT(*) AS rows_updated
            FROM cache_deep_analysis
            WHERE updated_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)
            GROUP BY day, hour
            ORDER BY day, hour
            """
        )
        by_day_hour = list(cursor.fetchall())
    return by_minute, by_day_hour


def render_html(brand: str, run: SelectedRun, parsed: dict[str, Any]) -> str:
    sections = []
    labels = {
        "phenomenon": "Phenomenon",
        "cause": "Cause",
        "prediction": "Prediction",
        "recommendation": "Recommendation",
    }
    for stage in STAGES:
        data = parsed[stage]
        bullets = "\n".join(f"<li>{bullet}</li>" for bullet in data.get("bullets", []))
        sections.append(
            f"""
            <section>
              <h2>{labels[stage]}</h2>
              <h3>{data.get('title', '')}</h3>
              <p>{data.get('body', '')}</p>
              <ul>{bullets}</ul>
            </section>
            """
        )
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>{brand} Phase ζ ai_analysis reload</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; line-height: 1.55; color: #1f2933; }}
    header {{ border-bottom: 1px solid #d9e2ec; margin-bottom: 24px; padding-bottom: 16px; }}
    h1 {{ font-size: 28px; margin: 0 0 8px; }}
    h2 {{ font-size: 18px; margin-top: 28px; color: #334e68; }}
    h3 {{ font-size: 16px; margin-bottom: 8px; }}
    section {{ max-width: 960px; }}
    .meta {{ color: #627d98; font-size: 13px; }}
  </style>
</head>
<body>
  <header>
    <h1>{brand}</h1>
    <div class="meta">run_id={run.run_id} · status={run.status} · stage={RELOAD_STAGE}</div>
  </header>
  {''.join(sections)}
</body>
</html>
"""


def zip_audit(audit_dir: Path) -> Path:
    zip_path = audit_dir.with_suffix(".zip")
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(audit_dir.rglob("*")):
            zf.write(path, path.relative_to(audit_dir.parent))
    return zip_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reload Phase ζ ai_analysis from zeta_analysis_outputs into cache_deep_analysis.")
    parser.add_argument("--db-host", default=os.getenv("DB_HOST", "localhost"))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("DB_PORT", "3308")))
    parser.add_argument("--db-user", default=os.getenv("DB_USER", "root"))
    parser.add_argument("--db-password", default=os.getenv("DB_ROOT_PASSWORD", ""))
    parser.add_argument("--db-name", default=os.getenv("DB_NAME", "jw_mart"))
    parser.add_argument("--audit-dir", default=None)
    parser.add_argument("--apply", action="store_true", help="Perform backup and cache update. Without this, only extract and verify source data.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parents[2]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    audit_dir = Path(args.audit_dir) if args.audit_dir else root / "outputs" / "phase_zeta_stage3a6" / f"audit_phase_zeta_stage3a6_{timestamp}"
    audit_dir.mkdir(parents=True, exist_ok=True)

    conn = connect(args)
    selected_runs = select_latest_runs(conn)
    missing_runs = [brand for brand in JW25_BRANDS if brand not in selected_runs]
    parsed_by_brand: dict[str, dict[str, Any]] = {}
    ai_payloads: dict[str, dict[str, Any]] = {}
    run_rows: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []

    for brand in JW25_BRANDS:
        run = selected_runs.get(brand)
        if not run:
            continue
        parsed = load_parsed_output(conn, run)
        missing_stages = [stage for stage in STAGES if stage not in parsed]
        run_rows.append(
            {
                "brand": brand,
                "run_id": run.run_id,
                "status": run.status,
                "model_version": run.model_version,
                "created_at": run.created_at,
                "stages_present": "|".join(parsed.keys()),
                "missing_stages": "|".join(missing_stages),
            }
        )
        if missing_stages:
            incomplete.append({"brand": brand, "run_id": run.run_id, "missing_stages": missing_stages})
            continue
        parsed_by_brand[brand] = parsed
        ai_payloads[brand] = build_ai_analysis(run, parsed)
        _write_json(audit_dir / "parsed_outputs_used" / f"{brand}_parsed_output.json", {"run": run_rows[-1], "parsed_output": parsed})
        (audit_dir / "narrative_previews").mkdir(parents=True, exist_ok=True)
        (audit_dir / "narrative_previews" / f"{brand}.html").write_text(render_html(brand, run, parsed), encoding="utf-8")

    _write_csv(audit_dir / "selected_runs.csv", run_rows)
    _write_json(audit_dir / "01_parsed_output_25brand.json", {"selected_runs": run_rows, "incomplete": incomplete, "missing_runs": missing_runs})
    _write_csv(audit_dir / "cache_update" / "pre_update_state.csv", current_state(conn))

    backup_info: dict[str, Any] | None = None
    reload_log: list[dict[str, Any]] = []
    if args.apply:
        if missing_runs or incomplete:
            raise SystemExit(f"Cannot apply reload with missing/incomplete outputs: missing={missing_runs}, incomplete={incomplete}")
        backup_info = ensure_backup(conn)
        reload_log = reload_cache(conn, ai_payloads)
        _write_csv(audit_dir / "reload_log_per_brand.csv", reload_log)
        _write_csv(audit_dir / "cache_update" / "post_update_verification.csv", current_state(conn))
    else:
        _write_csv(audit_dir / "cache_update" / "post_update_verification.csv", [])

    by_minute, by_day_hour = cache_batch_pattern(conn)
    _write_csv(audit_dir / "cache_batch_pattern.csv", by_minute)
    _write_json(audit_dir / "cache_batch_pattern_by_day_hour.json", by_day_hour)

    backups: list[dict[str, Any]] = []
    with conn.cursor() as cursor:
        cursor.execute("SHOW TABLES LIKE 'cache_deep_analysis_backup%'")
        for table_row in cursor.fetchall():
            table_name = next(iter(table_row.values()))
            cursor.execute(f"SELECT COUNT(*) AS row_count, COUNT(DISTINCT brand) AS brand_count, MIN(updated_at) AS min_updated_at, MAX(updated_at) AS max_updated_at FROM `{table_name}`")
            info = dict(cursor.fetchone())
            info["table_name"] = table_name
            backups.append(info)
    _write_json(audit_dir / "backup_tables_now.json", backups)
    (audit_dir / "cache_update" / "new_backup_table_name.txt").write_text(f"{BACKUP_TABLE}\n", encoding="utf-8")
    (audit_dir / "cache_update" / "rollback_sql.txt").write_text(
        f"""-- Roll back Stage 3-A.6 reload to the pre-reload snapshot.
-- Review before use. This restores the entire cache_deep_analysis table.
DELETE FROM cache_deep_analysis;
INSERT INTO cache_deep_analysis (brand, market_id, response_json, payload_size, updated_at)
SELECT brand, market_id, response_json, payload_size, updated_at
FROM {BACKUP_TABLE};
""",
        encoding="utf-8",
    )

    post_rows = current_state(conn)
    nonempty = [row for row in post_rows if int(row.get("ai_analysis_field_count") or 0) > 0]
    stage_ok = [row for row in post_rows if row.get("phase_zeta_stage") == RELOAD_STAGE]
    titles_ok = [row for row in post_rows if row.get("phenomenon_title")]
    total_rows_updated = sum(int(row.get("rows_updated") or 0) for row in reload_log)
    verdict = "PASS" if args.apply and len(nonempty) == 25 and len(stage_ok) == 25 and len(titles_ok) == 25 else "DRY_RUN" if not args.apply else "PARTIAL"

    try:
        head_before_commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=root, text=True).strip()
        git_status = subprocess.check_output(["git", "status", "--short"], cwd=root, text=True)
    except Exception as exc:
        head_before_commit = ""
        git_status = f"git status failed: {exc}"
    (audit_dir / "git_log.txt").write_text(f"HEAD before commit: {head_before_commit}\n\n{git_status}", encoding="utf-8")

    audit_result = {
        "verdict": verdict,
        "audit_time": datetime.now().isoformat(),
        "commit": None,
        "tag": "phase-zeta-stage3a6-ai-analysis-reload",
        "head_before_commit": head_before_commit,
        "background": {
            "previous_state": "ai_analysis empty due to jw_mart cache rebuild at 2026-05-26 00:04",
            "source_data": "zeta_analysis_outputs stage rows",
            "llm_recall_avoided": True,
            "cost_usd": 0,
        },
        "reload_summary": {
            "total_brands_attempted": len(JW25_BRANDS),
            "brands_with_complete_4_stages": len(parsed_by_brand),
            "brands_with_partial_stages": len(incomplete),
            "brands_failed_reload": len(missing_runs) + len(incomplete),
            "cache_rows_updated": total_rows_updated,
        },
        "post_update_verification": {
            "all_25_brands_have_nonempty_ai_analysis": len(nonempty) == 25,
            "all_25_have_phase_zeta_stage_marker": len(stage_ok) == 25,
            "all_25_have_phenomenon_title": len(titles_ok) == 25,
            "missing_brands_after_reload": [brand for brand in JW25_BRANDS if brand not in {row["brand"] for row in post_rows}],
        },
        "backup_tables_now": backups,
        "stage3a6_backup": backup_info,
        "jw_mart_cache_batch_observation": {
            "last_observed_rebuild_at": "2026-05-26 00:04",
            "pattern_file": "cache_batch_pattern.csv",
            "risk_of_overwrite_within_24h": "high",
            "recommended_db_chat_coordination": "cache rebuild must preserve response_json.data.ai_analysis or reapply Phase ζ after each rebuild.",
        },
        "next_steps": [
            "jw_mart DB 채팅 통보: cache 재생성 시 ai_analysis 키 보존 필수",
            "PL production /api/deep-analysis sample 검증",
            "jw_mart fix 후 영구 유지 확인",
        ],
    }
    _write_json(audit_dir / "audit_result.json", audit_result)

    (audit_dir / "summary.md").write_text(
        f"""# Phase ζ Stage 3-A.6 ai_analysis reload

Verdict: {verdict}

- Source: zeta_analysis_outputs, no LLM recall.
- Brands with complete 4-stage outputs: {len(parsed_by_brand)}/25.
- Cache rows updated: {total_rows_updated}.
- Stage marker: {RELOAD_STAGE}.
- Backup table: {BACKUP_TABLE}.
""",
        encoding="utf-8",
    )

    # Keep a copy of the script used in the audit pack.
    script_copy_dir = audit_dir / "code_diff"
    script_copy_dir.mkdir(parents=True, exist_ok=True)
    script_copy = script_copy_dir / "reload_script.py"
    script_copy.write_text(Path(__file__).read_text(encoding="utf-8"), encoding="utf-8")

    zip_path = zip_audit(audit_dir)
    print(_json_dumps({"audit_dir": str(audit_dir), "zip_path": str(zip_path), "audit_result": audit_result}, indent=2))
    conn.close()
    return 0 if verdict in {"PASS", "DRY_RUN"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
