from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Final

import pymysql
from pymysql.constants import CLIENT

BACKUP_TABLE: Final = "event_brand_scores_bak_v3rescore_20260704_pre5347"
CUTOFF_UTC: Final = "2026-07-03 19:44:00"
OUT_DIR: Final = Path("/tmp/wf196_v3_chunks")
BATCH_SIZE: Final = 200
VALID_TAGS: Final = {"신약/R&D", "정책/규제", "공급/생산", "자본/경영", "외부/트렌드", "기타"}


def chunk_index() -> int:
    return int(os.environ["CHUNK_INDEX"])


def result_path() -> Path:
    return OUT_DIR / f"chunk_{chunk_index():02d}_results.jsonl"


def summary_path() -> Path:
    return OUT_DIR / f"chunk_{chunk_index():02d}_apply_summary.json"


def connect_db():
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ.get("DB_NAME", "jw_mart_d2_stage_20260630_r2"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        client_flag=CLIENT.FOUND_ROWS,
        autocommit=False,
    )


def load_updates() -> list[dict]:
    with result_path().open(encoding="utf-8") as handle:
        updates = [json.loads(line) for line in handle if line.strip()]
    bad_tags = sorted({str(update["tag"]) for update in updates if update.get("tag") not in VALID_TAGS})
    if bad_tags:
        raise ValueError(f"invalid tags: {bad_tags}")
    if not updates:
        raise ValueError(f"empty checkpoint for chunk {chunk_index()}")
    return updates


def apply_updates(conn, updates: list[dict]) -> int:
    sql = f"""
        UPDATE event_brand_scores e
        SET e.score=%s, e.score_tier=%s, e.reason=%s, e.tag=%s, e.summary=%s, e.llm_meta=%s
        WHERE e.id=%s AND e.source_processor='workflow_196_optionB'
          AND e.derivation='llm_direct' AND e.tier=1 AND e.generated_at < %s
          AND EXISTS (SELECT 1 FROM {BACKUP_TABLE} b WHERE b.id=e.id)
    """
    total = 0
    with conn.cursor() as cursor:
        for index in range(0, len(updates), BATCH_SIZE):
            batch = updates[index:index + BATCH_SIZE]
            params = [(u["score"], u["score_tier"], u["reason"], u["tag"], u["summary"], u["llm_meta"], u["row_id"], CUTOFF_UTC) for u in batch]
            total += int(cursor.executemany(sql, params))
    return total


def main() -> int:
    updates = load_updates()
    conn = connect_db()
    try:
        affected = apply_updates(conn, updates)
        if affected != len(updates):
            conn.rollback()
            raise ValueError(f"affected row mismatch: {affected} expected={len(updates)}")
        conn.commit()
    finally:
        conn.close()
    summary = {"chunk": chunk_index(), "checkpoint_news": len({u["news_id"] for u in updates}), "checkpoint_rows": len(updates), "updated_rows": affected, "tag_violations": 0}
    summary_path().write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print("APPLY_SUMMARY=" + json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
