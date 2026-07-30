from __future__ import annotations

import os

from pipeline.etl.stages import s5_mart


def test_s5_reads_general_rows_from_the_same_isolated_build_schema(monkeypatch) -> None:
    observed: dict[str, str] = {}

    monkeypatch.setattr(s5_mart, "_ensure_isolated_schema", lambda *_args: None)

    def configure(target_db: str, source_db: str) -> None:
        observed["target_db"] = target_db
        observed["configured_source_db"] = source_db
        os.environ["MARIADB_DATABASE"] = target_db
        os.environ["MARIADB_SOURCE_DATABASE"] = source_db

    monkeypatch.setattr(s5_mart, "_configure_mart_env", configure)

    import pipeline.etl.io.mart.strategic_cd as strategic_cd
    import pipeline.etl.io.mart.strategic_ml as strategic_ml

    monkeypatch.setattr(
        strategic_ml,
        "compute_strategic_ml",
        lambda *_args, **_kwargs: (
            None,
            None,
            {"brand_rows": 1, "market_rows": 1, "ml_count": 1},
        ),
    )
    monkeypatch.setattr(
        strategic_cd,
        "compute_strategic_cd",
        lambda *_args, **_kwargs: (
            None,
            None,
            {"brand_rows": 1, "market_rows": 1, "cd_market_count": 1},
        ),
    )

    rc = s5_mart.run(
        {
            "target_db": "jw_mart_ingest_run1",
            "source_db": "jw_mart",
            "general_source_db": "jw_mart_ingest_run1",
        }
    )

    assert rc == 0
    assert observed == {
        "target_db": "jw_mart_ingest_run1",
        "configured_source_db": "jw_mart_ingest_run1",
    }
