from __future__ import annotations

from .repository import Connection


def load_recent_parse_counts(
    conn: Connection,
    *,
    window_runs: int,
) -> tuple[tuple[int, int], ...]:
    """Load completed-run counts used by the configurable alert window."""

    if window_runs <= 0:
        raise ValueError("window_runs must be positive")
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT parsed_count, failed_count
            FROM hira_benefit_crawl_run
            WHERE status = 'complete'
            ORDER BY finished_at DESC, run_id DESC
            LIMIT %s
            """,
            (window_runs,),
        )
        rows = cursor.fetchall()
    return tuple(
        (int(row["parsed_count"]), int(row["failed_count"]))
        for row in rows
    )


def record_alert_status(
    conn: Connection,
    *,
    run_id: str,
    alert_status: str,
) -> None:
    """Persist alert delivery state without changing crawl success state."""

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE hira_benefit_crawl_run SET alert_status=%s WHERE run_id=%s",
                (alert_status, run_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
