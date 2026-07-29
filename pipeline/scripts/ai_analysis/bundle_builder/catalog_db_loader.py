from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Optional

from .catalog_constants import ATC4_FALLBACK, MKT_TEAM_FALLBACK


def _json_load(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except Exception:
        return {}


def source_public_to_db(source: str) -> str:
    normalized = source.upper()
    if normalized == "IQVIA":
        return "iqvia_nsa"
    if normalized == "UBIST":
        return "ubist"
    return source.lower()


def source_db_to_public(source: str) -> str:
    normalized = source.lower()
    if normalized.startswith("iqvia"):
        return "IQVIA"
    if normalized == "ubist":
        return "UBIST"
    return source.upper()


def _table_exists(db_conn, table_name: str) -> bool:
    with db_conn.cursor() as cur:
        cur.execute("SHOW TABLES LIKE %s", (table_name,))
        return cur.fetchone() is not None


def _compact_brand_name(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def _load_json_catalog(path: str = "docs/crawl/_catalog.json") -> dict:
    catalog_path = Path(path)
    if not catalog_path.exists():
        return {}
    return json.loads(catalog_path.read_text(encoding="utf-8"))


def _load_search_keywords(path: str = "docs/crawl/search_keywords.json") -> dict:
    keyword_path = Path(path)
    if not keyword_path.exists():
        return {}
    return json.loads(keyword_path.read_text(encoding="utf-8"))


def _catalog_lookup_keys(brand_name: str) -> List[str]:
    """_catalog.json / search_keywords.json 조회 후보 키 (직접 키 우선, alias fallback).

    canonical 표시명과 catalog/keyword 키 불일치(예: 위너프A+ ↔ 위너프에이플러스)를 흡수한다.
    alias 는 직접 키가 없을 때만 fallback 으로 쓰므로, 직접 키가 존재하는 브랜드(예: 가드렛 —
    layer3_aliases 가 molecule 명) 는 alias 를 타지 않아 회귀가 없다. DISPLAY_BRANDS.layer3_aliases
    (api.catalog) 의 기존 매핑만 재사용하며 import 실패 시 brand_name 단독으로 안전 degrade 한다.
    """
    candidates = [brand_name]
    try:
        import sys

        repo_root = Path(__file__).resolve().parents[4]
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        from pipeline.scripts.api.catalog import DISPLAY_BRAND_BY_NAME

        display = DISPLAY_BRAND_BY_NAME.get(brand_name)
        if display:
            candidates.extend(alias for alias in display.layer3_aliases if alias not in candidates)
    except Exception:
        pass
    return candidates


def _english_name_from_parsed(parsed: dict) -> Optional[str]:
    """search_keywords.약 영문명 의 첫 값, 없으면 catalog english_name 으로 안전 fallback.

    line 226 IndexError 방어: 빈 list `[]` 도 안전 처리 (기존 .get default 는 key 누락만 처리).
    """
    sk_eng = (parsed.get("search_keywords") or {}).get("약 영문명") or []
    if sk_eng:
        return sk_eng[0]
    return parsed.get("english_name")


def _parse_catalog_description(brand_name: str) -> dict:
    catalog = _load_json_catalog()
    keywords_map = _load_search_keywords()
    candidates = _catalog_lookup_keys(brand_name)

    description = ""
    search_keywords = None
    for key in candidates:
        if not description:
            description = catalog.get(key, "") or ""
        if search_keywords is None and key in keywords_map:
            search_keywords = keywords_map[key]

    parts = [part.strip() for part in description.split("|")]
    intro = parts[0] if parts else ""
    english = intro.split(",", 1)[0].strip() if intro else None
    company = None
    competitors = []
    for part in parts:
        if part.startswith("회사:"):
            company = part.split(":", 1)[1].strip()
        if part.startswith("경쟁:"):
            competitors = [item.strip() for item in re.split(r"[,，]", part.split(":", 1)[1]) if item.strip()]
    if search_keywords is None:
        search_keywords = {"약 영문명": [english] if english else []}
    return {
        "description": intro or description,
        "english_name": english,
        "company": company,
        "catalog_competitors": competitors,
        "search_keywords": search_keywords,
    }


def load_cd_id_for_brand(brand_name: str, db_conn) -> Optional[str]:
    if _table_exists(db_conn, "catalog_cd_brand"):
        with db_conn.cursor() as cur:
            cur.execute("SELECT cd_id FROM catalog_cd_brand WHERE name = %s LIMIT 1", (brand_name,))
            row = cur.fetchone()
        if row:
            return row.get("cd_id")
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT cd_market_id
            FROM mart_strategic_cd_brand_metric
            WHERE brand_name = %s
            ORDER BY cd_market_id ASC
            LIMIT 1
            """,
            (brand_name,),
        )
        row = cur.fetchone()
    return row.get("cd_market_id") if row else None


def load_market_from_catalog(ml_id: str, db_conn) -> dict:
    if _table_exists(db_conn, "catalog_strategic_ml_market"):
        with db_conn.cursor() as cur:
            cur.execute("SELECT * FROM catalog_strategic_ml_market WHERE ml_id = %s LIMIT 1", (ml_id,))
            row = cur.fetchone()
        if row:
            return dict(row)
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT ml_id, ml_name, MAX(computed_at) AS computed_at
            FROM mart_strategic_ml_market_metric
            WHERE ml_id = %s
            GROUP BY ml_id, ml_name
            LIMIT 1
            """,
            (ml_id,),
        )
        row = cur.fetchone()
    return {
        "ml_id": ml_id,
        "ml_name": row.get("ml_name") if row else None,
        "atc4_code": ATC4_FALLBACK.get(ml_id),
        "market_label_kor": (row.get("ml_name") if row else None),
        "computed_at": row.get("computed_at") if row else None,
    }


def detect_available_sources(brand_name: str, db_conn) -> List[str]:
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT source
            FROM cache_cause
            WHERE brand = %s
            ORDER BY source ASC
            """,
            (brand_name,),
        )
        rows = cur.fetchall()
    sources = [source_db_to_public(row["source"]) for row in rows]
    if sources:
        return sorted(set(sources), key=lambda value: ("UBIST", "IQVIA").index(value) if value in ("UBIST", "IQVIA") else 99)

    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT source
            FROM mart_strategic_ml_brand_metric
            WHERE brand_name = %s
            ORDER BY source ASC
            """,
            (brand_name,),
        )
        rows = cur.fetchall()
    return sorted(
        {source_db_to_public(row["source"]) for row in rows},
        key=lambda value: ("UBIST", "IQVIA").index(value) if value in ("UBIST", "IQVIA") else 99,
    )


def load_brand_from_catalog(brand_name: str, db_conn) -> dict:
    compact_brand_name = _compact_brand_name(brand_name)
    if _table_exists(db_conn, "catalog_strategic_brand"):
        with db_conn.cursor() as cur:
            cur.execute("SELECT * FROM catalog_strategic_brand WHERE name = %s LIMIT 1", (brand_name,))
            row = cur.fetchone()
        if row:
            return dict(row)
        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM catalog_strategic_brand
                WHERE REPLACE(LOWER(name), ' ', '') = %s
                ORDER BY name ASC
                LIMIT 1
                """,
                (compact_brand_name,),
            )
            row = cur.fetchone()
        if row:
            return dict(row)

    parsed = _parse_catalog_description(brand_name)
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT ml_id, brand_id, brand_key, brand_name, is_jw, overlay_data, computed_at
            FROM mart_strategic_ml_brand_metric
            WHERE brand_name = %s
            ORDER BY ml_id ASC, brand_id ASC, source ASC, measure ASC, computed_at DESC
            LIMIT 1
            """,
            (brand_name,),
        )
        row = cur.fetchone()
    if not row:
        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT ml_id, brand_id, brand_key, brand_name, is_jw, overlay_data, computed_at
                FROM mart_strategic_ml_brand_metric
                WHERE REPLACE(LOWER(brand_name), ' ', '') = %s
                ORDER BY ml_id ASC, brand_id ASC, source ASC, measure ASC, computed_at DESC
                LIMIT 1
                """,
                (compact_brand_name,),
            )
            row = cur.fetchone()
    if not row:
        raise ValueError(f"brand not found in mart/catalog: {brand_name}")

    overlay = _json_load(row.get("overlay_data"))
    market = load_market_from_catalog(row["ml_id"], db_conn)
    return {
        "brand_id": row.get("brand_id"),
        "ml_id": row.get("ml_id"),
        "name": row.get("brand_name") or brand_name,
        "derived_key": row.get("brand_key") or brand_name,
        "is_jw": bool(row.get("is_jw")),
        "is_target": bool(overlay.get("is_target", row.get("is_jw"))),
        "atc4_code": overlay.get("atc4_code") or market.get("atc4_code"),
        "manufacturer": parsed.get("company"),
        "english_name": _english_name_from_parsed(parsed),
        "molecule": overlay.get("molecule"),
        "class": overlay.get("class"),
        "mkt_team": MKT_TEAM_FALLBACK.get(row.get("ml_id")),
        "notes": parsed.get("description"),
        "search_keywords": parsed.get("search_keywords") or {},
        "catalog_competitors": parsed.get("catalog_competitors") or [],
    }
