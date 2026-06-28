#!/usr/bin/env python3
"""Load workflow 196 option-B scored news into the local Agent 2 corpus tables."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pymysql


SOURCE_PROCESSOR = "workflow_196_optionB"
DEFAULT_WORKFLOW_ID = 196

TAG_TO_CATEGORY = {
    "신약/R&D": {"category": "rd", "category_label": "신약/R&D"},
    "정책/규제": {"category": "policy", "category_label": "정책/규제"},
    "공급/생산": {"category": "supply", "category_label": "공급/생산"},
    "자본/경영": {"category": "capital", "category_label": "자본/경영"},
    "외부/트렌드": {"category": "external", "category_label": "외부/트렌드"},
    "기타": {"category": "external", "category_label": "기타"},
}


@dataclass(frozen=True)
class BrandResolution:
    brand_canonical: str | None
    brand_id: str | None
    ml_id: str | None
    cd_id: str | None
    is_jw: int


@dataclass(frozen=True)
class BuiltRecords:
    news: dict[str, Any]
    event: dict[str, Any]
    scores: list[dict[str, Any]]
    scored_path: Path
    source_path: Path


class CatalogResolver:
    """Resolve workflow brand names against the JW25 catalog and optional marts."""

    def __init__(
        self,
        catalog: dict[str, Any],
        *,
        catalog_version: str | None = None,
        brand_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.catalog = catalog
        self.catalog_version = catalog_version
        self.jw25 = {self._clean(key) for key in catalog.keys() if self._clean(key)}
        self._lookup: dict[str, BrandResolution] = {}
        for brand in sorted(self.jw25):
            self._lookup[brand] = BrandResolution(brand, None, None, None, 1)
        for row in brand_rows or []:
            self._add_brand_row(row)

    @staticmethod
    def _clean(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _add_brand_row(self, row: dict[str, Any]) -> None:
        name = self._clean(row.get("name") or row.get("brand_name") or row.get("canonical_name"))
        canonical = self._clean(row.get("canonical_name") or row.get("name") or row.get("brand_name"))
        if not canonical:
            return
        resolution = BrandResolution(
            brand_canonical=canonical,
            brand_id=self._clean(row.get("brand_id")),
            ml_id=self._clean(row.get("ml_id")),
            cd_id=self._clean(row.get("cd_id")),
            is_jw=int(bool(row.get("is_jw"))) if row.get("is_jw") is not None else int(canonical in self.jw25),
        )
        for column in ("name", "brand_name", "merge_name", "canonical_name", "general_brand_key"):
            key = self._clean(row.get(column))
            if key:
                self._lookup[key] = resolution
        if name:
            self._lookup[name] = resolution

    def resolve(self, brand_name: Any) -> BrandResolution:
        key = self._clean(brand_name)
        if not key:
            return BrandResolution(None, None, None, None, 0)
        if key in self._lookup:
            return self._lookup[key]
        return BrandResolution(None, None, None, None, 0)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def load_catalog(path: Path, *, parquet_paths: list[Path] | None = None) -> CatalogResolver:
    raw = path.read_text(encoding="utf-8")
    catalog = json.loads(raw)
    if not isinstance(catalog, dict):
        raise ValueError(f"_catalog.json must be an object keyed by JW brand: {path}")
    rows = load_brand_rows(parquet_paths or [])
    return CatalogResolver(catalog, catalog_version=hashlib.sha1(raw.encode("utf-8")).hexdigest(), brand_rows=rows)


def load_brand_rows(paths: list[Path]) -> list[dict[str, Any]]:
    existing = [path for path in paths if path and path.exists()]
    if not existing:
        return []
    try:
        import pandas as pd  # type: ignore
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for path in existing:
        frame = pd.read_parquet(path)
        rows.extend(frame.to_dict("records"))
    return rows


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


def tag_to_category(tag: Any) -> dict[str, str]:
    if tag in TAG_TO_CATEGORY:
        return TAG_TO_CATEGORY[str(tag)]
    label = str(tag).strip() if tag else "기타"
    return {"category": "external", "category_label": label or "기타"}


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    text = str(value).strip()[:10]
    for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def mysql_datetime(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return str(value)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def now_mysql_utc() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def period_ubist(value: date | None) -> str | None:
    return value.strftime("%Y-%m") if value else None


def period_iqvia(value: date | None) -> str | None:
    if value is None:
        return None
    quarter = (value.month - 1) // 3 + 1
    return f"{value.year}Q{quarter}"


def first_source(news: dict[str, Any]) -> dict[str, Any]:
    sources = news.get("sources") or []
    if sources and isinstance(sources[0], dict):
        return sources[0]
    return {}


def source_name_from_path(path: Path) -> str:
    parent = path.parent.name
    if parent.startswith("news_5years_"):
        return parent.replace("news_5years_", "", 1)
    return parent or "unknown"


def logical_source_key(scored: dict[str, Any], source_path: Path, news: dict[str, Any]) -> str:
    for key in ("source_path", "batch_path"):
        value = scored.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    source = first_source(news)
    url = source.get("url") or news.get("article_url") or news.get("url")
    if url:
        return str(url)
    return f"{news.get('title', '')}|{news.get('date', '')}|{source_path.name}"


def generate_news_id(news: dict[str, Any], source_path: Path, scored: dict[str, Any]) -> str:
    explicit = scored.get("news_id") or news.get("news_id")
    if isinstance(explicit, str) and re.fullmatch(r"[0-9a-fA-F]{16}", explicit.strip()):
        return explicit.strip().lower()
    return hashlib.sha256(logical_source_key(scored, source_path, news).encode("utf-8")).hexdigest()[:16]


def scored_files(batch_dir: Path, scored_dir: Path | None = None) -> list[Path]:
    root = scored_dir or batch_dir / "_scored"
    if not root.exists():
        root = batch_dir
    return sorted(
        path
        for path in root.rglob("*_scored.json")
        if path.is_file() and path.name != "_log.json"
    )


def candidate_under(batch_dir: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(value).expanduser()
    candidates = [candidate] if candidate.is_absolute() else [batch_dir / candidate]
    for path in candidates:
        if path.exists() and path.is_file():
            return path
    return None


def resolve_news_path(batch_dir: Path, scored_path: Path, scored: dict[str, Any]) -> Path:
    for key in ("source_path", "batch_path"):
        candidate = candidate_under(batch_dir, scored.get(key))
        if candidate:
            return candidate
    stem = scored_path.name.removesuffix("_scored.json")
    matches = [
        path
        for path in batch_dir.rglob(f"{stem}.json")
        if "_scored" not in path.parts and path.name != "_catalog.json"
    ]
    if matches:
        return sorted(matches)[0]
    raise FileNotFoundError(f"could not resolve source news JSON for scored file: {scored_path}")


def normalize_score(value: Any) -> int:
    try:
        score = float(value)
    except (TypeError, ValueError):
        score = 0.0
    return max(0, min(100, int(round(score))))


def build_records(
    batch_dir: Path,
    scored_path: Path,
    catalog: CatalogResolver,
    *,
    workflow_id: int,
) -> BuiltRecords:
    scored = read_json(scored_path)
    if not isinstance(scored, dict):
        raise ValueError(f"scored JSON must be an object: {scored_path}")
    source_path = resolve_news_path(batch_dir, scored_path, scored)
    news = read_json(source_path)
    if not isinstance(news, dict):
        raise ValueError(f"source news JSON must be an object: {source_path}")

    source = first_source(news)
    published = parse_date(news.get("date") or news.get("published_date"))
    scored_at = mysql_datetime(scored.get("scored_at") or news.get("scored_at"))
    news_id = generate_news_id(news, source_path, scored)
    category = tag_to_category(scored.get("tag"))
    url = source.get("url") or news.get("article_url") or news.get("url")
    source_name = source.get("source") or news.get("source_name") or news.get("crawl_site") or source_name_from_path(source_path)
    content = news.get("content") or news.get("article_text") or ""

    news_row = {
        "news_id": news_id,
        "source_name": source_name,
        "title": news.get("title") or "",
        "article_url": url,
        "article_text": content,
        "published_date": published.isoformat() if published else None,
        "search_keyword": news.get("search_keyword"),
        "ingested_at": now_mysql_utc(),
        "matched_search_keywords": json_dumps(news.get("matched_search_keywords") or []),
        "matched_jw_search_contexts": json_dumps(news.get("matched_jw_search_contexts") or []),
        "news_source_file": str(source_path),
        "scored": 1,
        "scored_at": scored_at,
        "corpus_file_path": str(source_path),
    }
    event_row = {
        "event_id": news_id,
        "news_id": news_id,
        "category": category["category"],
        "category_label": category["category_label"],
        "date": published.isoformat() if published else None,
        "title": news.get("title") or "",
        "summary": scored.get("summary") or "",
        "body_full": content,
        "source_name": source_name,
        "source_url": url,
        "period_ubist": period_ubist(published),
        "period_iqvia": period_iqvia(published),
        "processed_by": SOURCE_PROCESSOR,
        "processed_at": scored_at,
        "search_keyword": news.get("search_keyword"),
    }

    scores: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in scored.get("matches") or []:
        if not isinstance(match, dict):
            continue
        brand = match.get("drug") or match.get("jw_brand") or match.get("brand") or match.get("brand_name")
        brand = CatalogResolver._clean(brand)
        if not brand:
            continue
        resolution = catalog.resolve(brand)
        canonical_key = resolution.brand_canonical or brand
        if canonical_key in seen:
            continue
        seen.add(canonical_key)
        score = normalize_score(match.get("score", match.get("importance", 0)))
        scores.append(
            {
                "event_id": news_id,
                "news_id": news_id,
                "brand_name": brand,
                "brand_canonical": resolution.brand_canonical or brand,
                "brand_id": resolution.brand_id,
                "ml_id": resolution.ml_id,
                "cd_id": resolution.cd_id,
                "is_jw": 1,
                "score": score,
                "score_tier": score_to_tier(score),
                "reason": match.get("reason") or "",
                "derivation": "llm_direct",
                "mirrored_from_jw_brands": None,
                "tag": scored.get("tag"),
                "summary": scored.get("summary") or "",
                "workflow_id": scored.get("workflow_id") or workflow_id,
                "catalog_version": scored.get("catalog_version") or catalog.catalog_version,
                "llm_meta": json_dumps(scored.get("llm_meta") or {}),
                "source_processor": SOURCE_PROCESSOR,
                "generated_at": scored_at or now_mysql_utc(),
            }
        )
    return BuiltRecords(news=news_row, event=event_row, scores=scores, scored_path=scored_path, source_path=source_path)


def execute_insert(cursor: Any, table: str, row: dict[str, Any], update_columns: list[str] | None = None) -> int:
    columns = list(row.keys())
    placeholders = ", ".join(["%s"] * len(columns))
    column_sql = ", ".join(f"`{column}`" for column in columns)
    sql = f"INSERT INTO `{table}` ({column_sql}) VALUES ({placeholders})"
    if update_columns:
        updates = ", ".join(f"`{column}` = VALUES(`{column}`)" for column in update_columns)
        sql = f"{sql} ON DUPLICATE KEY UPDATE {updates}"
    cursor.execute(sql, [row[column] for column in columns])
    return int(cursor.rowcount or 0)


def load_records(cursor: Any, records: BuiltRecords, *, dry_run: bool) -> dict[str, int]:
    if dry_run:
        return {"news_written": 0, "events_written": 0, "scores_written": 0}
    cursor.execute("SELECT 1 FROM news_raw WHERE news_id = %s LIMIT 1", (records.news["news_id"],))
    news_exists = cursor.fetchone() is not None
    news_rowcount = execute_insert(
        cursor,
        "news_raw",
        records.news,
        [
            "matched_search_keywords",
            "matched_jw_search_contexts",
            "news_source_file",
            "scored",
            "scored_at",
            "corpus_file_path",
        ],
    )
    event_rowcount = execute_insert(
        cursor,
        "events",
        records.event,
        ["category", "category_label", "summary", "body_full", "processed_by", "processed_at", "search_keyword"],
    )
    score_rowcount = 0
    for score in records.scores:
        score_rowcount += execute_insert(
            cursor,
            "event_brand_scores",
            score,
            [
                "news_id",
                "brand_name",
                "brand_id",
                "ml_id",
                "cd_id",
                "is_jw",
                "score",
                "score_tier",
                "reason",
                "source_processor",
                "generated_at",
                "derivation",
                "mirrored_from_jw_brands",
                "tag",
                "summary",
                "workflow_id",
                "catalog_version",
                "llm_meta",
            ],
        )
    return {
        "news_written": 0 if news_exists else min(news_rowcount, 1),
        "events_written": min(event_rowcount, 1),
        "scores_written": sum(1 for _ in records.scores) if score_rowcount else 0,
        "existing_news": int(news_exists),
    }


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


def load_batch(args: argparse.Namespace, catalog: CatalogResolver) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()
    batch_dir = args.batch_dir.expanduser().resolve()
    found = scored_files(batch_dir, args.scored_dir.expanduser().resolve() if args.scored_dir else None)
    if args.limit:
        found = found[: args.limit]

    stats: dict[str, Any] = {
        "started_at": started.isoformat(),
        "batch_dir": str(batch_dir),
        "scored_files_found": len(found),
        "dry_run": bool(args.dry_run),
        "news_raw_inserted": 0,
        "events_inserted": 0,
        "event_brand_scores_llm_direct": 0,
        "existing_news_skipped": 0,
        "error_count": 0,
        "errors": [],
        "tag_distribution": {},
        "score_tier_distribution": {},
    }
    tag_counter: collections.Counter[str] = collections.Counter()
    tier_counter: collections.Counter[str] = collections.Counter()

    conn = None if args.dry_run else connect(args)
    try:
        for scored_path in found:
            try:
                records = build_records(batch_dir, scored_path, catalog, workflow_id=args.workflow_id)
                tag_counter[str(records.event["category_label"])] += 1
                for score in records.scores:
                    tier_counter[str(score["score_tier"])] += 1
                if args.dry_run:
                    stats["event_brand_scores_llm_direct"] += len(records.scores)
                    stats["news_raw_inserted"] += 1
                    stats["events_inserted"] += 1
                    continue
                assert conn is not None
                with conn.cursor() as cursor:
                    written = load_records(cursor, records, dry_run=False)
                conn.commit()
                stats["news_raw_inserted"] += written.get("news_written", 0)
                stats["events_inserted"] += written.get("events_written", 0)
                stats["event_brand_scores_llm_direct"] += len(records.scores)
                stats["existing_news_skipped"] += written.get("existing_news", 0)
            except Exception as exc:
                if conn is not None:
                    conn.rollback()
                stats["error_count"] += 1
                stats["errors"].append({"scored_path": str(scored_path), "error": str(exc)})
    finally:
        if conn is not None:
            conn.close()
    ended = datetime.now(timezone.utc)
    stats["ended_at"] = ended.isoformat()
    stats["elapsed_seconds"] = round(time.monotonic() - started_monotonic, 2)
    stats["tag_distribution"] = dict(tag_counter)
    stats["score_tier_distribution"] = dict(tier_counter)
    stats["verdict"] = "passed" if stats["error_count"] == 0 else "partial"
    return stats


def default_parquet_paths(repo_root: Path) -> list[Path]:
    return [
        repo_root / "output" / "catalog" / "strategic_brand" / "strategic_brand.parquet",
        repo_root / "output" / "catalog" / "cd_brand" / "cd_brand.parquet",
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--scored-dir", type=Path)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--db-host", default=os.getenv("DB_HOST", "127.0.0.1"))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("DB_PORT", "3308")))
    parser.add_argument("--db-user", default=os.getenv("DB_USER", "root"))
    parser.add_argument("--db-password", default=os.getenv("DB_PASSWORD", ""))
    parser.add_argument("--db-name", default=os.getenv("DB_NAME", "jw_mart"))
    parser.add_argument("--workflow-id", type=int, default=DEFAULT_WORKFLOW_ID)
    parser.add_argument("--strategic-brand-catalog", type=Path)
    parser.add_argument("--cd-brand-catalog", type=Path)
    parser.add_argument("--no-local-parquet-enrich", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[3]
    parquet_paths: list[Path] = []
    if not args.no_local_parquet_enrich:
        parquet_paths.extend(default_parquet_paths(repo_root))
    if args.strategic_brand_catalog:
        parquet_paths.append(args.strategic_brand_catalog.expanduser())
    if args.cd_brand_catalog:
        parquet_paths.append(args.cd_brand_catalog.expanduser())
    catalog = load_catalog(args.catalog.expanduser(), parquet_paths=parquet_paths)
    result = load_batch(args, catalog)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["error_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
