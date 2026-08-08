from __future__ import annotations

from pipeline.etl.stages import s4_mart


def test_s4_nsa_build_uses_single_serving_seed_and_reads_build_raw(monkeypatch) -> None:
    observed: list[tuple[str, object]] = []
    monkeypatch.setattr(
        s4_mart, "_ensure_isolated_schema", lambda target, source: observed.append(("ensure", (target, source)))
    )
    monkeypatch.setattr(
        s4_mart, "_configure_mart_env", lambda target, source: observed.append(("env", (target, source)))
    )
    monkeypatch.setattr(
        "pipeline.etl.io.mart.layer3_compute_general_v3.compute_general",
        lambda source, **_kwargs: ([], [], {"brand_rows": 1, "market_rows": 1, "measures": 4}),
    )

    rc = s4_mart.run(
        {
            "target_db": "jw_ingest_nsa_build_run1",
            "source_db": "jw_mart_d2",
            "input_db": "jw_ingest_nsa_build_run1",
            "sources": ("iqvia_nsa",),
        }
    )

    assert rc == 0
    assert observed == [
        ("ensure", ("jw_ingest_nsa_build_run1", "jw_mart_d2")),
        ("env", ("jw_ingest_nsa_build_run1", "jw_ingest_nsa_build_run1")),
    ]
