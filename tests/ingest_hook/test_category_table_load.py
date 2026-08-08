from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.scripts.ingest_hook import category_table_load, config
from pipeline.scripts.ingest_hook.row_count_verifier import LoadKind, RowCountEvidence


def _evidence(table: str, *, loaded: int = 3) -> RowCountEvidence:
    return RowCountEvidence(
        schema="jw_ingest_stage_test",
        table=table,
        kind=LoadKind.APPEND,
        rows_before=10,
        rows_after=10 + loaded,
        rows_loaded=loaded,
        source_rows=loaded,
        difference_reasons=(),
    )


@pytest.mark.parametrize(
    ("category", "expected_loader"),
    [
        ("iqvia_nsa", "iqvia_loader"),
        ("iqvia_csd_channel", "brand_activity_raw_db:csd"),
        ("iqvia_csd_keyword", "brand_activity_raw_db:keyword"),
    ],
)
def test_connected_category_writes_a_verified_table_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    category: str,
    expected_loader: str,
) -> None:
    source = tmp_path / "source.xlsx"
    source.write_bytes(b"fixture")
    target = tmp_path / "target"
    seen: list[str] = []

    def fake_load(request: category_table_load.LoadRequest) -> category_table_load.LoadOutcome:
        seen.append(request.category)
        return category_table_load.LoadOutcome(
            loader=expected_loader,
            primary=_evidence("raw_table"),
            tables=(_evidence("raw_table"),),
        )

    monkeypatch.setattr(category_table_load, "_LOADERS", {category: fake_load})
    monkeypatch.setenv(config.ENV_LOAD_STAGING_DB, "jw_ingest_stage_test")

    result = category_table_load.load(category, source, target, "2026-03")

    assert seen == [category]
    assert result["rows_loaded"] == 3
    manifest = json.loads((target / "_manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "ingest-table-load-v1"
    assert manifest["loader"] == expected_loader
    assert manifest["primary"]["rows_before"] == 10
    assert manifest["primary"]["rows_after"] == 13
    assert manifest["primary"]["rows_loaded"] == 3


def test_batch_sources_reach_one_loader_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sources = tuple(tmp_path / f"source-{index}.xlsx" for index in range(2))
    for source in sources:
        source.write_bytes(b"fixture")
    seen: list[tuple[Path, ...]] = []

    def fake_load(request: category_table_load.LoadRequest) -> category_table_load.LoadOutcome:
        seen.append(request.sources)
        evidence = _evidence("raw_table")
        return category_table_load.LoadOutcome("iqvia_loader", evidence, (evidence,))

    monkeypatch.setattr(category_table_load, "_LOADERS", {"iqvia_nsa": fake_load})
    monkeypatch.setenv(config.ENV_LOAD_STAGING_DB, "jw_ingest_stage_test")

    category_table_load.load("iqvia_nsa", sources, tmp_path / "target", "2026-Q1")

    assert seen == [sources]


def test_nsa_row_count_uses_loader_first_pass_without_reparsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipeline.etl.io import iqvia_loader

    source = tmp_path / "source.xlsx"
    source.write_bytes(b"fixture")
    request = category_table_load.LoadRequest(
        category="iqvia_nsa",
        sources=(source,),
        target_dir=tmp_path / "target",
        epoch="2026-Q1",
        target_db="jw_ingest_stage_test",
    )
    counts = iter((0, 7))
    monkeypatch.setattr(category_table_load, "_count_rows", lambda *_args: next(counts))
    monkeypatch.setattr(
        iqvia_loader,
        "load_source",
        lambda *_args, **_kwargs: iqvia_loader.LoadStats(
            rows=7, source_rows=9, files=1, sheets=1
        ),
    )
    monkeypatch.setattr(
        iqvia_loader,
        "iter_records",
        lambda _path: pytest.fail("NSA source must not be parsed a second time"),
    )

    outcome = category_table_load._load_nsa(request)

    assert outcome.primary.rows_loaded == 7
    assert outcome.primary.source_rows == 9
    assert outcome.primary.difference_reasons == ("duplicate_or_previously_loaded=2",)


def test_non_isolated_database_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.xlsx"
    source.write_bytes(b"fixture")
    monkeypatch.setenv(config.ENV_LOAD_STAGING_DB, "jw_mart_d2")

    with pytest.raises(category_table_load.TableLoaderUnavailableError, match="non-isolated"):
        category_table_load.load("iqvia_nsa", source, tmp_path / "target", "2026-Q1")


def test_activation_overlay_database_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(config.ENV_LOAD_STAGING_DB, "jw_mart_ingest_run1")

    assert category_table_load._isolated_target_db() == "jw_mart_ingest_run1"


def test_keyword_activation_candidate_database_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = "jw_brand_activity_keyword_20260808121816001106"
    monkeypatch.setenv(config.ENV_LOAD_STAGING_DB, candidate)

    assert category_table_load._isolated_target_db() == candidate


def test_keyword_live_database_is_still_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(config.ENV_LOAD_STAGING_DB, "jw_brand_activity_stage")

    with pytest.raises(category_table_load.TableLoaderUnavailableError, match="non-isolated"):
        category_table_load._isolated_target_db()


def test_mi_master_requires_exactly_one_workbook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(config.ENV_LOAD_STAGING_DB, "jw_ingest_test")

    with pytest.raises(category_table_load.TableLoaderUnavailableError, match="exactly one"):
        category_table_load.load("mi_master", [], tmp_path / "target", "2026-03")


def test_unknown_category_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "source.xlsx"
    source.write_bytes(b"fixture")

    with pytest.raises(category_table_load.TableLoaderUnavailableError, match="no table loader"):
        category_table_load.load("unknown", source, tmp_path / "target", "2026-03")
