#!/usr/bin/env python3
"""Dry-run and load processed GCP news corpus into Agent 2 event tables."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pymysql


CATEGORY_MAPPING = {
    "신약/R&D": ("rd", "신약/R&D"),
    "정책/규제": ("policy", "정책/규제"),
    "공급/생산": ("supply", "공급/생산"),
    "자본/경영": ("capital", "자본/경영"),
    "외부/트렌드": ("external", "외부/트렌드"),
}


def tag_to_code(tag: Any) -> tuple[str, str]:
    if tag is None:
        return "other", "기타"
    return CATEGORY_MAPPING.get(str(tag), ("other", "기타"))


def score_to_tier(score: int) -> str:
    if score < 10:
        return "excluded"
    if score < 20:
        return "very_weak"
    if score < 30:
        return "side_mention"
    if score < 40:
        return "incidental"
    if score < 50:
        return "list_item"
    if score < 60:
        return "brief_paragraph"
    if score < 70:
        return "solo_coverage"
    if score < 80:
        return "deep_analysis"
    if score < 90:
        return "big_news"
    if score < 95:
        return "global_event"
    return "official_decision"


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value)[:10]
    for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def period_iqvia(value: date | None) -> str | None:
    if value is None:
        return None
    quarter = (value.month - 1) // 3 + 1
    return f"{value.year}Q{quarter}"


def period_ubist(value: date | None) -> str | None:
    return value.strftime("%Y-%m") if value else None


def media_name(path: Path) -> str:
    return path.parent.name.replace("news_5years_", "").replace("_processed", "")


def news_id(path: Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]


def source_url(item: dict[str, Any]) -> str | None:
    sources = item.get("sources") or []
    if sources and isinstance(sources[0], dict):
        return sources[0].get("url")
    return None


def processed_files(corpus: Path) -> list[Path]:
    files = set(corpus.glob("*_processed/*.json"))
    files.update(corpus.glob("*_scored/*.json"))
    files.update(corpus.glob("*.json"))
    return sorted(
        path
        for path in files
        if "report" not in path.name.lower()
        and path.name not in {"scraped_urls.json", "tier2_brand_plan.json"}
    )


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


@dataclass(frozen=True)
class BrandResolution:
    brand_canonical: str | None
    brand_id: str | None
    ml_id: str | None
    cd_id: str | None
    is_jw: int


class BrandResolver:
    def __init__(self, strategic_brand_path: Path, cd_brand_path: Path | None = None) -> None:
        frames = [self._load_catalog_frame(strategic_brand_path)]
        if cd_brand_path and cd_brand_path.exists():
            frames.append(self._load_catalog_frame(cd_brand_path))
        catalog = pd.concat([frame for frame in frames if not frame.empty], ignore_index=True)
        self._lookup: dict[str, BrandResolution] = {}
        for _, row in catalog.iterrows():
            resolution = BrandResolution(
                brand_canonical=self._clean(row.get("name")),
                brand_id=self._clean(row.get("brand_id")),
                ml_id=self._clean(row.get("ml_id")),
                cd_id=self._clean(row.get("cd_id")),
                is_jw=int(bool(row.get("is_jw"))),
            )
            for column in ["name", "merge_name", "canonical_name", "general_brand_key"]:
                key = self._clean(row.get(column))
                if key and key not in self._lookup:
                    self._lookup[key] = resolution

    @staticmethod
    def _clean(value: Any) -> str | None:
        if value is None or pd.isna(value):
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _load_catalog_frame(path: Path) -> pd.DataFrame:
        if path.suffix.lower() == ".parquet":
            return pd.read_parquet(path)
        if path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            rows: list[dict[str, Any]] = []
            if isinstance(data, dict):
                for key, value in data.items():
                    row = value if isinstance(value, dict) else {}
                    rows.append(
                        {
                            "name": row.get("name") or row.get("brand_name") or key,
                            "brand_id": row.get("brand_id"),
                            "ml_id": row.get("ml_id"),
                            "cd_id": row.get("cd_id"),
                            "is_jw": row.get("is_jw", True),
                        }
                    )
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        rows.append(item)
                    else:
                        rows.append({"name": item, "is_jw": True})
            return pd.DataFrame(rows)
        return pd.DataFrame()

    def resolve(self, drug_name: Any) -> BrandResolution:
        key = self._clean(drug_name)
        if not key:
            return BrandResolution(None, None, None, None, 0)
        return self._lookup.get(key, BrandResolution(None, None, None, None, 0))


def build_rows(
    path: Path,
    item: dict[str, Any],
    resolver: BrandResolver,
    *,
    processed_by: str = "corpus_v1",
    tier: int = 1,
    collected_at: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    nid = news_id(path)
    published = parse_date(item.get("date"))
    url = source_url(item)
    category, category_label = tag_to_code(item.get("tag"))
    collected = (collected_at or datetime.now()).isoformat(sep=" ", timespec="seconds")
    news = {
        "news_id": nid,
        "source_name": media_name(path),
        "title": item.get("title") or "",
        "article_url": url,
        "article_text": item.get("content"),
        "raw_html": None,
        "published_date": published.isoformat() if published else None,
        "search_keyword": item.get("search_keyword"),
        "corpus_file_path": str(path),
        "tier": tier,
        "collected_at": collected,
    }
    event = {
        "event_id": nid,
        "news_id": nid,
        "category": category,
        "category_label": category_label,
        "date": published.isoformat() if published else None,
        "title": item.get("title"),
        "summary": item.get("summary"),
        "body_full": item.get("content"),
        "source_name": media_name(path),
        "source_url": url,
        "period_ubist": period_ubist(published),
        "period_iqvia": period_iqvia(published),
        "processed_by": processed_by,
        "search_keyword": item.get("search_keyword"),
        "tier": tier,
        "collected_at": collected,
    }
    scores = []
    seen_keys: set[tuple[str, str | None]] = set()
    for match in item.get("matches", []) or []:
        if not isinstance(match, dict):
            continue
        resolution = resolver.resolve(match.get("drug"))
        brand_canonical = resolution.brand_canonical or BrandResolver._clean(
            match.get("brand_canonical") or match.get("brand_key")
        )
        brand_id = resolution.brand_id or BrandResolver._clean(match.get("brand_id"))
        ml_id = resolution.ml_id or BrandResolver._clean(match.get("ml_id"))
        cd_id = resolution.cd_id or BrandResolver._clean(match.get("cd_id"))
        is_jw = resolution.is_jw or int(bool(match.get("is_jw") or 0))
        try:
            score = int(match.get("score") or 0)
        except Exception:
            score = 0
        score = max(0, min(100, score))
        unique_key = (nid, brand_canonical or f"unknown::{match.get('drug')}")
        if unique_key in seen_keys:
            continue
        seen_keys.add(unique_key)
        scores.append(
            {
                "event_id": nid,
                "brand_name": match.get("drug"),
                "brand_canonical": brand_canonical,
                "brand_id": brand_id,
                "ml_id": ml_id,
                "cd_id": cd_id,
                "is_jw": is_jw,
                "score": score,
                "score_tier": score_to_tier(score),
                "reason": match.get("reason"),
                "source_processor": processed_by,
                "tier": tier,
                "collected_at": collected,
            }
        )
    return news, event, scores


def summarize(
    corpus: Path,
    resolver: BrandResolver,
    *,
    sample_limit: int = 5,
    processed_by: str = "corpus_v1",
    tier: int = 1,
    collected_at: datetime | None = None,
) -> dict[str, Any]:
    files = processed_files(corpus)
    media = collections.Counter()
    categories = collections.Counter()
    score_tiers = collections.Counter()
    unknown = collections.Counter()
    jw_events = collections.Counter()
    errors: list[dict[str, Any]] = []
    row_samples: list[dict[str, Any]] = []
    news_count = 0
    event_count = 0
    score_count = 0
    matched_score_count = 0

    for path in files:
        try:
            item = load_json(path)
            news, event, scores = build_rows(
                path,
                item,
                resolver,
                processed_by=processed_by,
                tier=tier,
                collected_at=collected_at,
            )
        except Exception as exc:
            errors.append({"path": str(path), "error": str(exc)})
            continue
        news_count += 1
        event_count += 1
        score_count += len(scores)
        media[news["source_name"]] += 1
        categories[(event["category"], event["category_label"])] += 1
        for score in scores:
            score_tiers[score["score_tier"]] += 1
            if score["brand_canonical"]:
                matched_score_count += 1
            else:
                unknown[str(score["brand_name"])] += 1
            if score["is_jw"]:
                jw_events[str(score["brand_canonical"])] += 1
        if len(row_samples) < sample_limit:
            row_samples.append({"news": news, "event": event, "scores": scores[:5]})

    return {
        "dry_run": True,
        "corpus": str(corpus),
        "processed_json_found": len(files),
        "news_raw_rows_planned": news_count,
        "events_rows_planned": event_count,
        "event_brand_scores_rows_planned": score_count,
        "brand_catalog_matched_scores": matched_score_count,
        "brand_catalog_match_pct": round(matched_score_count / max(score_count, 1) * 100, 2),
        "unknown_brand_unique": len(unknown),
        "unknown_brand_top30": unknown.most_common(30),
        "media_distribution": dict(media),
        "category_distribution": {f"{k[0]}|{k[1]}": v for k, v in categories.items()},
        "score_tier_distribution": dict(score_tiers),
        "jw_brand_event_distribution_top50": jw_events.most_common(50),
        "error_count": len(errors),
        "errors": errors[:20],
        "row_samples": row_samples,
    }


def insert_ignore(cursor: Any, table: str, row: dict[str, Any]) -> int:
    columns = list(row.keys())
    placeholders = ", ".join(["%s"] * len(columns))
    column_sql = ", ".join(f"`{column}`" for column in columns)
    sql = f"INSERT IGNORE INTO {table} ({column_sql}) VALUES ({placeholders})"
    cursor.execute(sql, [row[column] for column in columns])
    return int(cursor.rowcount or 0)


def score_exists(cursor: Any, score: dict[str, Any]) -> bool:
    if score.get("brand_canonical") is None:
        cursor.execute(
            """
            SELECT 1 FROM event_brand_scores
            WHERE event_id = %s AND brand_name = %s AND brand_canonical IS NULL
            LIMIT 1
            """,
            (score["event_id"], score["brand_name"]),
        )
    else:
        cursor.execute(
            """
            SELECT 1 FROM event_brand_scores
            WHERE event_id = %s AND brand_canonical = %s
            LIMIT 1
            """,
            (score["event_id"], score["brand_canonical"]),
        )
    return cursor.fetchone() is not None


def load_to_db(
    corpus: Path,
    resolver: BrandResolver,
    *,
    db_host: str,
    db_port: int,
    db_user: str,
    db_password: str,
    db_name: str,
    batch_size: int,
    processed_by: str,
    tier: int,
    collected_at: datetime | None,
) -> dict[str, Any]:
    started = datetime.now()
    started_monotonic = time.monotonic()
    files = processed_files(corpus)
    conn = pymysql.connect(
        host=db_host,
        port=db_port,
        user=db_user,
        password=db_password,
        database=db_name,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
    inserted_news = 0
    inserted_events = 0
    inserted_scores = 0
    planned_scores = 0
    errors: list[dict[str, Any]] = []
    media = collections.Counter()
    categories = collections.Counter()
    score_tiers = collections.Counter()
    unknown = collections.Counter()
    matched_score_count = 0

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO agent_run_log
                  (agent_name, agent_version, started_at, status, input_count, output_count,
                   skipped_count, error_count, notes)
                VALUES
                  (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    "corpus_loader",
                    "v1",
                    started,
                    "running",
                    len(files),
                    0,
                    0,
                    0,
                    f"corpus={corpus}; processed_by={processed_by}",
                ),
            )
            run_id = cursor.lastrowid
            conn.commit()

            for index, path in enumerate(files, start=1):
                try:
                    item = load_json(path)
                    news, event, scores = build_rows(
                        path,
                        item,
                        resolver,
                        processed_by=processed_by,
                        tier=tier,
                        collected_at=collected_at,
                    )
                    inserted_news += insert_ignore(cursor, "news_raw", news)
                    inserted_events += insert_ignore(cursor, "events", event)
                    media[news["source_name"]] += 1
                    categories[(event["category"], event["category_label"])] += 1
                    for score in scores:
                        planned_scores += 1
                        score_tiers[score["score_tier"]] += 1
                        if score["brand_canonical"]:
                            matched_score_count += 1
                        else:
                            unknown[str(score["brand_name"])] += 1
                        if not score_exists(cursor, score):
                            inserted_scores += insert_ignore(
                                cursor, "event_brand_scores", score
                            )
                except Exception as exc:
                    errors.append({"path": str(path), "error": str(exc)})
                if index % batch_size == 0:
                    conn.commit()

            finished = datetime.now()
            status = "success" if not errors else "partial"
            cursor.execute(
                """
                UPDATE agent_run_log
                SET finished_at = %s, status = %s, output_count = %s,
                    skipped_count = %s, error_count = %s
                WHERE run_id = %s
                """,
                (
                    finished,
                    status,
                    inserted_events,
                    len(files) - inserted_events,
                    len(errors),
                    run_id,
                ),
            )
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    elapsed = round(time.monotonic() - started_monotonic, 2)
    return {
        "dry_run": False,
        "run_id": run_id,
        "corpus": str(corpus),
        "processed_json_found": len(files),
        "news_raw_rows_inserted": inserted_news,
        "events_rows_inserted": inserted_events,
        "event_brand_scores_rows_planned": planned_scores,
        "event_brand_scores_rows_inserted": inserted_scores,
        "brand_catalog_matched_scores": matched_score_count,
        "brand_catalog_match_pct": round(matched_score_count / max(planned_scores, 1) * 100, 2),
        "unknown_brand_unique": len(unknown),
        "unknown_brand_top30": unknown.most_common(30),
        "media_distribution": dict(media),
        "category_distribution": {f"{k[0]}|{k[1]}": v for k, v in categories.items()},
        "score_tier_distribution": dict(score_tiers),
        "error_count": len(errors),
        "errors": errors[:20],
        "elapsed_seconds": elapsed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--cd-catalog", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true", help="Validate and summarize without DB writes.")
    parser.add_argument("--db-host", default=os.getenv("DB_HOST", "127.0.0.1"))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("DB_PORT", "3308")))
    parser.add_argument("--db-name", default=os.getenv("DB_NAME", "jw_mart_d2_stage_20260630_r2"))
    parser.add_argument("--db-user", default=os.getenv("DB_USER", "root"))
    parser.add_argument("--db-password", default=os.getenv("DB_PASSWORD", ""))
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--processed-by", default="corpus_v1")
    parser.add_argument("--tier", type=int, choices=(1, 2), default=1)
    parser.add_argument(
        "--collected-at",
        default="",
        help="Optional collection timestamp, ISO format. Defaults to current time.",
    )
    args = parser.parse_args()

    collected_at = datetime.fromisoformat(args.collected_at) if args.collected_at else None
    resolver = BrandResolver(args.catalog, args.cd_catalog)
    if args.dry_run:
        result = summarize(
            args.corpus,
            resolver,
            processed_by=args.processed_by,
            tier=args.tier,
            collected_at=collected_at,
        )
    else:
        result = load_to_db(
            args.corpus,
            resolver,
            db_host=args.db_host,
            db_port=args.db_port,
            db_user=args.db_user,
            db_password=args.db_password,
            db_name=args.db_name,
            batch_size=args.batch_size,
            processed_by=args.processed_by,
            tier=args.tier,
            collected_at=collected_at,
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_keys = [
        "processed_json_found",
        "news_raw_rows_planned",
        "events_rows_planned",
        "news_raw_rows_inserted",
        "events_rows_inserted",
        "event_brand_scores_rows_planned",
        "event_brand_scores_rows_inserted",
        "brand_catalog_match_pct",
        "unknown_brand_unique",
        "error_count",
    ]
    print(json.dumps({k: result[k] for k in summary_keys if k in result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
