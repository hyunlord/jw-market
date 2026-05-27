#!/usr/bin/env python3
"""Derive competitor cross-match scores from direct JW brand scores."""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pymysql


SOURCE_PROCESSOR = "cross_match_adapter_v1"
DEFAULT_WORKFLOW_ID = 196


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def load_jw25(catalog_path: Path) -> set[str]:
    catalog = read_json(catalog_path)
    if not isinstance(catalog, dict):
        raise ValueError(f"_catalog.json must be an object keyed by JW brand: {catalog_path}")
    return {str(key).strip() for key in catalog.keys() if str(key).strip()}


def score_to_tier(score: int | float) -> str:
    score_int = max(0, min(100, int(round(float(score)))))
    if score_int < 30:
        return "very_weak"
    if score_int < 50:
        return "weak"
    if score_int < 70:
        return "moderate"
    if score_int < 85:
        return "strong"
    return "very_strong"


def now_mysql_utc() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def normalize_contexts(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        value = json.loads(value) if value.strip() else []
    return value if isinstance(value, list) else []


def normalize_score(value: Any) -> int:
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return 0


def derive_cross_match_rows(
    *,
    news_id: str,
    event_id: str,
    contexts: list[dict[str, Any]],
    direct_scores: list[dict[str, Any]],
    jw25: set[str],
    workflow_id: int = DEFAULT_WORKFLOW_ID,
    catalog_version: str | None = None,
    tag: str | None = None,
    summary: str | None = None,
    generated_at: str | None = None,
) -> list[dict[str, Any]]:
    direct_by_brand = {
        str(row["brand_name"]).strip(): normalize_score(row.get("score"))
        for row in direct_scores
        if row.get("brand_name")
    }
    cross_scores: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for context in contexts:
        if not isinstance(context, dict):
            continue
        jw_brand = str(context.get("jw_brand") or "").strip()
        if not jw_brand or jw_brand not in direct_by_brand:
            continue
        jw_score = direct_by_brand[jw_brand]
        for keyword in context.get("matched_keywords") or []:
            keyword_text = str(keyword).strip()
            if not keyword_text or keyword_text == jw_brand or keyword_text in jw25:
                continue
            cross_scores[keyword_text].append({"score": jw_score, "from_jw_brand": jw_brand})

    rows: list[dict[str, Any]] = []
    for keyword in sorted(cross_scores):
        entries = cross_scores[keyword]
        avg_score_raw = sum(entry["score"] for entry in entries) / len(entries)
        avg_score = max(0, min(100, int(math.floor(avg_score_raw + 0.5))))
        mirrored = sorted({entry["from_jw_brand"] for entry in entries})
        rows.append(
            {
                "event_id": event_id,
                "news_id": news_id,
                "brand_name": keyword,
                "brand_canonical": None,
                "brand_id": None,
                "ml_id": None,
                "cd_id": None,
                "is_jw": 0,
                "score": avg_score,
                "score_tier": score_to_tier(avg_score),
                "reason": (
                    "Cross-match: 평균 score = "
                    f"{avg_score_raw:.2f} ({', '.join(mirrored)} 의 점수 mirror average). "
                    "본 약은 검색 키워드 매칭으로 잡혔으나 JW brand 가 아니므로 mirror average 적용."
                ),
                "source_processor": SOURCE_PROCESSOR,
                "generated_at": generated_at or now_mysql_utc(),
                "derivation": "cross_match",
                "mirrored_from_jw_brands": mirrored,
                "tag": tag,
                "summary": summary,
                "workflow_id": workflow_id,
                "catalog_version": catalog_version,
                "llm_meta": json_dumps({}),
            }
        )
    return rows


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


def month_bounds(yyyymm: str) -> tuple[str, str]:
    start = datetime.strptime(yyyymm, "%Y-%m").date()
    if start.month == 12:
        end = date(start.year + 1, 1, 1)
    else:
        end = date(start.year, start.month + 1, 1)
    return start.isoformat(), end.isoformat()


def select_news_ids(cursor: Any, args: argparse.Namespace) -> list[str]:
    if args.news_id:
        return [args.news_id]
    if args.batch:
        start, end = month_bounds(args.batch)
        cursor.execute(
            """
            SELECT news_id
            FROM news_raw
            WHERE scored = 1
              AND published_date >= %s
              AND published_date < %s
            ORDER BY published_date, news_id
            """,
            (start, end),
        )
        return [row["news_id"] for row in cursor.fetchall()]
    if args.all:
        cursor.execute("SELECT news_id FROM news_raw WHERE scored = 1 ORDER BY published_date, news_id")
        return [row["news_id"] for row in cursor.fetchall()]
    raise ValueError("choose one of --news-id, --batch, or --all")


def insert_cross_row(cursor: Any, row: dict[str, Any]) -> None:
    db_row = dict(row)
    db_row["mirrored_from_jw_brands"] = json_dumps(row["mirrored_from_jw_brands"])
    columns = list(db_row.keys())
    placeholders = ", ".join(["%s"] * len(columns))
    column_sql = ", ".join(f"`{column}`" for column in columns)
    cursor.execute(
        f"INSERT INTO event_brand_scores ({column_sql}) VALUES ({placeholders})",
        [db_row[column] for column in columns],
    )


def process_news(cursor: Any, news_id: str, jw25: set[str], *, dry_run: bool) -> dict[str, Any]:
    cursor.execute(
        """
        SELECT n.news_id, n.matched_jw_search_contexts, e.event_id,
               MIN(s.tag) AS tag, MIN(s.summary) AS summary,
               MIN(s.workflow_id) AS workflow_id, MIN(s.catalog_version) AS catalog_version
        FROM news_raw n
        JOIN events e ON e.news_id = n.news_id
        LEFT JOIN event_brand_scores s
          ON s.news_id = n.news_id AND s.derivation = 'llm_direct'
        WHERE n.news_id = %s
        GROUP BY n.news_id, n.matched_jw_search_contexts, e.event_id
        """,
        (news_id,),
    )
    news = cursor.fetchone()
    if not news:
        return {"news_id": news_id, "inserted": 0, "reason": "news_not_found"}

    cursor.execute(
        """
        SELECT brand_name, score
        FROM event_brand_scores
        WHERE news_id = %s AND derivation = 'llm_direct'
        """,
        (news_id,),
    )
    direct_scores = list(cursor.fetchall())
    rows = derive_cross_match_rows(
        news_id=news_id,
        event_id=news["event_id"],
        contexts=normalize_contexts(news["matched_jw_search_contexts"]),
        direct_scores=direct_scores,
        jw25=jw25,
        workflow_id=news.get("workflow_id") or DEFAULT_WORKFLOW_ID,
        catalog_version=news.get("catalog_version"),
        tag=news.get("tag"),
        summary=news.get("summary"),
    )
    if dry_run:
        return {"news_id": news_id, "planned": len(rows), "inserted": 0}

    cursor.execute(
        "DELETE FROM event_brand_scores WHERE news_id = %s AND derivation = 'cross_match'",
        (news_id,),
    )
    for row in rows:
        insert_cross_row(cursor, row)
    return {"news_id": news_id, "planned": len(rows), "inserted": len(rows)}


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()
    jw25 = load_jw25(args.catalog.expanduser())
    result: dict[str, Any] = {
        "started_at": started.isoformat(),
        "dry_run": bool(args.dry_run),
        "news_seen": 0,
        "event_brand_scores_cross_match": 0,
        "error_count": 0,
        "errors": [],
    }
    conn = connect(args)
    try:
        with conn.cursor() as cursor:
            news_ids = select_news_ids(cursor, args)
        result["news_seen"] = len(news_ids)
        for news_id in news_ids:
            try:
                with conn.cursor() as cursor:
                    item = process_news(cursor, news_id, jw25, dry_run=args.dry_run)
                if args.dry_run:
                    conn.rollback()
                else:
                    conn.commit()
                result["event_brand_scores_cross_match"] += item.get("planned", 0)
            except Exception as exc:
                conn.rollback()
                result["error_count"] += 1
                result["errors"].append({"news_id": news_id, "error": str(exc)})
    finally:
        conn.close()
    result["ended_at"] = datetime.now(timezone.utc).isoformat()
    result["elapsed_seconds"] = round(time.monotonic() - started_monotonic, 2)
    result["verdict"] = "passed" if result["error_count"] == 0 else "partial"
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--news-id")
    target.add_argument("--batch", help="YYYY-MM")
    target.add_argument("--all", action="store_true")
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--db-host", default=os.getenv("DB_HOST", "127.0.0.1"))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("DB_PORT", "3308")))
    parser.add_argument("--db-user", default=os.getenv("DB_USER", "root"))
    parser.add_argument("--db-password", default=os.getenv("DB_PASSWORD", ""))
    parser.add_argument("--db-name", default=os.getenv("DB_NAME", "jw_mart"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run(args)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["error_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
