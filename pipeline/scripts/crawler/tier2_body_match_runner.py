"""Build Tier2 body-exact brand matches into an intermediate table.

This runner is intentionally match-only. It writes no scores and never touches
``event_brand_scores``; the scoring workflow consumes the staging table later.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

import pymysql


DEFAULT_DB_NAME = "jw_mart_d2_stage_20260630_r2"
DEFAULT_TARGET_TABLE = "tier2_match_staging"
TIER2_EXACT_PROCESSOR = "tier2_exact_rule_v1"
TIER1_PROCESSORS = ("workflow_196_optionB", "cross_match_adapter_v1")
STOPLIST_BRAND_NAMES: frozenset[str] = frozenset(
    {
        "제로",
        "케어",
        "프로",
        "센스",
        "데일리",
        "로이드",
        "탑",
        "트라",
        "이지",
        "파인",
        "피디",
        "코미",
        "웰",
    }
)


@dataclass(frozen=True)
class BodyMatchRunnerConfig:
    ambiguous_compact_len: int = 3


@dataclass(frozen=True)
class BodyMatchBrand:
    brand_key: str
    brand_name: str
    source: str


@dataclass(frozen=True)
class BodyMatch:
    news_id: str
    brand_key: str
    brand_name: str
    match_source: str
    matched_keywords: tuple[str, ...]

    def as_insert_tuple(self, run_id: str) -> tuple[str, str, str, str, str, str]:
        return (
            run_id,
            self.news_id,
            self.brand_key,
            self.brand_name,
            self.match_source,
            json.dumps(list(self.matched_keywords), ensure_ascii=False, separators=(",", ":")),
        )


@dataclass(frozen=True)
class NewsItem:
    news_id: str
    title: str
    article_text: str
    collection_provenance: str


@dataclass(frozen=True)
class _TrieHit:
    start: int
    end: int
    brand_index: int

    @property
    def length(self) -> int:
        return self.end - self.start


class _TrieNode:
    def __init__(self) -> None:
        self.children: dict[str, int] = {}
        self.brand_indexes: list[int] = []


class _BrandTrie:
    def __init__(self, brands: Sequence[BodyMatchBrand]) -> None:
        self._brands = brands
        self._compact_names = [compact_text(brand.brand_name) for brand in brands]
        self._nodes: list[_TrieNode] = [_TrieNode()]
        for index, compact_name in enumerate(self._compact_names):
            if not compact_name:
                continue
            node_index = 0
            for char in compact_name:
                node = self._nodes[node_index]
                if char not in node.children:
                    node.children[char] = len(self._nodes)
                    self._nodes.append(_TrieNode())
                node_index = node.children[char]
            self._nodes[node_index].brand_indexes.append(index)

    def find(self, haystack: str) -> list[_TrieHit]:
        hits: list[_TrieHit] = []
        for start in range(len(haystack)):
            node_index = 0
            cursor = start
            while cursor < len(haystack):
                node = self._nodes[node_index]
                next_index = node.children.get(haystack[cursor])
                if next_index is None:
                    break
                node_index = next_index
                cursor += 1
                for brand_index in self._nodes[node_index].brand_indexes:
                    hits.append(_TrieHit(start=start, end=cursor, brand_index=brand_index))
        return hits


class Tier2BodyMatcher:
    def __init__(self, brands: Sequence[BodyMatchBrand], config: BodyMatchRunnerConfig) -> None:
        self._brands = tuple(brands)
        self._config = config
        self._trie = _BrandTrie(self._brands)

    def match_news(
        self,
        *,
        news_id: str,
        title: str,
        article_text: str,
        collection_provenance: str,
    ) -> list[BodyMatch]:
        provenance_keys, provenance_names = tier2_provenance_lookup(collection_provenance)
        accepted_hits = self._accepted_longest_hits(compact_text(f"{title} {article_text}"))
        out: list[BodyMatch] = []
        seen_keys: set[str] = set()
        for hit in accepted_hits:
            brand = self._brands[hit.brand_index]
            if brand.brand_key in seen_keys:
                continue
            has_provenance = (
                brand.brand_key in provenance_keys
                or compact_text(brand.brand_name) in provenance_names
            )
            if should_pause_for_ambiguous_brand(
                brand.brand_name,
                config=self._config,
            ) and not has_provenance:
                continue
            seen_keys.add(brand.brand_key)
            out.append(
                BodyMatch(
                    news_id=news_id,
                    brand_key=brand.brand_key,
                    brand_name=brand.brand_name,
                    match_source="body+search_provenance" if has_provenance else "body",
                    matched_keywords=(brand.brand_name,),
                )
            )
        return out

    def _accepted_longest_hits(self, haystack: str) -> list[_TrieHit]:
        hits = self._trie.find(haystack)
        accepted: list[_TrieHit] = []
        occupied: list[tuple[int, int]] = []
        for hit in sorted(hits, key=lambda item: (-item.length, item.start, item.end)):
            if any(hit.start < end and start < hit.end for start, end in occupied):
                continue
            occupied.append((hit.start, hit.end))
            accepted.append(hit)
        return sorted(accepted, key=lambda item: (item.start, item.end))


def compact_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def should_pause_for_ambiguous_brand(
    brand_name: str,
    *,
    config: BodyMatchRunnerConfig | None = None,
) -> bool:
    cfg = config or BodyMatchRunnerConfig()
    compact_name = compact_text(brand_name)
    if not compact_name:
        return True
    if compact_name in {compact_text(item) for item in STOPLIST_BRAND_NAMES}:
        return True
    return len(compact_name) <= cfg.ambiguous_compact_len


def tier2_provenance_lookup(collection_provenance: str | None) -> tuple[set[str], set[str]]:
    keys: set[str] = set()
    names: set[str] = set()
    for item in _json_list(collection_provenance):
        if not isinstance(item, dict):
            continue
        tier = str(item.get("tier") or "").strip()
        if tier != "2":
            continue
        brand_key = str(item.get("brand_key") or "").strip()
        brand_name = str(item.get("brand") or item.get("jw_brand") or "").strip()
        if brand_key:
            keys.add(brand_key)
        if brand_name:
            names.add(compact_text(brand_name))
        for keyword in item.get("matched_keywords") or ():
            if str(keyword or "").strip():
                names.add(compact_text(str(keyword)))
    return keys, names


def _json_list(value: str | None) -> list[object]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return parsed
    return []


def connect_from_env() -> pymysql.connections.Connection:
    user = (
        os.getenv("D2_WRITER_USER")
        or os.getenv("DB_USER")
        or os.getenv("MARIADB_USER")
        or "root"
    )
    password = (
        os.getenv("D2_WRITER_PASSWORD")
        or os.getenv("DB_PASSWORD")
        or os.getenv("MARIADB_PASSWORD")
        or ""
    )
    return pymysql.connect(
        host=os.getenv("DB_HOST", os.getenv("MARIADB_HOST", "127.0.0.1")),
        port=int(os.getenv("DB_PORT", os.getenv("MARIADB_PORT", "3306"))),
        user=user,
        password=password,
        database=os.getenv("DB_NAME", os.getenv("MARIADB_DATABASE", DEFAULT_DB_NAME)),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def load_excluded_brand_names(conn: pymysql.connections.Connection) -> set[str]:
    placeholders = ", ".join(["%s"] * len(TIER1_PROCESSORS))
    sql = f"""
        SELECT DISTINCT brand_canonical
        FROM event_brand_scores
        WHERE source_processor IN ({placeholders})
          AND brand_canonical IS NOT NULL
          AND brand_canonical <> ''
    """
    with conn.cursor() as cursor:
        cursor.execute(sql, TIER1_PROCESSORS)
        rows = cursor.fetchall()
    return {compact_text(str(row["brand_canonical"])) for row in rows}


def load_tier2_brands(conn: pymysql.connections.Connection) -> list[BodyMatchBrand]:
    excluded = load_excluded_brand_names(conn)
    sql = """
        SELECT brand_key, brand_name, MIN(source) AS source
        FROM mart_general_brand_metric
        WHERE measure = 'sales'
          AND source IN ('ubist', 'iqvia_nsa')
          AND brand_key IS NOT NULL
          AND brand_key <> ''
          AND brand_name IS NOT NULL
          AND brand_name <> ''
        GROUP BY brand_key, brand_name
    """
    with conn.cursor() as cursor:
        cursor.execute(sql)
        rows = cursor.fetchall()
    brands: list[BodyMatchBrand] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        brand_key = str(row["brand_key"]).strip()
        brand_name = str(row["brand_name"]).strip()
        if compact_text(brand_name) in excluded:
            continue
        identity = (brand_key, compact_text(brand_name))
        if identity in seen:
            continue
        seen.add(identity)
        brands.append(
            BodyMatchBrand(
                brand_key=brand_key,
                brand_name=brand_name,
                source=str(row.get("source") or ""),
            )
        )
    return sorted(brands, key=lambda item: (-len(compact_text(item.brand_name)), item.brand_name))


def load_tier2_exact_news_ids(conn: pymysql.connections.Connection) -> set[str]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT COALESCE(news_id, event_id) AS news_id
            FROM event_brand_scores
            WHERE source_processor = %s
              AND COALESCE(news_id, event_id) IS NOT NULL
            """,
            (TIER2_EXACT_PROCESSOR,),
        )
        rows = cursor.fetchall()
    return {str(row["news_id"]) for row in rows}


def load_news_items(conn: pymysql.connections.Connection) -> list[NewsItem]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT news_id, title, article_text, collection_provenance
            FROM news_raw
            ORDER BY news_id
            """
        )
        rows = cursor.fetchall()
    return [
        NewsItem(
            news_id=str(row["news_id"]),
            title=str(row.get("title") or ""),
            article_text=str(row.get("article_text") or ""),
            collection_provenance=str(row.get("collection_provenance") or "[]"),
        )
        for row in rows
    ]


def filter_target_news(items: Iterable[NewsItem], exact_news_ids: set[str]) -> list[NewsItem]:
    out: list[NewsItem] = []
    for item in items:
        provenance_keys, provenance_names = tier2_provenance_lookup(item.collection_provenance)
        if item.news_id in exact_news_ids or provenance_keys or provenance_names:
            out.append(item)
    return out


def create_target_table(conn: pymysql.connections.Connection, table_name: str, *, replace: bool) -> None:
    with conn.cursor() as cursor:
        if replace:
            cursor.execute(f"DROP TABLE IF EXISTS `{table_name}`")
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS `{table_name}` (
              run_id varchar(64) NOT NULL,
              news_id varchar(64) NOT NULL,
              brand_key varchar(255) NOT NULL,
              brand_canonical varchar(255) NOT NULL,
              match_source varchar(64) NOT NULL,
              matched_keywords longtext NOT NULL CHECK (JSON_VALID(matched_keywords)),
              created_at datetime NOT NULL DEFAULT current_timestamp(),
              PRIMARY KEY (run_id, news_id, brand_key),
              KEY idx_tier2_match_news (news_id),
              KEY idx_tier2_match_brand (brand_key),
              KEY idx_tier2_match_source (match_source)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci
            """
        )
    conn.commit()


def insert_matches(
    conn: pymysql.connections.Connection,
    table_name: str,
    *,
    run_id: str,
    matches: Sequence[BodyMatch],
    batch_size: int,
) -> int:
    if not matches:
        return 0
    sql = f"""
        INSERT INTO `{table_name}`
          (run_id, news_id, brand_key, brand_canonical, match_source, matched_keywords)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
          brand_canonical = VALUES(brand_canonical),
          match_source = VALUES(match_source),
          matched_keywords = VALUES(matched_keywords),
          created_at = current_timestamp()
    """
    inserted = 0
    with conn.cursor() as cursor:
        for offset in range(0, len(matches), batch_size):
            chunk = matches[offset : offset + batch_size]
            cursor.executemany(sql, [item.as_insert_tuple(run_id) for item in chunk])
            inserted += len(chunk)
            conn.commit()
    return inserted


def summarize_matches(matches: Sequence[BodyMatch], *, news_count: int, brand_count: int) -> dict[str, object]:
    per_news = Counter(item.news_id for item in matches)
    per_source = Counter(item.match_source for item in matches)
    return {
        "brand_dictionary_count": brand_count,
        "target_news_count": news_count,
        "matched_news_count": len(per_news),
        "multi_brand_news_count": sum(1 for count in per_news.values() if count >= 2),
        "match_rows": len(matches),
        "match_source_counts": dict(sorted(per_source.items())),
        "avg_brands_per_matched_news": round(len(matches) / len(per_news), 4) if per_news else 0,
    }


def run_matching(conn: pymysql.connections.Connection) -> tuple[list[BodyMatch], dict[str, object]]:
    brands = load_tier2_brands(conn)
    matcher = Tier2BodyMatcher(brands, BodyMatchRunnerConfig())
    exact_news_ids = load_tier2_exact_news_ids(conn)
    target_news = filter_target_news(load_news_items(conn), exact_news_ids)
    matches: list[BodyMatch] = []
    for item in target_news:
        matches.extend(
            matcher.match_news(
                news_id=item.news_id,
                title=item.title,
                article_text=item.article_text,
                collection_provenance=item.collection_provenance,
            )
        )
    return matches, summarize_matches(matches, news_count=len(target_news), brand_count=len(brands))


def make_run_id() -> str:
    return "tier2_body_" + dt.datetime.now(dt.UTC).strftime("%Y%m%d%H%M%S")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-table", default=DEFAULT_TARGET_TABLE)
    parser.add_argument("--run-id", default=make_run_id())
    parser.add_argument("--apply", action="store_true", help="Write matches to the intermediate table.")
    parser.add_argument("--replace-table", action="store_true", help="Drop/recreate only the target staging table.")
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()

    conn = connect_from_env()
    try:
        matches, summary = run_matching(conn)
        summary = {
            **summary,
            "run_id": args.run_id,
            "target_table": args.target_table,
            "applied": bool(args.apply),
        }
        if args.apply:
            create_target_table(conn, args.target_table, replace=args.replace_table)
            summary["inserted_rows"] = insert_matches(
                conn,
                args.target_table,
                run_id=args.run_id,
                matches=matches,
                batch_size=args.batch_size,
            )
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
