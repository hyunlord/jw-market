from __future__ import annotations

import json

from .config import CompetitorConfig
from .market_context_builder import _filter_history, _json_load, _mat_12m


def _metric_template():
    return {"latest_3m": {}, "mat_12m": {"latest_month": None, "value": None, "raw_value_12m": None, "growth_yoy": None}}


def build_competitor_context(
    brand: str,
    competitors: list,
    primary_market_id: str,
    db_conn,
    snapshot_at,
    config: CompetitorConfig,
) -> dict:
    competitor_items = []
    snapshot_sql = snapshot_at.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    snapshot_date = snapshot_at.date().isoformat()

    for competitor in competitors:
        with db_conn.cursor() as cur:
            cur.execute(
                """
                SELECT brand_name, source, measure, is_jw, metric_history, raw_value_history
                FROM mart_strategic_ml_brand_metric
                WHERE brand_name = %s
                  AND ml_id = %s
                  AND computed_at <= %s
                ORDER BY source ASC, measure ASC
                """,
                (competitor, primary_market_id, snapshot_sql),
            )
            metric_rows = cur.fetchall()

            cur.execute(
                """
                SELECT s.news_id, n.published_date, s.score, n.title, s.summary
                FROM event_brand_scores s
                JOIN news_raw n ON s.news_id = n.news_id
                WHERE (s.brand_canonical = %s OR s.brand_name = %s)
                  AND s.derivation = 'llm_direct'
                  AND s.source_processor = 'workflow_196_optionB'
                  AND s.score >= %s
                  AND n.published_date >= DATE_SUB(%s, INTERVAL %s MONTH)
                  AND n.published_date <= %s
                ORDER BY s.score DESC, n.published_date DESC, s.news_id ASC
                LIMIT %s
                """,
                (
                    competitor,
                    competitor,
                    config.recent_high_score_event_min,
                    snapshot_date,
                    config.latest_n_months,
                    snapshot_date,
                    config.recent_high_score_event_max_count,
                ),
            )
            event_rows = cur.fetchall()

        metrics = {}
        is_jw = False
        for row in metric_rows:
            key = f"{row['source']}.{row['measure']}"
            metric_history = _json_load(row["metric_history"])
            raw_history = _json_load(row["raw_value_history"])
            metrics[key] = {
                "latest_3m": _filter_history(metric_history, snapshot_at, config.latest_n_months),
                "mat_12m": _mat_12m(metric_history, raw_history, snapshot_at) if config.include_mat_12m else {},
            }
            is_jw = is_jw or bool(row.get("is_jw"))

        recent_events = [
            {
                "news_id": row["news_id"],
                "published_date": str(row["published_date"]) if row["published_date"] else None,
                "score": int(row["score"]),
                "title": row.get("title") or "",
                "summary": row.get("summary") or "",
            }
            for row in event_rows
        ]

        competitor_items.append(
            {
                "name": competitor,
                "is_jw": is_jw,
                "brand_metrics_summary": metrics,
                "recent_high_score_events": recent_events,
            }
        )

    return {"competitors": competitor_items}
