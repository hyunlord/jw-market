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
              AND s.source_processor IN ('workflow_196_optionB', 'workflow_196_rev5674', 'cross_match_adapter_v1')
              AND s.tag <> '기타'
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
    competitors_by_view: dict,
    snapshot_at,
    config,
    db_conn,
) -> dict:
    result = {"by_source": {}, "by_view": {}}
    event_cache: dict[str, list[dict]] = {}

    def sort_key(view_id: str) -> tuple:
        view_order = {"ML": 0, "CD": 1}
        source_order = {"UBIST": 0, "IQVIA": 1}
        measure_order = {"sales": 0, "volume": 1, "unit": 2, "dosage_unit": 3, "counting_unit": 4}
        short, source, measure = (view_id.split(".", 2) + ["", "", ""])[:3]
        return (view_order.get(short, 99), source_order.get(source, 99), measure_order.get(measure, 99), measure)

    for view_id in sorted(competitors_by_view, key=sort_key):
        view_payload = competitors_by_view[view_id] or {}
        source = str(view_payload.get("source") or view_id.split(".")[1]).upper()
        view = view_payload.get("view")
        competitors = []
        for item in view_payload.get("competitors", []) or []:
            brand_name = item["brand_name"]
            if brand_name not in event_cache:
                event_cache[brand_name] = _fetch_events(brand_name, snapshot_at, config, db_conn)
            competitors.append(
                {
                    "brand_name": brand_name,
                    "rank_in_market": item.get("rank_in_market"),
                    "is_jw": bool(item.get("is_jw")),
                    "events": event_cache[brand_name],
                }
            )
        result["by_view"][view_id] = {
            "view_id": view_id,
            "view": view,
            "source": source,
            "competitors": competitors,
        }
        if source not in result["by_source"]:
            result["by_source"][source] = {
                "source": source,
                "view_id": view_id,
                "view": view,
                "competitors": competitors,
            }
    return result
