from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Final

import pymysql

BACKUP_TABLE: Final = "event_brand_scores_bak_v3rescore_20260704_pre5347"
CUTOFF_UTC: Final = "2026-07-03 19:44:00"
OUT_DIR: Final = Path("/tmp/wf196_v3_chunks")
CHUNK_SIZE: Final = 1000
EXPECTED_BACKUP_HASH: Final = "95763c85971dfe986641ee302f808b5b082e33e220b5fddac71548ed394c347e"
EXPECTED_CROSS_HASH: Final = "b536e53f03da18743a670de12186daa4121452128423c0f26e736752b7c6e547"
COLS: Final = "id,event_id,brand_name,brand_canonical,brand_id,ml_id,cd_id,is_jw,score,score_tier,reason,source_processor,generated_at,news_id,derivation,mirrored_from_jw_brands,tag,summary,workflow_id,catalog_version,llm_meta,tier,collected_at,expire_at"


def chunk_index() -> int:
    return int(os.environ["CHUNK_INDEX"])


def result_path() -> Path:
    return OUT_DIR / f"chunk_{chunk_index():02d}_results.jsonl"


def summary_path() -> Path:
    return OUT_DIR / f"chunk_{chunk_index():02d}_verify.json"


def connect_db():
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database="jw_mart",
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def rows_hash(cursor, sql: str) -> dict:
    digest = hashlib.sha256()
    count = 0
    cursor.execute(sql)
    while True:
        rows = cursor.fetchmany(1000)
        if not rows:
            break
        for row in rows:
            count += 1
            digest.update(("\t".join(str(row.get(key, "")) for key in row.keys()) + "\n").encode("utf-8", "surrogatepass"))
    return {"rows": count, "sha256": digest.hexdigest()}


def load_results() -> dict[int, dict]:
    with result_path().open(encoding="utf-8") as handle:
        return {int(item["row_id"]): item for line in handle if line.strip() for item in [json.loads(line)]}


def ids_sql(ids: set[int]) -> str:
    return ",".join(str(row_id) for row_id in sorted(ids))


def verify_live_rows(cursor, results: dict[int, dict]) -> list[dict]:
    cursor.execute(f"SELECT id, score, score_tier, reason, tag, summary FROM event_brand_scores WHERE id IN ({ids_sql(set(results))}) ORDER BY id")
    live_rows = {int(row["id"]): row for row in cursor.fetchall()}
    mismatches = []
    for row_id, expected in results.items():
        live = live_rows.get(row_id)
        if live is None:
            mismatches.append({"row_id": row_id, "field": "missing_live"})
            continue
        for field in ["score", "score_tier", "reason", "tag", "summary"]:
            if live.get(field) != expected.get(field):
                mismatches.append({"row_id": row_id, "field": field, "live": live.get(field), "expected": expected.get(field)})
    return mismatches[:50]


def future_hash(cursor, table_name: str, alias: str, backup_join: bool = False) -> dict:
    offset = (chunk_index() + 1) * CHUNK_SIZE
    cols = ",".join(f"{alias}.{col}" for col in COLS.split(","))
    join_backup = f"JOIN {BACKUP_TABLE} target_backup ON target_backup.id = {alias}.id" if backup_join else ""
    live_filter = (
        f"WHERE {alias}.source_processor='workflow_196_optionB' "
        f"AND {alias}.derivation='llm_direct' AND {alias}.tier=1 "
        f"AND {alias}.generated_at < '{CUTOFF_UTC}'"
        if table_name == "event_brand_scores"
        else ""
    )
    return rows_hash(
        cursor,
        f"""
        SELECT {cols} FROM {table_name} {alias}
        JOIN (SELECT news_id FROM {BACKUP_TABLE} GROUP BY news_id ORDER BY SHA2(news_id, 256), news_id LIMIT 100000 OFFSET {offset}) future
          ON future.news_id = {alias}.news_id
        {join_backup}
        {live_filter}
        ORDER BY {alias}.id
        """,
    )


def main() -> int:
    results = load_results()
    conn = connect_db()
    try:
        with conn.cursor() as cursor:
            mismatches = verify_live_rows(cursor, results)
            backup_hash = rows_hash(cursor, f"SELECT {COLS} FROM {BACKUP_TABLE} ORDER BY id")
            cross_hash = rows_hash(cursor, "SELECT id,news_id,brand_canonical,score,reason,tag,summary,source_processor,derivation,tier,generated_at FROM event_brand_scores WHERE source_processor='cross_match_adapter_v1' OR derivation='cross_match' ORDER BY id")
            future_live = future_hash(cursor, "event_brand_scores", "e", backup_join=True)
            future_backup = future_hash(cursor, BACKUP_TABLE, "b")
            cursor.execute(f"SELECT COUNT(*) rows_cnt, COUNT(DISTINCT news_id) news_cnt, SUM(score>=50) ge50_rows FROM event_brand_scores WHERE id IN ({ids_sql(set(results))})")
            chunk_counts = cursor.fetchone()
            cursor.execute(f"SELECT COUNT(*) rows_cnt, COUNT(DISTINCT news_id) news_cnt, SUM(score>=50) ge50_rows FROM event_brand_scores WHERE source_processor='workflow_196_optionB' AND derivation='llm_direct' AND tier=1 AND generated_at < '{CUTOFF_UTC}'")
            target_counts = cursor.fetchone()
    finally:
        conn.close()
    output = {
        "chunk": chunk_index(),
        "checkpoint_rows": len(results),
        "checkpoint_news": len({u["news_id"] for u in results.values()}),
        "live_matches_checkpoint": not mismatches,
        "live_mismatches_sample": mismatches,
        "backup_intact": backup_hash["sha256"] == EXPECTED_BACKUP_HASH,
        "backup_hash": backup_hash,
        "cross_match_unchanged": cross_hash["sha256"] == EXPECTED_CROSS_HASH,
        "cross_match_hash": cross_hash,
        "future_unprocessed_unchanged": future_live == future_backup,
        "future_live_hash": future_live,
        "future_backup_hash": future_backup,
        "chunk_counts_after_update": chunk_counts,
        "target_counts_after_chunk": target_counts,
    }
    summary_path().write_text(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print("VERIFY_SUMMARY=" + json.dumps(output, ensure_ascii=False, sort_keys=True, default=str), flush=True)
    if not output["live_matches_checkpoint"] or not output["backup_intact"] or not output["cross_match_unchanged"] or not output["future_unprocessed_unchanged"]:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
