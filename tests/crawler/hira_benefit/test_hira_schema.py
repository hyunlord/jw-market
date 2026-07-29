from __future__ import annotations

from pipeline.scripts.crawler.hira_benefit.schema import (
    MIGRATION_PATHS,
    SCHEMA_PATH,
    validate_schema_sql,
)


def test_schema_is_namespaced_and_dry_run_safe() -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    result = validate_schema_sql(sql)

    assert result.tables == (
        "hira_benefit_notice",
        "hira_benefit_notice_brand",
        "hira_benefit_crawl_run",
        "hira_benefit_crawl_state",
    )
    assert result.destructive_statements == ()
    assert "raw_text LONGTEXT NOT NULL" in sql
    assert "parse_status VARCHAR(16) NOT NULL" in sql
    assert "target_status VARCHAR(16) NOT NULL" in sql
    assert "exclusion_status VARCHAR(16) NOT NULL" in sql
    assert "dosage_status VARCHAR(16) NOT NULL" in sql
    assert "listing_fingerprint CHAR(64) NOT NULL" in sql
    assert "KEY idx_hira_notice_date" in sql
    assert "KEY idx_hira_brand_name" in sql


def test_field_status_migration_is_additive_and_dry_run_safe() -> None:
    assert len(MIGRATION_PATHS) == 1
    sql = MIGRATION_PATHS[0].read_text(encoding="utf-8")
    result = validate_schema_sql(sql)

    assert result.destructive_statements == ()
    assert "ALTER TABLE hira_benefit_notice" in sql
    assert "ADD COLUMN target_status" in sql
    assert "ADD COLUMN exclusion_status" in sql
    assert "ADD COLUMN dosage_status" in sql
    assert "UPDATE " not in sql
