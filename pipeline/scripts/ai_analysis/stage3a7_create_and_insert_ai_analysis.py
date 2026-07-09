#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import csv
import json
import os
import subprocess
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

import pymysql


DEFAULT_STAGE3A7_BRANDS = [
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
WEAK_NARRATIVE_BRANDS = {
    "플라주오피": "events 부족으로 phenomenon 정밀도 제한적",
    "위너프": "events 부족으로 phenomenon 정밀도 제한적",
    "위너프A+": "events 부족으로 phenomenon 정밀도 제한적",
    "피나스타": "events 부족으로 phenomenon 정밀도 제한적",
}
STAGE_MARKER = "stage3a7"
TARGET_TABLE = "cache_deep_analysis_ai_analysis"

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TARGET_TABLE} (
    brand VARCHAR(255) NOT NULL,
    market_id VARCHAR(20),
    ai_analysis_json LONGTEXT,
    ai_analysis_short_json LONGTEXT,
    ai_analysis_long_json LONGTEXT,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (brand)
);
"""

ADD_VARIANT_COLUMNS_SQL = [
    f"ALTER TABLE {TARGET_TABLE} ADD COLUMN ai_analysis_short_json LONGTEXT AFTER ai_analysis_json",
    f"ALTER TABLE {TARGET_TABLE} ADD COLUMN ai_analysis_long_json LONGTEXT AFTER ai_analysis_short_json",
]


@dataclass
class SelectedRun:
    brand: str
    run_id: int
    status: str
    model_version: str
    created_at: Any
    bundle_hash: str
    analysis_variant: str = "legacy"


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _json_dumps(value: Any, *, indent: int | None = None) -> str:
    return json.dumps(value, ensure_ascii=False, indent=indent, default=_json_default)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json_dumps(value, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


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


def target_table_exists(conn: pymysql.connections.Connection) -> bool:
    with conn.cursor() as cursor:
        cursor.execute("SHOW TABLES LIKE %s", (TARGET_TABLE,))
        return cursor.fetchone() is not None


def create_and_describe_table(conn: pymysql.connections.Connection) -> dict[str, Any]:
    existed_before = target_table_exists(conn)
    with conn.cursor() as cursor:
        cursor.execute(CREATE_TABLE_SQL)
        cursor.execute(f"SHOW COLUMNS FROM {TARGET_TABLE}")
        fields = {str(row["Field"]) for row in cursor.fetchall()}
        for sql in ADD_VARIANT_COLUMNS_SQL:
            column = sql.split(" ADD COLUMN ", 1)[1].split()[0]
            if column not in fields:
                cursor.execute(sql)
        cursor.execute(f"SHOW CREATE TABLE {TARGET_TABLE}")
        show_create = cursor.fetchone()
        cursor.execute(f"DESCRIBE {TARGET_TABLE}")
        describe = cursor.fetchall()
        cursor.execute(f"SELECT COUNT(*) AS row_count FROM {TARGET_TABLE}")
        row_count = cursor.fetchone()["row_count"]
    conn.commit()
    return {
        "existed_before": existed_before,
        "table_created_or_existed": "existed" if existed_before else "created",
        "show_create": show_create,
        "describe": describe,
        "initial_row_count": row_count,
    }


def schema_matches(describe_rows: list[dict[str, Any]]) -> bool:
    expected = {
        "brand": {"Null": "NO", "Key": "PRI"},
        "market_id": {"Null": "YES"},
        "ai_analysis_json": {"Null": "YES"},
        "ai_analysis_short_json": {"Null": "YES"},
        "ai_analysis_long_json": {"Null": "YES"},
        "updated_at": {"Null": "YES"},
    }
    by_field = {str(row["Field"]): row for row in describe_rows}
    for field, checks in expected.items():
        row = by_field.get(field)
        if row is None:
            return False
        for key, expected_value in checks.items():
            if str(row.get(key)) != expected_value:
                return False
    return (
        "varchar(255)" in str(by_field["brand"]["Type"]).lower()
        and "longtext" in str(by_field["ai_analysis_json"]["Type"]).lower()
        and "longtext" in str(by_field["ai_analysis_short_json"]["Type"]).lower()
        and "longtext" in str(by_field["ai_analysis_long_json"]["Type"]).lower()
    )


def _dedupe_brands(brands: Sequence[str]) -> list[str]:
    deduped: list[str] = []
    for brand in brands:
        if brand and brand not in deduped:
            deduped.append(brand)
    if not deduped:
        raise ValueError("at least one brand is required")
    return deduped


def align_stage_evidence_basis(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy whose stage evidence items use the portal `basis` contract.

    The portal renders evidence text from `basis`, while historical Agent2
    stage payloads emitted the same value under `source`.  This intentionally
    touches only the four stage-level `evidence[]` arrays; top-level
    `evidence_pool` and other `source` fields use separate meanings.
    """

    aligned = copy.deepcopy(payload)
    for stage in STAGES:
        stage_payload = aligned.get(stage)
        if not isinstance(stage_payload, dict):
            continue
        evidence = stage_payload.get("evidence")
        if not isinstance(evidence, list):
            continue
        for item in evidence:
            if not isinstance(item, dict) or "source" not in item:
                continue
            if "basis" not in item:
                item["basis"] = item["source"]
            item.pop("source", None)
    return aligned


def table_has_column(conn: pymysql.connections.Connection, table: str, column: str) -> bool:
    with conn.cursor() as cursor:
        cursor.execute(f"SHOW COLUMNS FROM {table} LIKE %s", (column,))
        return cursor.fetchone() is not None


def select_latest_runs(
    conn: pymysql.connections.Connection,
    brands: Sequence[str],
    analysis_variant: str = "legacy",
) -> dict[str, SelectedRun]:
    brands = _dedupe_brands(brands)
    placeholders = ",".join(["%s"] * len(brands))
    has_variant_column = table_has_column(conn, "zeta_analysis_runs", "analysis_variant")
    variant_select = "analysis_variant" if has_variant_column else "'legacy' AS analysis_variant"
    variant_filter = "AND analysis_variant = %s" if has_variant_column else ""
    sql = f"""
    SELECT run_id, brand, status, model_version, created_at, bundle_hash, {variant_select}
    FROM zeta_analysis_runs
    WHERE brand IN ({placeholders})
      AND model_version = 'genos_workflow_217'
      AND created_at >= '2026-05-25 00:00:00'
      AND status IN ('ok', 'partial')
      {variant_filter}
    ORDER BY brand, run_id DESC
    """
    with conn.cursor() as cursor:
        params = [*brands, analysis_variant] if has_variant_column else list(brands)
        cursor.execute(sql, params)
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
            analysis_variant=str(row.get("analysis_variant") or analysis_variant),
        )
    return selected


def load_parsed_output(conn: pymysql.connections.Connection, run: SelectedRun) -> dict[str, Any]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT stage, title, body, bullets
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


def load_market_ids(conn: pymysql.connections.Connection, brands: Sequence[str]) -> tuple[dict[str, str | None], str]:
    brands = _dedupe_brands(brands)
    placeholders = ",".join(["%s"] * len(brands))
    with conn.cursor() as cursor:
        cursor.execute("SHOW TABLES LIKE 'strategic_brand'")
        has_strategic_brand = cursor.fetchone() is not None
        if has_strategic_brand:
            cursor.execute(
                f"""
                SELECT brand_name AS brand, ml_id AS market_id
                FROM strategic_brand
                WHERE brand_name IN ({placeholders}) AND is_jw = 1
                """,
                brands,
            )
            rows = cursor.fetchall()
            return {str(row["brand"]): row.get("market_id") for row in rows}, "strategic_brand.ml_id"

        cursor.execute(
            f"""
            SELECT brand, MIN(market_id) AS market_id
            FROM cache_deep_analysis
            WHERE brand IN ({placeholders})
            GROUP BY brand
            """,
            brands,
        )
        rows = cursor.fetchall()
        return {str(row["brand"]): row.get("market_id") for row in rows}, "cache_deep_analysis.market_id fallback"


def build_ai_analysis(run: SelectedRun, parsed: dict[str, Any]) -> dict[str, Any]:
    ai_analysis = {
        "generated_at": datetime.now().isoformat(),
        "model_version": run.model_version,
        "phase_zeta_stage": STAGE_MARKER,
        "run_id_phase_zeta": run.run_id,
        "reload_reason": (
            "Permanent insert into cache_deep_analysis_ai_analysis "
            "(separated from cache_deep_analysis). Source: zeta_analysis_outputs."
        ),
        "phenomenon": parsed["phenomenon"],
        "cause": parsed["cause"],
        "prediction": parsed["prediction"],
        "recommendation": parsed["recommendation"],
    }
    if run.brand in WEAK_NARRATIVE_BRANDS:
        ai_analysis["note"] = WEAK_NARRATIVE_BRANDS[run.brand]
    return align_stage_evidence_basis(ai_analysis)


def build_variant_ai_analysis(run: SelectedRun, parsed: dict[str, Any], analysis_variant: str) -> dict[str, Any]:
    payload = build_ai_analysis(
        SelectedRun(
            brand=run.brand,
            run_id=run.run_id,
            status=run.status,
            model_version=run.model_version,
            created_at=run.created_at,
            bundle_hash=run.bundle_hash,
            analysis_variant=analysis_variant,
        ),
        parsed,
    )
    payload["analysis_variant"] = analysis_variant
    return payload


def insert_ai_analysis(
    conn: pymysql.connections.Connection,
    payloads: dict[str, dict[str, Any]],
    market_ids: dict[str, str | None],
    brands: Sequence[str],
    short_payloads: dict[str, dict[str, Any]] | None = None,
    long_payloads: dict[str, dict[str, Any]] | None = None,
    variants_only: bool = False,
) -> list[dict[str, Any]]:
    include_variants = short_payloads is not None or long_payloads is not None
    if variants_only:
        if not include_variants:
            raise ValueError("variants_only requires short_payloads or long_payloads")
        sql = f"""
        INSERT INTO {TARGET_TABLE}
            (brand, market_id, ai_analysis_short_json, ai_analysis_long_json)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            ai_analysis_short_json = COALESCE(VALUES(ai_analysis_short_json), ai_analysis_short_json),
            ai_analysis_long_json = COALESCE(VALUES(ai_analysis_long_json), ai_analysis_long_json)
        """
    elif include_variants:
        sql = f"""
        INSERT INTO {TARGET_TABLE}
            (brand, market_id, ai_analysis_json, ai_analysis_short_json, ai_analysis_long_json)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            ai_analysis_json = VALUES(ai_analysis_json),
            ai_analysis_short_json = COALESCE(VALUES(ai_analysis_short_json), ai_analysis_short_json),
            ai_analysis_long_json = COALESCE(VALUES(ai_analysis_long_json), ai_analysis_long_json),
            market_id = VALUES(market_id)
        """
    else:
        sql = f"""
        INSERT INTO {TARGET_TABLE}
            (brand, market_id, ai_analysis_json)
        VALUES (%s, %s, %s)
        ON DUPLICATE KEY UPDATE
            ai_analysis_json = VALUES(ai_analysis_json),
            market_id = VALUES(market_id)
        """
    rows: list[dict[str, Any]] = []
    with conn.cursor() as cursor:
        for brand in _dedupe_brands(brands):
            payload = payloads.get(brand)
            short_payload = short_payloads.get(brand) if short_payloads else None
            long_payload = long_payloads.get(brand) if long_payloads else None
            market_id = market_ids.get(brand)
            if variants_only:
                cursor.execute(
                    sql,
                    (
                        brand,
                        market_id,
                        _json_dumps(short_payload) if short_payload else None,
                        _json_dumps(long_payload) if long_payload else None,
                    ),
                )
            elif include_variants:
                if payload is None:
                    raise KeyError(f"missing legacy payload for {brand}")
                cursor.execute(
                    sql,
                    (
                        brand,
                        market_id,
                        _json_dumps(payload),
                        _json_dumps(short_payload) if short_payload else None,
                        _json_dumps(long_payload) if long_payload else None,
                    ),
                )
            else:
                if payload is None:
                    raise KeyError(f"missing payload for {brand}")
                cursor.execute(sql, (brand, market_id, _json_dumps(payload)))
            rows.append(
                {
                    "brand": brand,
                    "market_id": market_id,
                    "run_id": payload.get("run_id_phase_zeta") if payload else None,
                    "short_run_id": short_payload.get("run_id_phase_zeta") if short_payload else None,
                    "long_run_id": long_payload.get("run_id_phase_zeta") if long_payload else None,
                    "phase_zeta_stage": (
                        (payload or short_payload or long_payload or {}).get("phase_zeta_stage")
                    ),
                    "affected_rows": int(cursor.rowcount),
                }
            )
    conn.commit()
    return rows


def verify_insert(conn: pymysql.connections.Connection, brands: Sequence[str]) -> list[dict[str, Any]]:
    brands = _dedupe_brands(brands)
    placeholders = ",".join(["%s"] * len(brands))
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT
                brand,
                JSON_LENGTH(ai_analysis_json) AS analysis_size,
                JSON_LENGTH(ai_analysis_short_json) AS short_analysis_size,
                JSON_LENGTH(ai_analysis_long_json) AS long_analysis_size,
                JSON_UNQUOTE(JSON_EXTRACT(ai_analysis_json, '$.phase_zeta_stage')) AS stage,
                JSON_UNQUOTE(JSON_EXTRACT(ai_analysis_short_json, '$.analysis_variant')) AS short_variant,
                JSON_UNQUOTE(JSON_EXTRACT(ai_analysis_long_json, '$.analysis_variant')) AS long_variant,
                JSON_UNQUOTE(JSON_EXTRACT(ai_analysis_json, '$.phenomenon.title')) AS phenomenon_title,
                JSON_UNQUOTE(JSON_EXTRACT(ai_analysis_short_json, '$.phenomenon.title')) AS short_phenomenon_title,
                JSON_UNQUOTE(JSON_EXTRACT(ai_analysis_long_json, '$.phenomenon.title')) AS long_phenomenon_title,
                market_id,
                updated_at
            FROM {TARGET_TABLE}
            WHERE brand IN ({placeholders})
            ORDER BY brand
            """,
            brands,
        )
        return list(cursor.fetchall())


def verify_weak_notes(conn: pymysql.connections.Connection) -> list[dict[str, Any]]:
    brands = list(WEAK_NARRATIVE_BRANDS)
    placeholders = ",".join(["%s"] * len(brands))
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT brand,
                   JSON_UNQUOTE(JSON_EXTRACT(ai_analysis_json, '$.note')) AS quality_note
            FROM {TARGET_TABLE}
            WHERE brand IN ({placeholders})
            ORDER BY brand
            """,
            brands,
        )
        return list(cursor.fetchall())


def render_html(brand: str, run: SelectedRun, parsed: dict[str, Any]) -> str:
    labels = {
        "phenomenon": "Phenomenon",
        "cause": "Cause",
        "prediction": "Prediction",
        "recommendation": "Recommendation",
    }
    sections = []
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
  <title>{brand} Phase ζ ai_analysis</title>
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
    <div class="meta">run_id={run.run_id} · status={run.status} · stage={STAGE_MARKER}</div>
  </header>
  {''.join(sections)}
</body>
</html>
"""


def zip_audit(audit_dir: Path) -> Path:
    zip_path = audit_dir.with_suffix(".zip")
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(audit_dir.rglob("*")):
            archive.write(path, path.relative_to(audit_dir.parent))
    return zip_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create permanent Phase ζ ai_analysis table and insert Phase ζ narratives.")
    parser.add_argument("--db-host", default=os.getenv("DB_HOST", "localhost"))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("DB_PORT", "3308")))
    parser.add_argument("--db-user", default=os.getenv("DB_USER", "root"))
    parser.add_argument("--db-password", default=os.getenv("DB_ROOT_PASSWORD", ""))
    parser.add_argument("--db-name", default=os.getenv("DB_NAME", "jw_mart_d2_stage_20260630_r2"))
    parser.add_argument("--audit-dir", default=None)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--brands",
        nargs="*",
        default=None,
        help="Brands to insert. Defaults to the original JW25 list for backward-compatible safe runs.",
    )
    parser.add_argument("--include-variants", action="store_true", help="Assemble short/long sibling payloads from variant zeta runs.")
    parser.add_argument(
        "--variants-only",
        action="store_true",
        help="Update only ai_analysis_short_json/ai_analysis_long_json; leave ai_analysis_json untouched.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    brands = _dedupe_brands(args.brands if args.brands is not None else DEFAULT_STAGE3A7_BRANDS)
    root = Path(__file__).resolve().parents[3]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    audit_dir = Path(args.audit_dir) if args.audit_dir else root / "outputs" / "phase_zeta_stage3a7" / f"audit_phase_zeta_stage3a7_{timestamp}"
    audit_dir.mkdir(parents=True, exist_ok=True)

    conn = connect(args)
    table_info = create_and_describe_table(conn) if args.apply else {
        "existed_before": target_table_exists(conn),
        "table_created_or_existed": "dry_run",
        "show_create": {},
        "describe": [],
        "initial_row_count": None,
    }
    if args.apply and not schema_matches(table_info["describe"]):
        raise SystemExit("cache_deep_analysis_ai_analysis schema does not match DB chat §2.1 contract")

    _write_text(audit_dir / "new_table_setup" / "create_table_sql.sql", CREATE_TABLE_SQL.strip() + "\n")
    _write_text(audit_dir / "new_table_setup" / "show_create_table.txt", _json_dumps(table_info["show_create"], indent=2) + "\n")
    _write_text(audit_dir / "new_table_setup" / "describe_output.txt", _json_dumps(table_info["describe"], indent=2) + "\n")
    _write_text(audit_dir / "new_table_setup" / "initial_row_count.txt", f"{table_info['initial_row_count']}\n")

    selected_runs = select_latest_runs(conn, brands)
    selected_short_runs = select_latest_runs(conn, brands, analysis_variant="short") if args.include_variants or args.variants_only else {}
    selected_long_runs = select_latest_runs(conn, brands, analysis_variant="long") if args.include_variants or args.variants_only else {}
    market_ids, market_id_source = load_market_ids(conn, brands)
    missing_runs = [] if args.variants_only else [brand for brand in brands if brand not in selected_runs]
    missing_short_runs = [brand for brand in brands if (args.include_variants or args.variants_only) and brand not in selected_short_runs]
    missing_long_runs = [brand for brand in brands if (args.include_variants or args.variants_only) and brand not in selected_long_runs]
    parsed_by_brand: dict[str, dict[str, Any]] = {}
    payloads: dict[str, dict[str, Any]] = {}
    short_payloads: dict[str, dict[str, Any]] = {}
    long_payloads: dict[str, dict[str, Any]] = {}
    run_rows: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []

    if not args.variants_only:
        for brand in brands:
            run = selected_runs.get(brand)
            if run is None:
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
                    "bundle_hash": run.bundle_hash,
                    "analysis_variant": run.analysis_variant,
                    "stages_present": "|".join(parsed.keys()),
                    "missing_stages": "|".join(missing_stages),
                    "market_id": market_ids.get(brand),
                }
            )
            if missing_stages:
                incomplete.append({"brand": brand, "run_id": run.run_id, "missing_stages": missing_stages})
                continue
            parsed_by_brand[brand] = parsed
            payloads[brand] = build_ai_analysis(run, parsed)
            _write_json(audit_dir / "parsed_outputs_used" / f"{brand}_parsed_output.json", {"run": run_rows[-1], "parsed_output": parsed})
            _write_text(audit_dir / "narrative_previews" / f"{brand}.html", render_html(brand, run, parsed))

    for variant_name, selected, target in (
        ("short", selected_short_runs, short_payloads),
        ("long", selected_long_runs, long_payloads),
    ):
        if not (args.include_variants or args.variants_only):
            continue
        for brand in brands:
            run = selected.get(brand)
            if run is None:
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
                    "bundle_hash": run.bundle_hash,
                    "analysis_variant": variant_name,
                    "stages_present": "|".join(parsed.keys()),
                    "missing_stages": "|".join(missing_stages),
                    "market_id": market_ids.get(brand),
                }
            )
            if missing_stages:
                incomplete.append({"brand": brand, "run_id": run.run_id, "analysis_variant": variant_name, "missing_stages": missing_stages})
                continue
            target[brand] = build_variant_ai_analysis(run, parsed, variant_name)
            _write_json(
                audit_dir / "parsed_outputs_used" / f"{brand}_{variant_name}_parsed_output.json",
                {"run": run_rows[-1], "parsed_output": parsed},
            )

    _write_json(audit_dir / "parsed_outputs_used" / "selected_runs.json", run_rows)
    _write_json(audit_dir / "parsed_outputs_used" / "market_id_mapping.json", {"source": market_id_source, "market_ids": market_ids})

    insert_rows: list[dict[str, Any]] = []
    if args.apply:
        missing_all = missing_runs + missing_short_runs + missing_long_runs
        if missing_all or incomplete:
            raise SystemExit(f"Cannot insert with missing/incomplete outputs: missing={missing_all}, incomplete={incomplete}")
        insert_rows = insert_ai_analysis(
            conn,
            payloads,
            market_ids,
            brands,
            short_payloads=short_payloads if (args.include_variants or args.variants_only) else None,
            long_payloads=long_payloads if (args.include_variants or args.variants_only) else None,
            variants_only=args.variants_only,
        )
    _write_csv(audit_dir / "insert_results" / "insert_log_per_brand.csv", insert_rows)

    verification = verify_insert(conn, brands) if args.apply else []
    weak_notes = verify_weak_notes(conn) if args.apply else []
    _write_csv(audit_dir / "insert_results" / "post_insert_verification.csv", verification)
    _write_csv(audit_dir / "insert_results" / "weak_brand_notes.csv", weak_notes)
    _write_json(audit_dir / "insert_results" / "post_insert_verification.json", verification)

    try:
        head_before_commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=root, text=True).strip()
        git_status = subprocess.check_output(["git", "status", "--short"], cwd=root, text=True)
    except Exception as exc:
        head_before_commit = ""
        git_status = f"git status failed: {exc}"
    _write_text(audit_dir / "git_log.txt", f"HEAD before commit: {head_before_commit}\n\n{git_status}")

    found = {row["brand"] for row in verification}
    stage_ok = [row for row in verification if row.get("stage") == STAGE_MARKER]
    title_ok = [row for row in verification if row.get("phenomenon_title")]
    analysis_ok = [row for row in verification if int(row.get("analysis_size") or 0) > 4]
    short_ok = [row for row in verification if row.get("short_variant") == "short" and int(row.get("short_analysis_size") or 0) > 4]
    long_ok = [row for row in verification if row.get("long_variant") == "long" and int(row.get("long_analysis_size") or 0) > 4]
    note_by_brand = {row["brand"]: row.get("quality_note") for row in weak_notes}
    expected_weak_notes = {brand: note for brand, note in WEAK_NARRATIVE_BRANDS.items() if brand in brands}
    weak_notes_correct = {
        brand: note_by_brand.get(brand) == note
        for brand, note in expected_weak_notes.items()
    }
    expected_count = len(brands)
    if args.variants_only:
        pass_condition = (
            args.apply
            and len(found) == expected_count
            and len(short_ok) == expected_count
            and len(long_ok) == expected_count
        )
    else:
        pass_condition = (
            args.apply
            and len(found) == expected_count
            and len(stage_ok) == expected_count
            and len(title_ok) == expected_count
            and len(analysis_ok) == expected_count
            and all(weak_notes_correct.values())
        )
    verdict = "PASS" if pass_condition else "DRY_RUN" if not args.apply else "PARTIAL"

    audit_result = {
        "verdict": verdict,
        "audit_time": datetime.now().isoformat(),
        "commit": None,
        "tag": "phase-zeta-stage3a7-new-table-insert",
        "head_before_commit": head_before_commit,
        "background": {
            "milestone": "Permanent ai_analysis insert into separated table",
            "decision_source": "DB chat answer 2026-05-26 (Option 2 variant)",
            "stage3a6_data_fate": "cache_deep_analysis.ai_analysis may be reset by build_cache; this insert is independent.",
            "llm_recall_avoided": True,
            "cost_usd": 0,
        },
        "new_table_setup": {
            "table_name": TARGET_TABLE,
            "schema_source": "DB chat answer §2.1",
            "schema_match": args.apply and schema_matches(table_info["describe"]),
            "table_created_or_existed": table_info["table_created_or_existed"],
            "initial_row_count": table_info["initial_row_count"],
        },
        "insert_summary": {
            "total_brands_attempted": expected_count,
            "brands_with_complete_4_stages": len(parsed_by_brand),
            "brands_with_partial_stages": len(incomplete),
            "brands_failed_insert": len(missing_runs) + len(incomplete),
            "table_rows_after_insert": len(verification),
            "market_id_source": market_id_source,
        },
        "post_insert_verification": {
            "all_requested_brands_present": len(found) == expected_count,
            "all_requested_have_phase_zeta_stage_marker": len(stage_ok) == expected_count,
            "all_requested_have_phenomenon_title": len(title_ok) == expected_count,
            "all_requested_stage_eq_stage3a7": len(stage_ok) == expected_count,
            "all_requested_have_nontrivial_json": len(analysis_ok) == expected_count,
            "all_25_brands_present": expected_count == 25 and len(found) == 25,
            "all_25_have_phase_zeta_stage_marker": expected_count == 25 and len(stage_ok) == 25,
            "all_25_have_phenomenon_title": expected_count == 25 and len(title_ok) == 25,
            "all_25_stage_eq_stage3a7": expected_count == 25 and len(stage_ok) == 25,
            "all_25_have_nontrivial_json": expected_count == 25 and len(analysis_ok) == 25,
            "missing_brands_after_insert": [brand for brand in brands if brand not in found],
            "weak_brand_notes_correct": weak_notes_correct,
        },
        "permanent_safety_evidence": {
            "new_table_independent_of_build_cache": True,
            "phase_30_2_30_3_impact_on_new_table": "none expected unless future jobs explicitly target this table",
            "next_steps_for_screen_display": "Phase 30.3 Backend API merge",
        },
        "stage3a6_data_fate_record": {
            "cache_deep_analysis_ai_analysis_key": "cache_deep_analysis may be reset by build_cache",
            "permanent_data_location": TARGET_TABLE,
        },
        "next_steps": [
            "PL review of 25 brand narrative HTML",
            "DB chat Phase 30.3 Backend API merge logic coordination",
            "Phase 30.3 completion -> /api/deep-analysis screen display verification",
            "Phase ζ Agent 2 final completion report",
        ],
    }
    _write_json(audit_dir / "audit_result.json", audit_result)
    _write_text(
        audit_dir / "summary.md",
        f"""# Phase ζ Stage 3-A.7 permanent ai_analysis table insert

Verdict: {verdict}

- Table: `{TARGET_TABLE}`
- Stage marker: `{STAGE_MARKER}`
- Complete 4-stage parsed outputs: {len(parsed_by_brand)}/{expected_count}
- Rows in permanent table for requested brands after insert: {len(verification)}
- Market ID source: {market_id_source}
""",
    )

    script_copy_dir = audit_dir / "code_diff"
    script_copy_dir.mkdir(parents=True, exist_ok=True)
    _write_text(script_copy_dir / "stage3a7_create_and_insert.py", Path(__file__).read_text(encoding="utf-8"))

    zip_path = zip_audit(audit_dir)
    conn.close()
    print(_json_dumps({"audit_dir": str(audit_dir), "zip_path": str(zip_path), "audit_result": audit_result}, indent=2))
    return 0 if verdict in {"PASS", "DRY_RUN"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
