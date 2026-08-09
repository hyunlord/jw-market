from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pipeline.scripts.etl.brand_activity.csd_core import CsdRow
from pipeline.scripts.etl.brand_activity.km_core import KeywordEvent
from pipeline.scripts.etl.brand_activity.load_raw_staging import (
    CoverageSources,
    build_stage_plan,
    parse_args,
    discover_combined_source_files,
    discover_scoped_source_files,
    source_collection_by_file,
    target_market_coverage,
)
from pipeline.scripts.etl.brand_activity.raw_schema import RAW_DDL
from pipeline.scripts.etl.brand_activity.raw_extract import CsdSourceRow, resolve_source_roots
from pipeline.scripts.etl.brand_activity.raw_db import SourceRows, quote_stage_name
import pipeline.scripts.etl.brand_activity.raw_db as raw_db
from pipeline.scripts.etl.brand_activity.raw_stage_refresh import refresh_stage
from pipeline.scripts.etl.brand_activity.raw_staging import (
    csd_dedup_key,
    keyword_dedup_key,
    recent_month_window,
)


def test_csd_key_uses_datapoint_grain_when_annual_files_overlap() -> None:
    # Given: two annual CSD files carry the same product_details datapoint.
    old_row = CsdRow(
        source_file="ChannelDynamics_JW Pharma Regional Report_Dec.23.xlsx",
        source_sheet="LIVALO Market",
        source_row_no=8,
        period_ym="2023-12",
        market="LIVALO Market",
        jw_channel="TOTAL",
        master_product="LIVALO",
        representing_company="JW",
        product_details=100,
    )
    newer_row = CsdRow(
        source_file="ChannelDynamics_JW Pharma Regional Report_Dec.24.xlsx",
        source_sheet="LIVALO Market",
        source_row_no=99,
        period_ym="2023-12",
        market="LIVALO Market",
        jw_channel="TOTAL",
        master_product="LIVALO",
        representing_company="JW",
        product_details=100,
    )

    # When: raw staging keys are generated.
    old_key = csd_dedup_key(old_row)
    newer_key = csd_dedup_key(newer_row)

    # Then: provenance does not split the same CSD datapoint.
    assert old_key == newer_key


def test_keyword_key_preserves_duplicate_event_rows() -> None:
    # Given: duplicate-looking source events appear on separate workbook rows.
    keyword_1 = _keyword_event(source_row_no=2)
    keyword_2 = _keyword_event(source_row_no=3)

    # When: raw staging keys are generated.
    keyword_keys = {keyword_dedup_key(keyword_1), keyword_dedup_key(keyword_2)}

    # Then: row identity, not message text, controls Keyword dedup.
    assert len(keyword_keys) == 2


def test_keyword_key_ignores_filename_for_identical_workbook_row() -> None:
    # Given: one workbook was uploaded under a normalized filename without changing its bytes.
    original = _keyword_event(
        source_row_no=2,
        source_file="Keywords for JW Oct. 25.xlsx",
    )
    normalized_copy = replace(original, source_file="Keywords_for_JW_Oct._25.xlsx")

    # When: raw staging keys are generated for the same workbook row.
    original_key = keyword_dedup_key(original)
    normalized_copy_key = keyword_dedup_key(normalized_copy)

    # Then: filename normalization cannot duplicate an otherwise identical source event.
    assert original_key == normalized_copy_key


def test_recent_month_window_is_inclusive_36_months() -> None:
    # Given: Apr.26 is the newest source period across staged data.
    max_period = "2026-04"

    # When: the analysis window is calculated.
    start_period, end_period = recent_month_window(max_period, months=36)

    # Then: the 36 inclusive months start at May.23.
    assert (start_period, end_period) == ("2023-05", "2026-04")


def test_combined_discovery_keeps_csd_new_only_while_adding_legacy_keyword(tmp_path: Path) -> None:
    # Given: new CSD folders and legacy CSD2 folders with monthly Keyword files.
    new_root = tmp_path / "CSD"
    legacy_root = tmp_path / "CSD2"
    _touch_source(new_root / "ChannelDynamics (콜 수=영업 횟수)" / "ChannelDynamics_JW Pharma Regional Report_Dec.25.xlsx")
    _touch_source(new_root / "Keyword (고객=의사에게 전달한 메시지)" / "Keywords for JW Dec. 25.xlsx")
    _touch_source(new_root / "Keyword (고객=의사에게 전달한 메시지)" / "202605_Keywords for JW May. 26.xlsx")
    _touch_source(legacy_root / "Keyword (고객=의사에게 전달한 메시지)" / "Keywords for JW Jan. 25.xlsx")

    # When: the combined P-0 file set is discovered.
    roots = resolve_source_roots(new_root)
    files = discover_combined_source_files(roots, legacy_root)
    collection_map = source_collection_by_file(files)

    # Then: CSD stays on the new source root, while Keyword includes old+new.
    assert [path.name for path in files["csd"]] == ["ChannelDynamics_JW Pharma Regional Report_Dec.25.xlsx"]
    assert [path.name for path in files["keyword"]] == [
        "Keywords for JW Jan. 25.xlsx",
        "Keywords for JW Dec. 25.xlsx",
        "202605_Keywords for JW May. 26.xlsx",
    ]
    assert "meeting" not in files
    assert collection_map["Keywords for JW Jan. 25.xlsx"] == "old"
    assert collection_map["Keywords for JW Dec. 25.xlsx"] == "new"
    assert collection_map["202605_Keywords for JW May. 26.xlsx"] == "new"


def test_scoped_discovery_csd_does_not_scan_keyword_sources(tmp_path: Path) -> None:
    # Given: both source families exist under the reorganized root.
    new_root = tmp_path / "CSD"
    legacy_root = tmp_path / "CSD2"
    _touch_source(new_root / "ChannelDynamics (콜 수=영업 횟수)" / "ChannelDynamics_JW Pharma Regional Report_Dec.25.xlsx")
    _touch_source(new_root / "Keyword (고객=의사에게 전달한 메시지)" / "202605_Keywords for JW May. 26.xlsx")
    _touch_source(legacy_root / "Keyword (고객=의사에게 전달한 메시지)" / "Keywords for JW Jan. 25.xlsx")

    # When: the loader is scoped to CSD only.
    files = discover_scoped_source_files(resolve_source_roots(new_root), legacy_root, "csd")

    # Then: only CSD workbooks are discovered, so Keyword raw loading cannot start.
    assert sorted(files) == ["csd"]
    assert [path.name for path in files["csd"]] == ["ChannelDynamics_JW Pharma Regional Report_Dec.25.xlsx"]


def test_scoped_discovery_keyword_does_not_scan_csd_sources(tmp_path: Path) -> None:
    # Given: both source families exist under the reorganized root.
    new_root = tmp_path / "CSD"
    legacy_root = tmp_path / "CSD2"
    _touch_source(new_root / "ChannelDynamics (콜 수=영업 횟수)" / "ChannelDynamics_JW Pharma Regional Report_Dec.25.xlsx")
    _touch_source(new_root / "Keyword (고객=의사에게 전달한 메시지)" / "202605_Keywords for JW May. 26.xlsx")
    _touch_source(legacy_root / "Keyword (고객=의사에게 전달한 메시지)" / "Keywords for JW Jan. 25.xlsx")

    # When: the loader is scoped to Keyword only.
    files = discover_scoped_source_files(resolve_source_roots(new_root), legacy_root, "keyword")

    # Then: only Keyword workbooks are discovered.
    assert sorted(files) == ["keyword"]
    assert [path.name for path in files["keyword"]] == ["Keywords for JW Jan. 25.xlsx", "202605_Keywords for JW May. 26.xlsx"]


def test_csd_scope_plan_leaves_keyword_stage_untouched() -> None:
    # Given: parsed CSD and Keyword rows are both available.
    rows = SourceRows(
        csd=[
            _csd_source_row(period_ym="2026-05", product_details=1),
            _csd_source_row(period_ym="2026-05", product_details=2, selected_for_stage=False),
        ],
        keyword=[_keyword_event(source_row_no=2, product_name="LIVALO")],
    )

    # When: the dry-run execution plan is built for CSD only.
    plan = build_stage_plan(rows, ("2026-05", "2026-05"), "csd")

    # Then: the plan has no Keyword raw or stage target.
    assert plan["keyword_stage_untouched"] is True
    assert plan["raw_insert_targets"] == ["raw_csd_channel_dynamics"]
    assert plan["truncate_targets"] == ["csd_channel_dynamics_stage"]
    assert plan["expected_stage_rows"] == {"csd_channel_dynamics_stage": 1}


def test_refresh_stage_csd_scope_never_truncates_or_copies_keyword(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: the Keyword copy helper would fail loudly if the CSD-only path reached it.
    cursor = _RecordingCursor()
    monkeypatch.setattr(
        "pipeline.scripts.etl.brand_activity.raw_stage_refresh._canonical_csd_stage_rows",
        lambda *_args: [_csd_row()],
    )
    monkeypatch.setattr(
        "pipeline.scripts.etl.brand_activity.raw_stage_refresh._copy_keyword_stage",
        lambda *_args: pytest.fail("keyword stage copy should not run in CSD scope"),
    )

    # When: stage refresh is scoped to CSD.
    result = refresh_stage(cursor, "raw_schema", "stage_schema", ("2026-05", "2026-05"), stage_scope="csd")

    # Then: only the CSD table is truncated and refreshed.
    assert result == {"csd_channel_dynamics_stage": 1}
    executed_sql = "\n".join(cursor.statements)
    assert "csd_channel_dynamics_stage" in executed_sql
    assert "km_keyword_event_stage" not in executed_sql


def test_refresh_stage_all_scope_preserves_legacy_both_table_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: both refresh branches have rows to report.
    cursor = _RecordingCursor()
    monkeypatch.setattr(
        "pipeline.scripts.etl.brand_activity.raw_stage_refresh._canonical_csd_stage_rows",
        lambda *_args: [_csd_row()],
    )
    monkeypatch.setattr(
        "pipeline.scripts.etl.brand_activity.raw_stage_refresh._copy_keyword_stage",
        lambda *_args: 7,
    )

    # When: the default all-source refresh runs.
    result = refresh_stage(cursor, "raw_schema", "stage_schema", ("2026-05", "2026-05"))

    # Then: legacy behavior still refreshes both stage tables.
    assert result == {"csd_channel_dynamics_stage": 1, "km_keyword_event_stage": 7}
    executed_sql = "\n".join(cursor.statements)
    assert "csd_channel_dynamics_stage" in executed_sql
    assert "km_keyword_event_stage" in executed_sql


def test_load_sources_csd_scope_skips_keyword_insert_and_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: a fake DB adapter and a Keyword insert path that would fail if reached.
    connection = _FakeConnection()
    calls: list[str] = []
    monkeypatch.setitem(
        sys.modules,
        "pymysql",
        SimpleNamespace(MySQLError=RuntimeError, connect=lambda **_kwargs: connection),
    )
    monkeypatch.setattr(raw_db, "_execute_ddl", lambda *_args: None)
    monkeypatch.setattr(raw_db, "_raw_counts", lambda _cursor, _schema, datasets=("csd", "keyword"): {dataset: 0 for dataset in datasets})
    monkeypatch.setattr(raw_db, "_stage_counts", lambda _cursor, _schema, datasets=("csd", "keyword"): {"csd_channel_dynamics_stage": 4})
    monkeypatch.setattr(raw_db, "_insert_csd", lambda *_args: calls.append("csd") or 1)
    monkeypatch.setattr(raw_db, "_insert_keyword", lambda *_args: pytest.fail("keyword raw insert should not run in CSD scope"))
    monkeypatch.setattr(raw_db, "refresh_stage", lambda *_args, **kwargs: {"scope": kwargs["stage_scope"]})

    rows = SourceRows(csd=[_csd_source_row()], keyword=[_keyword_event(source_row_no=2)])
    config = raw_db.DbConfig(
        host="localhost",
        port=3306,
        user="root",
        password="",
        raw_schema="jw_brand_activity_raw_stage",
        stage_schema="jw_brand_activity_stage",
    )

    # When: only the CSD scope is loaded.
    stats = raw_db.load_sources(config, rows, ("2026-05", "2026-05"), stage_scope="csd")

    # Then: CSD inserts and commit happen, but Keyword insert never starts.
    assert calls == ["csd"]
    assert stats.inserted == {"raw_csd_channel_dynamics": 1}
    assert stats.stage_before == {"csd_channel_dynamics_stage": 4}
    assert stats.stage_rows == {"scope": "csd"}
    assert connection.committed is True


def test_load_sources_derives_window_from_the_raw_table_when_unspecified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection()
    observed: dict[str, tuple[str, str]] = {}
    monkeypatch.setitem(
        sys.modules,
        "pymysql",
        SimpleNamespace(MySQLError=RuntimeError, connect=lambda **_kwargs: connection),
    )
    monkeypatch.setattr(raw_db, "_execute_ddl", lambda *_args: None)
    monkeypatch.setattr(raw_db, "_raw_counts", lambda *_args, **_kwargs: {"raw_keyword_events": 0})
    monkeypatch.setattr(raw_db, "_stage_counts", lambda *_args, **_kwargs: {"km_keyword_event_stage": 0})
    monkeypatch.setattr(raw_db, "_insert_keyword", lambda *_args: 1)
    monkeypatch.setattr(raw_db, "_max_raw_period", lambda *_args, **_kwargs: "2026-05")

    def fake_refresh(_cursor, _raw, _stage, window, **_kwargs):
        observed["window"] = window
        return {"km_keyword_event_stage": 1}

    monkeypatch.setattr(raw_db, "refresh_stage", fake_refresh)
    config = raw_db.DbConfig(
        host="localhost",
        port=3306,
        user="root",
        password="",
        raw_schema="jw_brand_activity_raw_stage",
        stage_schema="jw_brand_activity_stage",
    )

    raw_db.load_sources(
        config,
        SourceRows(csd=[], keyword=[_keyword_event(source_row_no=2)]),
        None,
        stage_scope="keyword",
    )

    assert observed["window"] == ("2023-06", "2026-05")


def test_isolated_ingest_stage_schemas_are_accepted() -> None:
    from pipeline.scripts.etl.brand_activity.ingest_csd import stage_ddl as csd_stage_ddl
    from pipeline.scripts.etl.brand_activity.ingest_keyword_stage import stage_ddl as keyword_stage_ddl

    assert "`jw_ingest_contract_stage`" in csd_stage_ddl("jw_ingest_contract_stage")
    assert "`jw_ingest_contract_stage`" in keyword_stage_ddl("jw_ingest_contract_stage")


def test_target_market_coverage_splits_old_and_new_keyword_contributions() -> None:
    # Given: LIVALO/LIVALOZET rows from old and new Keyword sources.
    product_markets = {
        "LIVALO": {"LIVALO Market"},
        "LIVALOZET": {"LIVALOZET Market"},
    }
    keyword_events = [
        _keyword_event(source_row_no=2, source_file="Keywords for JW Jan. 25.xlsx", product_name="LIVALO"),
        _keyword_event(source_row_no=3, source_file="Keywords for JW Dec. 25.xlsx", product_name="LIVALOZET"),
    ]
    collection_map = {
        "Keywords for JW Jan. 25.xlsx": "old",
        "Keywords for JW Dec. 25.xlsx": "new",
    }

    # When: 11-market coverage is calculated.
    coverage = target_market_coverage(
        CoverageSources(
            product_markets=product_markets,
            keyword_events=keyword_events,
            window=("2025-01", "2025-12"),
            source_collection=collection_map,
        )
    )
    livalo = next(row for row in coverage if row["market"] == "LIVALO+LIVALOZET Market")

    # Then: total coverage and old/new contributions are both visible.
    assert livalo["keyword_rows"] == 2
    assert livalo["keyword_rows_old"] == 1
    assert livalo["keyword_rows_new"] == 1
    assert "meeting_rows" not in livalo


def test_raw_schema_excludes_meeting_tables() -> None:
    # Given / When: the raw staging DDL is generated from the committed loader.
    ddl = RAW_DDL.lower()

    # Then: meeting raw tables are no longer created by reproducible ETL.
    assert "raw_keyword_events" in ddl
    assert "raw_meeting_events" not in ddl
    assert "meeting_topic" not in ddl


def test_raw_loader_accepts_explicit_brand_activity_scratch_schemas(monkeypatch) -> None:
    # Given: a disposable repro schema is passed for raw and stage loading.
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "load_raw_staging.py",
            "--raw-schema",
            "jw_brand_activity_repro",
            "--stage-schema",
            "jw_brand_activity_repro",
        ],
    )

    # When: the CLI args and schema names are parsed.
    args = parse_args()
    raw_schema = quote_stage_name(args.raw_schema, "jw_brand_activity_raw_stage")
    stage_schema = quote_stage_name(args.stage_schema, "jw_brand_activity_stage")

    # Then: explicit scratch schemas under the brand-activity namespace are allowed.
    assert raw_schema == "jw_brand_activity_repro"
    assert stage_schema == "jw_brand_activity_repro"


def test_raw_loader_rejects_non_brand_activity_scratch_schema() -> None:
    # Given / When / Then: unrelated schemas remain blocked for write safety.
    with pytest.raises(ValueError, match="brand-activity scratch"):
        quote_stage_name("prod_mart", "jw_brand_activity_stage")


def _keyword_event(
    source_row_no: int,
    source_file: str = "Keywords for JW July. 25.xlsx",
    product_name: str = "ATOZET",
) -> KeywordEvent:
    """Build a minimal KeywordEvent fixture with meaningful duplicate text."""
    return KeywordEvent(
        period_ym="2025-07",
        visit_location="Clinic",
        specialty="Cardiology",
        representing_company="JW",
        product_name=product_name,
        therapeutic_class="C10C0",
        keyword_text="same message",
        interest="VERY USEFUL",
        prescription_frequency="frequently",
        prescription_evolution="increase",
        abstract_lit="NO",
        patient_lit="NO",
        promotional_lit="YES",
        samples_left="NO",
        other_materials_left="NO",
        what_other_materials="",
        other_comments="",
        source_file=source_file,
        source_sheet="Keywords",
        source_row_no=source_row_no,
        source_file_sha256="a" * 64,
    )


def _csd_row() -> CsdRow:
    return CsdRow(
        source_file="ChannelDynamics_JW Pharma Regional Report_May26.xlsx",
        source_sheet="LIVALO Market",
        source_row_no=8,
        period_ym="2026-05",
        market="LIVALO Market",
        jw_channel="TOTAL",
        master_product="LIVALO",
        representing_company="JW",
        product_details=100,
    )


def _csd_source_row(
    period_ym: str = "2026-05",
    product_details: int = 100,
    selected_for_stage: bool = True,
) -> CsdSourceRow:
    row = _csd_row()
    return CsdSourceRow(
        source_file=row.source_file,
        source_file_sha256="b" * 64,
        source_sheet=row.source_sheet,
        source_row_no=row.source_row_no,
        source_period_ym="2026-05",
        period_ym=period_ym,
        market=row.market,
        jw_channel=row.jw_channel,
        region="TOTAL" if selected_for_stage else "서울",
        master_product=row.master_product,
        manufacturer="JW",
        representing_company=row.representing_company,
        product_details=product_details,
        selected_for_stage=selected_for_stage,
    )


class _RecordingCursor:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def __enter__(self) -> "_RecordingCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, _params: object = None) -> None:
        self.statements.append(sql)

    def executemany(self, sql: str, _rows: object) -> None:
        self.statements.append(sql)


class _FakeConnection:
    def __init__(self) -> None:
        self.committed = False

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor()

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.committed = False

    def close(self) -> None:
        pass


def _touch_source(path: Path) -> None:
    """Create a minimal placeholder workbook path for discovery tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"placeholder")
