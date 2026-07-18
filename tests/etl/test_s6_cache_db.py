from __future__ import annotations

from pipeline.etl.io.cache import db


def test_copy_inputs_includes_general_dimension_sidecar(monkeypatch) -> None:
    copied: list[tuple[str, str, str]] = []

    def fake_copy_table(source: str, target: str, table: str, **_kwargs: object) -> int:
        copied.append((source, target, table))
        return 1

    monkeypatch.setattr(db, "copy_table", fake_copy_table)

    counts = db.copy_inputs(
        general_db="general_rehearsal",
        strategic_db="strategic_rehearsal",
        target_db="cache_rehearsal",
        event_db="event_reference",
    )

    assert (
        "general_rehearsal",
        "cache_rehearsal",
        "mart_general_filter_dimension_metric",
    ) in copied
    assert counts["mart_general_filter_dimension_metric"] == 1
    assert len(copied) == 10
