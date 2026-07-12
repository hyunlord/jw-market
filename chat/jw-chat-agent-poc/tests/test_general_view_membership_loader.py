from __future__ import annotations

from scripts.build_general_view_membership import (
    BUILD_TABLE,
    TARGET_TABLE,
    build_membership_rows,
    create_table_sql,
)


def test_build_membership_rows_deduplicates_metric_rows_without_losing_sources() -> None:
    rows = [
        {"brand_key": "mounjaro", "brand_name": "마운자로", "atc4_code": "a10s0", "atc4_desc": "GLP-1", "source": "IQVIA_NSA"},
        {"brand_key": "mounjaro", "brand_name": "마운자로", "atc4_code": "a10s0", "atc4_desc": "GLP-1", "source": "IQVIA_NSA"},
        {"brand_key": "mounjaro", "brand_name": "마운자로", "atc4_code": "a10s0", "atc4_desc": "GLP-1", "source": "UBIST"},
    ]

    memberships = build_membership_rows(rows)

    assert len(memberships) == 2
    assert {row.source for row in memberships} == {"iqvia", "ubist"}
    assert {row.normalized_brand_name for row in memberships} == {"마운자로"}


def test_membership_table_has_exact_lookup_and_atc4_indexes() -> None:
    ddl = create_table_sql()

    assert "idx_general_membership_name_source (normalized_brand_name, source)" in ddl
    assert "idx_general_membership_atc4_source (atc4_code, source)" in ddl
    assert "metric_history" not in ddl


def test_membership_loader_uses_distinct_shadow_table_names() -> None:
    assert BUILD_TABLE != TARGET_TABLE
    assert BUILD_TABLE.startswith(TARGET_TABLE)
