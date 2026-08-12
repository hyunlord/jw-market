from __future__ import annotations

from pipeline.scripts.analysis.brand_activity.auto_topic import row_topic_identity
from pipeline.scripts.analysis.brand_activity.auto_topic import row_topic_sql


def _semantic_fields() -> row_topic_identity.SemanticEventFields:
    return {
        "period_ym": "2026-05",
        "visit_location": "HOSPITAL",
        "specialty": "Cardiology",
        "representing_company": "JW",
        "product_name": "LIVAROZET",
        "therapeutic_class": "C10A1",
        "keyword_text": "LDL-C control",
        "interest": "VERY USEFUL",
        "prescription_frequency": "monthly",
        "prescription_evolution": "increase",
        "abstract_lit": "Y",
        "patient_lit": "N",
        "promotional_lit": "Y",
        "samples_left": "N",
        "other_materials_left": "Y",
        "what_other_materials": "guide",
        "other_comments": "follow up",
    }


def test_semantic_key_is_equal_for_same_content() -> None:
    given = _semantic_fields()

    first = row_topic_identity.semantic_event_key_v1(given)
    second = row_topic_identity.semantic_event_key_v1(dict(given))

    assert first == second


def test_semantic_key_ignores_provenance_only_changes() -> None:
    given = _semantic_fields()
    first = row_topic_identity.StageOccurrenceInput(
        stage_row_id=1,
        semantic_fields=given,
        stage_row_sha256="a" * 64,
        source_file="May26.xlsx",
        source_sheet="Keywords",
        source_row_no=2,
        source_file_sha256="b" * 64,
    )
    second = row_topic_identity.StageOccurrenceInput(
        stage_row_id=2,
        semantic_fields=given,
        stage_row_sha256="c" * 64,
        source_file="renamed.xlsx",
        source_sheet="Keyword export",
        source_row_no=99,
        source_file_sha256="d" * 64,
    )

    assert first.semantic_event_key == second.semantic_event_key


def test_semantic_key_changes_when_one_semantic_field_changes() -> None:
    before = _semantic_fields()
    after = {**before, "keyword_text": "different message"}

    assert row_topic_identity.semantic_event_key_v1(before) != row_topic_identity.semantic_event_key_v1(after)


def test_semantic_key_is_independent_of_mapping_order() -> None:
    ordered = _semantic_fields()
    reversed_order = dict(reversed(tuple(ordered.items())))

    assert row_topic_identity.canonical_semantic_json_v1(ordered) == row_topic_identity.canonical_semantic_json_v1(reversed_order)
    assert row_topic_identity.semantic_event_key_v1(ordered) == row_topic_identity.semantic_event_key_v1(reversed_order)


def test_duplicate_semantic_occurrences_remain_two_bridge_rows() -> None:
    fields = _semantic_fields()
    occurrences = (
        row_topic_identity.StageOccurrenceInput(
            stage_row_id=10,
            semantic_fields=fields,
            stage_row_sha256="a" * 64,
            source_file="May26.xlsx",
            source_sheet="Keywords",
            source_row_no=10,
            source_file_sha256="b" * 64,
        ),
        row_topic_identity.StageOccurrenceInput(
            stage_row_id=11,
            semantic_fields=fields,
            stage_row_sha256="c" * 64,
            source_file="May26.xlsx",
            source_sheet="Keywords",
            source_row_no=11,
            source_file_sha256="b" * 64,
        ),
    )

    bridge_rows = row_topic_identity.bridge_rows(occurrences)

    assert len(bridge_rows) == 2
    assert len({row.stage_row_id for row in bridge_rows}) == 2
    assert len({row.semantic_event_key_v1 for row in bridge_rows}) == 1


def test_shadow_ddl_preserves_occurrences_and_legacy_contract() -> None:
    bridge = row_topic_sql.stage_occurrence_table_ddl("jw_brand_activity_stage")
    assignment = row_topic_sql.semantic_assignment_table_ddl("jw_brand_activity_stage")
    status = row_topic_sql.semantic_assignment_status_table_ddl("jw_brand_activity_stage")
    legacy_view = row_topic_sql.compatible_share_view_sql("jw_brand_activity_stage")

    assert "UNIQUE (semantic_event_key_v1)" not in bridge
    assert "FOREIGN KEY" not in bridge
    assert "ON DELETE CASCADE" not in bridge
    assert "PRIMARY KEY (stage_generation_id, stage_row_id)" in bridge
    assert "PRIMARY KEY (semantic_event_key_v1, scope_id, topic_set_version, topic_id)" in assignment
    assert "PRIMARY KEY (semantic_event_key_v1, scope_id, topic_set_version)" in status
    assert "classified_stage_generation_id" in status
    assert "COUNT(DISTINCT a.row_id)" in legacy_view
