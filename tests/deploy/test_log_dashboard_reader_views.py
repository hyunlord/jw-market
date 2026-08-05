from pathlib import Path


SQL = Path("deploy/k8s/jw-market/log-dashboard-reader-views.sql").read_text()
REVOKE_SQL = Path("deploy/k8s/jw-market/log-dashboard-reader-revoke-raw.sql").read_text()


def test_dashboard_views_exclude_conversation_content_and_auth_details() -> None:
    prefix, detail_and_tail = SQL.split("dashboard_chat_turn_detail_v` AS", 1)
    _, tail = detail_and_tail.split("CREATE OR REPLACE SQL SECURITY DEFINER VIEW", 1)
    lowered = (prefix + "CREATE OR REPLACE SQL SECURITY DEFINER VIEW" + tail).lower()
    assert "question_text" not in lowered
    assert "answer_text" not in lowered
    assert "select\n    `id`, `actor_uid`, `actor_type`, `called_at`, `endpoint`, `request_params`" not in lowered
    assert "`request_params` as" not in lowered
    assert "`ip`" not in lowered
    assert "`description`" not in lowered
    assert "`note`" not in lowered


def test_reader_receives_views_only_not_raw_tables() -> None:
    grant_lines = [line.strip() for line in SQL.splitlines() if line.strip().startswith("GRANT SELECT")]
    assert len(grant_lines) == 7
    assert all("dashboard_" in line for line in grant_lines)
    assert "GRANT INSERT" not in SQL
    assert "GRANT UPDATE" not in SQL
    assert "GRANT DELETE" not in SQL


def test_reader_raw_table_privileges_are_explicitly_revoked() -> None:
    assert "REVOKE SELECT ON `jw_market_audit_stage`.`audit_api_call_log`" in REVOKE_SQL
    assert "REVOKE SELECT ON `llmops`.`user_tb`" in REVOKE_SQL
    assert "dashboard_" not in REVOKE_SQL


def test_chat_view_joins_by_full_deterministic_key() -> None:
    assert "o.`source_log_id` = c.`id` AND o.`projection_version` = 1" in SQL
    assert "COUNT(DISTINCT `chat_service_id`) = 1" in SQL
    assert "GROUP BY `uid`" in SQL


def test_chat_detail_view_is_trace_backed_and_answer_only() -> None:
    detail_sql = SQL.split("dashboard_chat_turn_detail_v` AS", 1)[1].split(
        "CREATE OR REPLACE SQL SECURITY DEFINER VIEW", 1
    )[0]

    assert "COALESCE(o.`trace_id`, CONCAT('conversation-log:', c.`id`)) AS `detail_key`" in detail_sql
    assert "o.`source_log_id` = c.`id` AND o.`projection_version` = 1" in detail_sql
    assert "r.`trace_id` AS `detail_key`" in detail_sql
    assert "r.`source_system` = 'genos_monitoring' AND r.`service_id` = 61" in detail_sql
    assert detail_sql.count("`question_text`") == 2
    assert detail_sql.count("`answer_text`") == 2
    assert "GRANT SELECT ON `jw_market_audit_stage`.`dashboard_chat_turn_detail_v`" in SQL


def test_user_directory_view_exposes_only_dashboard_dimensions() -> None:
    view_sql = SQL.split("dashboard_user_directory_v` AS", 1)[1].split(
        "CREATE OR REPLACE SQL SECURITY DEFINER VIEW", 1
    )[0]
    assert "`id`, `user_id`, `name`, `department`, `group_name`" in view_sql
    for sensitive_column in ("password", "email", "refresh_token", "allowed_ip"):
        assert sensitive_column not in view_sql.lower()


def test_api_view_exposes_only_explicit_request_options_allowlist() -> None:
    view_sql = SQL.split("dashboard_api_usage_v` AS", 1)[1].split(
        "CREATE OR REPLACE SQL SECURITY DEFINER VIEW", 1
    )[0]
    lowered = view_sql.lower()

    assert "`request_options`" in view_sql
    for allowed in (
        "$.path.brand_name",
        "$.query.market_id",
        "$.query.view",
        "$.query.source",
        "$.query.measure",
        "$.query.brand",
        "$.query.atc4_codes",
        "$.body.selected_brand",
        "$.body.filters.analysis_level",
        "$.body.options.period_range",
    ):
        assert allowed in view_sql
    for forbidden in (
        "$.query.q",
        "$.query.query",
        "$.query.audit_probe",
        "$.query.cursor",
        "$.query.user_id",
        "$.body.user_id",
        "$.body.auth",
        "$.body.token",
        "$.body.text",
    ):
        assert forbidden not in lowered
