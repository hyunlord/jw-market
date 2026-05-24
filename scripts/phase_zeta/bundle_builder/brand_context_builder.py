from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict


def _as_sql_datetime(snapshot_at) -> str:
    return snapshot_at.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def _split_competitors(description: str) -> list:
    match = re.search(r"경쟁:\s*(.*)$", description or "")
    if not match:
        return []
    return [item.strip() for item in re.split(r"[,，]", match.group(1)) if item.strip()]


def _parse_description(description: str) -> dict:
    parts = [part.strip() for part in (description or "").split("|")]
    intro = parts[0] if parts else ""
    english_name = intro.split(",", 1)[0].strip() if intro else None
    company = None
    for part in parts:
        if part.startswith("회사:"):
            company = part.split(":", 1)[1].strip()
    return {
        "english_name": english_name or None,
        "company": company,
        "description": intro or description,
        "competitors": _split_competitors(description),
    }


def build_brand_context(
    brand: str,
    catalog_path: str = "docs/crawl/_catalog.json",
) -> Dict:
    catalog = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
    if brand not in catalog:
        raise ValueError(f"brand not found in catalog: {brand}")

    parsed = _parse_description(catalog[brand])
    search_keywords = {"약 영문명": [parsed["english_name"]]} if parsed["english_name"] else {"약 영문명": []}
    return {
        "name": brand,
        "english_name": parsed["english_name"],
        "company": parsed["company"],
        "description": parsed["description"],
        "search_keywords": search_keywords,
        "market_ids": [],
        "competitors": parsed["competitors"],
    }


def find_market_ids_for_brand(
    brand: str,
    db_conn,
    snapshot_at,
) -> Dict[str, list]:
    snapshot_sql = _as_sql_datetime(snapshot_at)
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ml_id
            FROM mart_strategic_ml_brand_metric
            WHERE brand_name = %s AND computed_at <= %s
            ORDER BY ml_id ASC
            """,
            (brand, snapshot_sql),
        )
        ml_ids = [row["ml_id"] for row in cur.fetchall()]

        cur.execute(
            """
            SELECT DISTINCT cd_market_id
            FROM mart_strategic_cd_brand_metric
            WHERE brand_name = %s AND computed_at <= %s
            ORDER BY cd_market_id ASC
            """,
            (brand, snapshot_sql),
        )
        cd_ids = [row["cd_market_id"] for row in cur.fetchall()]

    return {"ml_ids": ml_ids, "cd_ids": cd_ids}
