#!/usr/bin/env python3
# allow: SIZE_OK — cutover SQL generator keeps the parse boundary, component build,
# and emitted map/pick SQL together so audit replay uses one immutable artifact.
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PREFIXES: Final[tuple[str, ...]] = ("utm_",)
TRACKING_KEYS: Final[set[str]] = {
    "fbclid",
    "gclid",
    "dclid",
    "yclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "spm",
    "ref",
    "ref_src",
}


@dataclass(frozen=True, slots=True)
class NewsRow:
    news_id: str
    article_url: str
    title: str
    published_date: str
    search_keyword: str
    source_name: str
    matched_search_keywords: str
    matched_jw_search_contexts: str
    tier: int
    text_len: int
    ingested_at: str
    collected_at: str


@dataclass(frozen=True, slots=True)
class ScoreRow:
    score_id: int
    news_id: str
    event_id: str
    brand_name: str
    brand_canonical: str
    score: str
    score_tier: str
    source_processor: str
    generated_at: str
    derivation: str
    tag: str
    workflow_id: str
    catalog_version: str
    reason_present: int
    summary_present: int
    llm_meta_present: int
    text_len: int


class UnionFind:
    def __init__(self, ids: list[str]) -> None:
        self._parent = {item: item for item in ids}

    def find(self, item: str) -> str:
        parent = self._parent[item]
        if parent != item:
            self._parent[item] = self.find(parent)
        return self._parent[item]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self._parent[right_root] = left_root

    def components(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = defaultdict(list)
        for item in self._parent:
            grouped[self.find(item)].append(item)
        return dict(grouped)


def decode_b64(value: str) -> str:
    if not value:
        return ""
    return base64.b64decode(value).decode("utf-8", errors="replace")


def norm_url(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = urlsplit(value.strip())
        scheme = (parsed.scheme or "https").lower()
        netloc = parsed.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        path = re.sub(r"/+$", "", parsed.path or "/") or "/"
        query = []
        for key, raw in parse_qsl(parsed.query, keep_blank_values=True):
            lowered = key.lower()
            if lowered in TRACKING_KEYS or any(lowered.startswith(prefix) for prefix in TRACKING_PREFIXES):
                continue
            query.append((key, raw))
        return urlunsplit((scheme, netloc, path, urlencode(query, doseq=True), ""))
    except ValueError:
        return value.strip().lower()


def sql(value: str | None) -> str:
    if value is None:
        return "NULL"
    return "'" + value.replace("\\", "\\\\").replace("'", "''").replace("\x00", "") + "'"


def read_news(path: Path) -> list[NewsRow]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = []
        for raw in csv.DictReader(handle, delimiter="\t"):
            rows.append(
                NewsRow(
                    news_id=raw["news_id"],
                    article_url=decode_b64(raw["article_url_b64"]),
                    title=decode_b64(raw["title_b64"]),
                    published_date=raw["published_date"],
                    search_keyword=decode_b64(raw["search_keyword_b64"]),
                    source_name=decode_b64(raw["source_name_b64"]),
                    matched_search_keywords=decode_b64(raw["matched_search_keywords_b64"]),
                    matched_jw_search_contexts=decode_b64(raw["matched_jw_search_contexts_b64"]),
                    tier=int(raw["tier"] or 0),
                    text_len=int(raw["text_len"] or 0),
                    ingested_at=raw["ingested_at"],
                    collected_at=raw["collected_at"],
                )
            )
        return rows


def read_scores(path: Path) -> list[ScoreRow]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = []
        for raw in csv.DictReader(handle, delimiter="\t"):
            rows.append(
                ScoreRow(
                    score_id=int(raw["id"]),
                    news_id=raw["news_id"],
                    event_id=raw["event_id"],
                    brand_name=decode_b64(raw["brand_name_b64"]),
                    brand_canonical=decode_b64(raw["brand_canonical_b64"]),
                    score=raw["score"],
                    score_tier=raw["score_tier"],
                    source_processor=raw["source_processor"],
                    generated_at=raw["generated_at"],
                    derivation=raw["derivation"],
                    tag=raw["tag"],
                    workflow_id=raw["workflow_id"],
                    catalog_version=raw["catalog_version"],
                    reason_present=int(raw["reason_present"] or 0),
                    summary_present=int(raw["summary_present"] or 0),
                    llm_meta_present=int(raw["llm_meta_present"] or 0),
                    text_len=int(raw["text_len"] or 0),
                )
            )
        return rows


def provenance_for(row: NewsRow) -> list[dict[str, str | int | None | list[str]]]:
    contexts: list[dict[str, str | int | None | list[str]]] = []
    if row.matched_jw_search_contexts:
        parsed = json.loads(row.matched_jw_search_contexts)
        if isinstance(parsed, list):
            contexts.extend(item for item in parsed if isinstance(item, dict))
        elif isinstance(parsed, dict):
            contexts.append(parsed)
    matched = _matched_keywords(row)
    if not contexts:
        contexts = [{}]
    result: list[dict[str, str | int | None | list[str]]] = []
    for context in contexts:
        raw_keywords = context.get("matched_keywords", matched)
        keywords = [str(raw_keywords)] if isinstance(raw_keywords, str) else [str(item) for item in raw_keywords]
        result.append(
            {
                "tier": context.get("tier", row.tier),
                "brand": str(context.get("jw_brand") or context.get("brand") or context.get("brand_name") or row.search_keyword),
                "brand_key": context.get("brand_key") or context.get("ml_id") or context.get("brand_id"),
                "source": str(context.get("source") or row.source_name),
                "matched_keywords": list(dict.fromkeys(item for item in keywords if item)),
            }
        )
    return result


def _matched_keywords(row: NewsRow) -> list[str]:
    if row.matched_search_keywords:
        parsed = json.loads(row.matched_search_keywords)
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    return [row.search_keyword] if row.search_keyword else []


def build_components(rows: list[NewsRow]) -> dict[str, list[str]]:
    by_id = {row.news_id: row for row in rows}
    union_find = UnionFind(list(by_id))
    for key_fn in (lambda row: norm_url(row.article_url), lambda row: (row.title.strip(), row.published_date)):
        grouped: dict[str | tuple[str, str], list[str]] = defaultdict(list)
        for row in rows:
            key = key_fn(row)
            valid = all(key) if isinstance(key, tuple) else bool(key)
            if valid:
                grouped[key].append(row.news_id)
        for ids in grouped.values():
            first = ids[0]
            for news_id in ids[1:]:
                union_find.union(first, news_id)
    return union_find.components()


def representative(ids: list[str], by_id: dict[str, NewsRow]) -> str:
    return max(ids, key=lambda item: (1 if by_id[item].tier == 1 else 0, by_id[item].text_len, by_id[item].ingested_at or by_id[item].collected_at, item))


def score_complete(row: ScoreRow) -> int:
    text_present = sum(
        1
        for item in (row.score, row.score_tier, row.source_processor, row.generated_at, row.derivation, row.tag, row.workflow_id, row.catalog_version)
        if item
    )
    return text_present + row.reason_present + row.summary_present + row.llm_meta_present


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--news-b64-tsv", required=True, type=Path)
    parser.add_argument("--scores-b64-tsv", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--db", required=True)
    parser.add_argument("--suffix", required=True)
    args = parser.parse_args()
    rows = read_news(args.news_b64_tsv)
    scores = read_scores(args.scores_b64_tsv)
    by_id = {row.news_id: row for row in rows}
    components = build_components(rows)
    map_rows: list[tuple[str, str, int, str, int, str, str, str]] = []
    old_to_new: dict[str, str] = {}
    for index, ids in enumerate(sorted(components.values(), key=lambda item: sorted(item)[0]), start=1):
        ids_sorted = sorted(ids)
        rep = representative(ids_sorted, by_id)
        basis_url = norm_url(by_id[rep].article_url)
        basis = basis_url or f"title-date:{by_id[rep].title.strip()}|{by_id[rep].published_date}"
        new_id = hashlib.sha256(basis.encode()).hexdigest()[:16]
        provenance_seen: set[str] = set()
        provenance = []
        for news_id in ids_sorted:
            for item in provenance_for(by_id[news_id]):
                key = json.dumps(item, ensure_ascii=False, sort_keys=True)
                if key not in provenance_seen:
                    provenance_seen.add(key)
                    provenance.append(item)
        provenance_json = json.dumps(provenance, ensure_ascii=False, separators=(",", ":"))
        legacy_json = json.dumps(ids_sorted, ensure_ascii=False, separators=(",", ":"))
        for news_id in ids_sorted:
            old_to_new[news_id] = new_id
            map_rows.append((news_id, new_id, index, rep, 1 if news_id == rep else 0, "url_or_title_date_component" if len(ids_sorted) > 1 else "singleton", provenance_json, legacy_json))
    score_groups: dict[tuple[str, str], list[ScoreRow]] = defaultdict(list)
    for row in scores:
        new_id = old_to_new.get(row.news_id)
        if new_id:
            score_groups[(new_id, row.brand_canonical or row.brand_name)].append(row)
    score_picks = [
        (
            pick.score_id,
            new_id,
            brand,
            len(group),
            1 if len({item.score for item in group}) > 1 else 0,
        )
        for (new_id, brand), group in score_groups.items()
        for pick in [max(group, key=lambda row: (score_complete(row), row.text_len, row.generated_at, row.score_id))]
    ]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    _write_sql(args.out_dir / "load_cutover_maps.sql", args.db, args.suffix, map_rows, score_picks)
    summary = {"source_news": len(rows), "components": len(components), "reduction": len(rows) - len(components), "target_news_rows": len(components), "score_pick_rows": len(score_picks)}
    (args.out_dir / "mapping_build_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def _write_sql(
    path: Path,
    db: str,
    suffix: str,
    map_rows: list[tuple[str, str, int, str, int, str, str, str]],
    score_picks: list[tuple[int, str, str, int, int]],
) -> None:
    table_map = f"_cutover_news_map_final_843_{suffix}"
    table_pick = f"_cutover_score_pick_final_843_{suffix}"
    with path.open("w", encoding="utf-8") as handle:
        handle.write(f"USE `{db}`;\n")
        handle.write(f"DROP TABLE IF EXISTS `{table_pick}`;\nDROP TABLE IF EXISTS `{table_map}`;\n")
        handle.write(f"CREATE TABLE `{table_map}` (old_news_id varchar(64) NOT NULL PRIMARY KEY, new_news_id varchar(64) NOT NULL, component_id int NOT NULL, representative_old_news_id varchar(64) NOT NULL, is_representative tinyint(1) NOT NULL, merge_reason varchar(64) NOT NULL, collection_provenance longtext NOT NULL CHECK (JSON_VALID(collection_provenance)), legacy_news_ids longtext NOT NULL CHECK (JSON_VALID(legacy_news_ids)), KEY idx_new_news_id (new_news_id), KEY idx_component (component_id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;\n")
        _write_chunks(handle, table_map, "(old_news_id,new_news_id,component_id,representative_old_news_id,is_representative,merge_reason,collection_provenance,legacy_news_ids)", [f"({sql(a)},{sql(b)},{c},{sql(d)},{e},{sql(f)},{sql(g)},{sql(h)})" for a, b, c, d, e, f, g, h in map_rows])
        handle.write(f"CREATE TABLE `{table_pick}` (source_score_id bigint NOT NULL PRIMARY KEY, new_news_id varchar(64) NOT NULL, brand_canonical varchar(255) NOT NULL, source_rows int NOT NULL, had_score_conflict tinyint(1) NOT NULL, KEY idx_new_brand (new_news_id, brand_canonical)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_uca1400_ai_ci;\n")
        values = [f"({score_id},{sql(new_id)},{sql(brand)},{count},{conflict})" for score_id, new_id, brand, count, conflict in score_picks]
        _write_chunks(handle, table_pick, "(source_score_id,new_news_id,brand_canonical,source_rows,had_score_conflict)", values)


def _write_chunks(handle, table: str, columns: str, values: list[str]) -> None:
    for start in range(0, len(values), 400):
        handle.write(f"INSERT INTO `{table}` {columns} VALUES\n" + ",\n".join(values[start : start + 400]) + ";\n")


if __name__ == "__main__":
    main()
