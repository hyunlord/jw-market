from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Optional

ATC4_FALLBACK = {
    "ml_001": "A02B",
    "ml_003": "A10B",
    "ml_006": "C10A1",
    "ml_013": "B02D",
}

MKT_TEAM_FALLBACK = {
    # MI Master 2026-05-18 기준. Long-term source는 catalog ETL로
    # 통합해야 하지만, Phase ζ bundle 생성은 이 fallback을 사용한다.
    "ml_001": "MKT 1팀",
    "ml_002": "MKT 1팀",
    "ml_003": "MKT 1팀",
    "ml_004": "MKT 1팀",
    "ml_005": "MKT 1팀",
    "ml_006": "MKT 1팀",
    "ml_007": "MKT 1팀",
    "ml_008": "MKT 1팀",
    "ml_009": "MKT 1팀",
    "ml_010": "MKT 1팀",
    "ml_011": "MKT 1팀",
    "ml_012": "MKT 2팀",
    "ml_013": "MKT 2팀",
    "ml_014": "MKT 3팀",
    "ml_015": "MKT 2팀",
    "ml_016": "MKT 3팀",
}


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


def _parse_catalog_description(brand_name: str) -> dict:
    catalog = _load_json_catalog()
    description = catalog.get(brand_name, "")
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
    return {
        "description": intro or description,
        "english_name": english,
        "company": company,
        "catalog_competitors": competitors,
        "search_keywords": _load_search_keywords().get(brand_name, {"약 영문명": [english] if english else []}),
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
    if _table_exists(db_conn, "catalog_strategic_brand"):
        with db_conn.cursor() as cur:
            cur.execute("SELECT * FROM catalog_strategic_brand WHERE name = %s LIMIT 1", (brand_name,))
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
            ORDER BY ml_id ASC, computed_at DESC
            LIMIT 1
            """,
            (brand_name,),
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
        "english_name": (parsed.get("search_keywords") or {}).get("약 영문명", [parsed.get("english_name")])[0],
        "molecule": overlay.get("molecule"),
        "class": overlay.get("class"),
        "mkt_team": MKT_TEAM_FALLBACK.get(row.get("ml_id")),
        "notes": parsed.get("description"),
        "search_keywords": parsed.get("search_keywords") or {},
        "catalog_competitors": parsed.get("catalog_competitors") or [],
    }
