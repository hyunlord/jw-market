from __future__ import annotations

from pathlib import Path

from pipeline.scripts.ingest_hook.rehearsal_pipeline import build_full_rehearsal


def test_full_rehearsal_is_no_write_and_uses_real_structure_validators(
    tmp_path: Path,
) -> None:
    report = build_full_rehearsal(tmp_path / "rehearsal")

    assert report.no_production_writes is True
    assert report.semantic_replay_matches is True
    assert report.iqvia.history_quarters == 19
    assert report.iqvia.latest_quarters == ("2025-Q4",)
    assert report.iqvia.activation.dry_run is True
    assert report.iqvia.activation.published is False
    assert report.iqvia.activation.nsa_quarters == 20
    assert report.iqvia.history_validation.detail == "iqvia_loader.iter_nsa_xlsx"
    assert report.iqvia.latest_validation.periods == frozenset({"2025-Q4"})
    assert report.iqvia.shuffled_columns_identical is True

    assert report.ubist.category == "ubist"
    assert report.ubist.epoch == "2026-08"
    assert report.ubist.validation_rows == 3
    assert report.ubist.actions == (
        "validate manifest with G3",
        "load next-month source into isolated staging target",
        "defer mart publish until explicit production activation",
    )

    assert report.csd_channel.activation.dry_run is True
    assert report.csd_channel.activation.published is False
    assert report.csd_channel.validation.detail == "csd_core.iter_market_rows"
    assert report.csd_channel.activation.target_tables == (
        "jw_ingest_shadow_csd_raw.raw_csd_channel_dynamics",
        "jw_ingest_shadow_csd_stage.csd_channel_dynamics_stage",
    )

    assert report.csd_keyword.activation.dry_run is True
    assert report.csd_keyword.activation.published is False
    assert report.csd_keyword.validation.detail == "ingest_keyword.read_keyword_events"
    assert report.csd_keyword.activation.target_tables == (
        "jw_ingest_shadow_csd_raw.raw_keyword_events",
        "jw_ingest_shadow_csd_stage.km_keyword_event_stage",
    )
