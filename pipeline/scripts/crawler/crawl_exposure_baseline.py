"""Read and persist the central Agent2 exposure baseline for a crawl run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pipeline.etl.io.mart.agent2_eligibility import (
    AGENT2_ALLOWED_DERIVATIONS,
    AGENT2_ALLOWED_PROCESSORS,
    AGENT2_ELIGIBILITY_REVISION,
    agent2_eligibility_sql_predicate,
)


class BaselineOrphanError(RuntimeError):
    """Central-policy score rows without ``news_raw`` are never ignored."""


@dataclass(frozen=True, slots=True)
class EligibleBaselineRows:
    rows: tuple[dict[str, str], ...]
    eligibility_revision: str
    orphan_count: int


def load_eligible_baseline_rows(conn: Any) -> EligibleBaselineRows:
    """Load distinct central-eligible brand/news identities with an orphan gate."""

    processor_marks = ", ".join("%s" for _ in AGENT2_ALLOWED_PROCESSORS)
    derivation_marks = ", ".join("%s" for _ in AGENT2_ALLOWED_DERIVATIONS)
    orphan_params: tuple[object, ...] = (
        *AGENT2_ALLOWED_PROCESSORS,
        *AGENT2_ALLOWED_DERIVATIONS,
    )
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT COUNT(*) AS orphans
            FROM event_brand_scores s
            LEFT JOIN news_raw n ON n.news_id = s.news_id
            WHERE s.source_processor IN ({processor_marks})
              AND s.derivation IN ({derivation_marks})
              AND n.news_id IS NULL
            """,
            orphan_params,
        )
        orphan_count = int(cursor.fetchone()["orphans"] or 0)
    if orphan_count:
        raise BaselineOrphanError(
            f"central Agent2 score rows have no news_raw parent: orphan_count={orphan_count}"
        )

    predicate, params = agent2_eligibility_sql_predicate("s", "n")
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT DISTINCT s.brand_canonical, s.news_id
            FROM event_brand_scores s
            JOIN news_raw n ON n.news_id = s.news_id
            WHERE s.brand_canonical IS NOT NULL
              AND TRIM(s.brand_canonical) <> ''
              AND ({predicate})
            ORDER BY s.brand_canonical, s.news_id
            """,
            params,
        )
        rows = tuple(
            {
                "brand_canonical": str(row["brand_canonical"]),
                "news_id": str(row["news_id"]),
            }
            for row in cursor.fetchall()
        )
    return EligibleBaselineRows(
        rows=rows,
        eligibility_revision=AGENT2_ELIGIBILITY_REVISION,
        orphan_count=0,
    )
