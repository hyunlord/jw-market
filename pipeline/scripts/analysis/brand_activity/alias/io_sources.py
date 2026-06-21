from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
import csv
import hashlib
import os

import pymysql

from pipeline.scripts.analysis.brand_activity.alias.builder import KorEvidence, SourceName, SourceObservation
from pipeline.scripts.analysis.brand_activity.alias.normalize import normalize_iqvia_en


@dataclass(frozen=True, slots=True)
class NsaEvidence:
    kor_evidence: dict[str, KorEvidence]
    atc4_by_anchor: dict[str, tuple[str, ...]]
    manufacturer_by_anchor: dict[str, tuple[str, ...]]
    molecule_by_anchor: dict[str, tuple[str, ...]]
    summary: dict[str, object]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        env[key.strip()] = value.strip().strip("'\"")
    return env


def connect_stage_db(repo_root: Path) -> pymysql.connections.Connection:
    env = read_env_file(repo_root / "pipeline" / "docker" / ".env")
    return pymysql.connect(
        host=os.getenv("MARIADB_HOST", env.get("MARIADB_HOST", "127.0.0.1")),
        port=int(os.getenv("HOST_PORT", env.get("HOST_PORT", "3308"))),
        user=os.getenv("MARIADB_USER", "root"),
        password=os.getenv("MARIADB_ROOT_PASSWORD", env.get("MARIADB_ROOT_PASSWORD", "")),
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def fetch_stage_observations(conn: pymysql.connections.Connection) -> list[SourceObservation]:
    statements = [
        """
        SELECT 'csd' AS source_name, master_product AS product_name, '' AS atc4,
               market AS csd_market, representing_company, NULL AS manufacturer
        FROM jw_brand_activity_stage.csd_channel_dynamics_stage
        GROUP BY master_product, market, representing_company
        """,
        """
        SELECT 'keyword' AS source_name, product_name, therapeutic_class AS atc4,
               '' AS csd_market, representing_company, NULL AS manufacturer
        FROM jw_brand_activity_stage.km_keyword_event_stage
        GROUP BY product_name, therapeutic_class, representing_company
        """,
        """
        SELECT 'meeting' AS source_name, product_name, therapeutic_class AS atc4,
               '' AS csd_market, '' AS representing_company, NULL AS manufacturer
        FROM jw_brand_activity_stage.km_meeting_event_stage
        GROUP BY product_name, therapeutic_class
        """,
    ]
    observations: list[SourceObservation] = []
    with conn.cursor() as cur:
        for sql in statements:
            cur.execute(sql)
            for row in cur.fetchall():
                source_name = str(row["source_name"])
                if source_name not in {"csd", "keyword", "meeting"}:
                    continue
                source: SourceName = "csd" if source_name == "csd" else "keyword" if source_name == "keyword" else "meeting"
                observations.append(
                    SourceObservation(
                        source=source,
                        product_name=str(row["product_name"] or ""),
                        atc4=str(row["atc4"] or ""),
                        csd_market=str(row["csd_market"] or ""),
                        representing_company=str(row["representing_company"] or ""),
                        manufacturer=None if row["manufacturer"] is None else str(row["manufacturer"]),
                    )
                )
    return observations


def fetch_stage_snapshot(conn: pymysql.connections.Connection) -> dict[str, object]:
    tables = (
        "csd_channel_dynamics_stage",
        "km_keyword_event_stage",
        "km_meeting_event_stage",
    )
    row_counts: dict[str, int] = {}
    schemas: dict[str, list[str]] = {}
    with conn.cursor() as cur:
        for table in tables:
            cur.execute(f"SELECT COUNT(*) AS row_count FROM jw_brand_activity_stage.{table}")
            row_counts[table] = int(cur.fetchone()["row_count"])
            cur.execute(f"DESCRIBE jw_brand_activity_stage.{table}")
            schemas[table] = [str(row["Field"]) for row in cur.fetchall()]
        queries = {
            "csd_products": "SELECT COUNT(DISTINCT master_product) AS n FROM jw_brand_activity_stage.csd_channel_dynamics_stage",
            "keyword_products": "SELECT COUNT(DISTINCT product_name) AS n FROM jw_brand_activity_stage.km_keyword_event_stage",
            "meeting_products": "SELECT COUNT(DISTINCT product_name) AS n FROM jw_brand_activity_stage.km_meeting_event_stage",
            "union_products": """
                SELECT COUNT(DISTINCT p) AS n FROM (
                  SELECT master_product p FROM jw_brand_activity_stage.csd_channel_dynamics_stage
                  UNION SELECT product_name p FROM jw_brand_activity_stage.km_keyword_event_stage
                  UNION SELECT product_name p FROM jw_brand_activity_stage.km_meeting_event_stage
                ) u
            """,
            "csd_keyword_exact": """
                SELECT COUNT(*) AS n
                FROM (SELECT DISTINCT master_product p FROM jw_brand_activity_stage.csd_channel_dynamics_stage) c
                JOIN (SELECT DISTINCT product_name p FROM jw_brand_activity_stage.km_keyword_event_stage) k USING (p)
            """,
            "csd_meeting_exact": """
                SELECT COUNT(*) AS n
                FROM (SELECT DISTINCT master_product p FROM jw_brand_activity_stage.csd_channel_dynamics_stage) c
                JOIN (SELECT DISTINCT product_name p FROM jw_brand_activity_stage.km_meeting_event_stage) m USING (p)
            """,
            "keyword_meeting_exact": """
                SELECT COUNT(*) AS n
                FROM (SELECT DISTINCT product_name p FROM jw_brand_activity_stage.km_keyword_event_stage) k
                JOIN (SELECT DISTINCT product_name p FROM jw_brand_activity_stage.km_meeting_event_stage) m USING (p)
            """,
            "all_three_exact": """
                SELECT COUNT(*) AS n
                FROM (SELECT DISTINCT master_product p FROM jw_brand_activity_stage.csd_channel_dynamics_stage) c
                JOIN (SELECT DISTINCT product_name p FROM jw_brand_activity_stage.km_keyword_event_stage) k USING (p)
                JOIN (SELECT DISTINCT product_name p FROM jw_brand_activity_stage.km_meeting_event_stage) m USING (p)
            """,
        }
        distincts: dict[str, int] = {}
        for key, sql in queries.items():
            cur.execute(sql)
            distincts[key] = int(cur.fetchone()["n"])
        cur.execute(
            """
            SELECT therapeutic_class, COUNT(DISTINCT product_name) AS product_count
            FROM jw_brand_activity_stage.km_keyword_event_stage
            WHERE product_name NOT IN (
              SELECT DISTINCT master_product FROM jw_brand_activity_stage.csd_channel_dynamics_stage
            )
            GROUP BY therapeutic_class ORDER BY therapeutic_class
            """
        )
        missing_by_atc4 = {str(row["therapeutic_class"]): int(row["product_count"]) for row in cur.fetchall()}
        cur.execute(
            """
            SELECT DISTINCT product_name, therapeutic_class
            FROM jw_brand_activity_stage.km_keyword_event_stage
            WHERE product_name NOT IN (
              SELECT DISTINCT master_product FROM jw_brand_activity_stage.csd_channel_dynamics_stage
            )
            ORDER BY therapeutic_class, product_name
            """
        )
        missing_products = [
            {"product_name": str(row["product_name"]), "therapeutic_class": str(row["therapeutic_class"])}
            for row in cur.fetchall()
        ]
    return {
        "row_counts": row_counts,
        "schemas": schemas,
        "distincts_and_intersections": distincts,
        "keyword_missing_csd_by_atc4": missing_by_atc4,
        "keyword_missing_csd_products": missing_products,
    }


def _row_value(row: dict[str, str], *keys: str) -> str:
    lowered = {key.lower(): value for key, value in row.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value is not None:
            return value.strip()
    return ""


def _ordered(counter: Counter[str], limit: int = 8) -> tuple[str, ...]:
    return tuple(value for value, _ in counter.most_common(limit) if value)


def load_nsa_evidence(nsa_dir: Path, target_anchors: set[str]) -> NsaEvidence:
    kr_names: defaultdict[str, Counter[str]] = defaultdict(Counter)
    atc4: defaultdict[str, Counter[str]] = defaultdict(Counter)
    manufacturers: defaultdict[str, Counter[str]] = defaultdict(Counter)
    molecules: defaultdict[str, Counter[str]] = defaultdict(Counter)
    matched_rows = 0
    scanned_rows = 0
    files = sorted(nsa_dir.glob("*.csv"))
    for path in files:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                scanned_rows += 1
                anchor = normalize_iqvia_en(_row_value(row, "PRODUCT NAME"))
                if anchor not in target_anchors:
                    continue
                matched_rows += 1
                kr_names[anchor][_row_value(row, "PRODUCT NAME KOR")] += 1
                atc4[anchor][_row_value(row, "ATC 4 CODE", "ATC 4")] += 1
                manufacturers[anchor][_row_value(row, "MFR NAME KOR", "MFR NAME")] += 1
                molecules[anchor][_row_value(row, "MOLECULE DESC")] += 1
    kor_evidence = {
        anchor: KorEvidence(_ordered(counter, 1)[0], "nsa_product_name_kor", "data/IQVIA/NSA/*.csv")
        for anchor, counter in kr_names.items()
        if _ordered(counter, 1)
    }
    return NsaEvidence(
        kor_evidence=kor_evidence,
        atc4_by_anchor={anchor: _ordered(counter) for anchor, counter in atc4.items()},
        manufacturer_by_anchor={anchor: _ordered(counter) for anchor, counter in manufacturers.items()},
        molecule_by_anchor={anchor: _ordered(counter) for anchor, counter in molecules.items()},
        summary={
            "nsa_files": [str(path) for path in files],
            "scanned_rows": scanned_rows,
            "matched_stage_product_rows": matched_rows,
            "anchors_with_kor": len(kor_evidence),
        },
    )

