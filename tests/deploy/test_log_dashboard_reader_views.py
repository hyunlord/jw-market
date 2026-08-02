from pathlib import Path


SQL = Path("deploy/k8s/jw-market/log-dashboard-reader-views.sql").read_text()


def test_dashboard_views_exclude_conversation_content_and_auth_details() -> None:
    lowered = SQL.lower()
    assert "question_text" not in lowered
    assert "answer_text" not in lowered
    assert "request_params" not in lowered
    assert "`ip`" not in lowered
    assert "`description`" not in lowered
    assert "`note`" not in lowered


def test_reader_receives_views_only_not_raw_tables() -> None:
    grant_lines = [line.strip() for line in SQL.splitlines() if line.strip().startswith("GRANT SELECT")]
    assert len(grant_lines) == 5
    assert all("dashboard_" in line for line in grant_lines)
    assert "GRANT INSERT" not in SQL
    assert "GRANT UPDATE" not in SQL
    assert "GRANT DELETE" not in SQL


def test_chat_view_joins_by_full_deterministic_key() -> None:
    assert "o.`source_log_id` = c.`id` AND o.`projection_version` = 1" in SQL
