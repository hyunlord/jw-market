from __future__ import annotations


def _event_row(row: dict) -> dict:
    return {
        "news_id": row["news_id"],
        "published_date": str(row["published_date"]) if row.get("published_date") else None,
        "score": int(row["score"]),
        "tag": row.get("tag"),
        "title": row.get("title") or "",
        "summary": row.get("summary") or "",
        "source_name": row.get("source_name"),
    }


def _fetch_events(brand_name: str, snapshot_at, config, db_conn) -> list[dict]:
    events_cfg = config.competitor.events or {}
    min_score = int(events_cfg.get("min_score", config.competitor.recent_high_score_event_min))
    max_count = int(events_cfg.get("max_count_per_competitor", config.competitor.recent_high_score_event_max_count))
    lookback_months = int(events_cfg.get("lookback_months", config.competitor.latest_n_months))
    snapshot_date = snapshot_at.date().isoformat()
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.news_id, n.published_date, s.score, s.tag, n.title, s.summary, n.source_name
            FROM event_brand_scores s
            JOIN news_raw n ON s.news_id = n.news_id
            WHERE (s.brand_canonical = %s OR s.brand_name = %s)
              AND s.source_processor IN ('workflow_196_optionB', 'cross_match_adapter_v1')
              AND s.score >= %s
              AND n.published_date >= DATE_SUB(%s, INTERVAL %s MONTH)
              AND n.published_date <= %s
            ORDER BY s.score DESC, n.published_date DESC, s.news_id ASC
            LIMIT %s
            """,
            (brand_name, brand_name, min_score, snapshot_date, lookback_months, snapshot_date, max_count),
        )
        rows = cur.fetchall()
    return [_event_row(row) for row in rows]


def build_competitor_events(
    competitors_by_source: dict,
    snapshot_at,
    config,
    db_conn,
) -> dict:
    result = {"by_source": {}}
    for source in sorted(competitors_by_source, key=lambda value: {"UBIST": 0, "IQVIA": 1}.get(value, 99)):
        competitors = []
        for item in competitors_by_source[source]:
            competitors.append(
                {
                    "brand_name": item["brand_name"],
                    "rank_in_market": item.get("rank_in_market"),
                    "is_jw": bool(item.get("is_jw")),
                    "events": _fetch_events(item["brand_name"], snapshot_at, config, db_conn),
                }
            )
        result["by_source"][source] = {"competitors": competitors}
    return result
