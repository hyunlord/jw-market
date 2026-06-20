from __future__ import annotations

from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pipeline.scripts.etl.brand_activity.csd_core import CsdRow
from pipeline.scripts.etl.brand_activity.km_core import KeywordEvent, MeetingEvent
from pipeline.scripts.etl.brand_activity.load_raw_staging import (
    CoverageSources,
    parse_args,
    discover_combined_source_files,
    source_collection_by_file,
    target_market_coverage,
)
from pipeline.scripts.etl.brand_activity.raw_extract import resolve_source_roots
from pipeline.scripts.etl.brand_activity.raw_db import quote_stage_name
from pipeline.scripts.etl.brand_activity.raw_staging import (
    csd_dedup_key,
    keyword_dedup_key,
    meeting_dedup_key,
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


def test_keyword_and_meeting_keys_preserve_duplicate_event_rows() -> None:
    # Given: duplicate-looking source events appear on separate workbook rows.
    keyword_1 = _keyword_event(source_row_no=2)
    keyword_2 = _keyword_event(source_row_no=3)
    meeting_1 = _meeting_event(source_row_no=2)
    meeting_2 = _meeting_event(source_row_no=3)

    # When: raw staging keys are generated.
    keyword_keys = {keyword_dedup_key(keyword_1), keyword_dedup_key(keyword_2)}
    meeting_keys = {meeting_dedup_key(meeting_1), meeting_dedup_key(meeting_2)}

    # Then: row identity, not message text, controls Keyword/Meeting dedup.
    assert len(keyword_keys) == 2
    assert len(meeting_keys) == 2


def test_recent_month_window_is_inclusive_36_months() -> None:
    # Given: Apr.26 is the newest source period across staged data.
    max_period = "2026-04"

    # When: the analysis window is calculated.
    start_period, end_period = recent_month_window(max_period, months=36)

    # Then: the 36 inclusive months start at May.23.
    assert (start_period, end_period) == ("2023-05", "2026-04")


def test_combined_discovery_keeps_csd_new_only_while_adding_legacy_keyword_meeting(tmp_path: Path) -> None:
    # Given: new CSD folders and legacy CSD2 folders with monthly Keyword/Meeting files.
    new_root = tmp_path / "CSD"
    legacy_root = tmp_path / "CSD2"
    _touch_source(new_root / "ChannelDynamics (콜 수=영업 횟수)" / "ChannelDynamics_JW Pharma Regional Report_Dec.25.xlsx")
    _touch_source(new_root / "Keyword (고객=의사에게 전달한 메시지)" / "Keywords for JW Dec. 25.xlsx")
    _touch_source(new_root / "Meetings" / "Meetings for JW Dec. 25.xlsx")
    _touch_source(legacy_root / "Keyword (고객=의사에게 전달한 메시지)" / "Keywords for JW Jan. 25.xlsx")
    _touch_source(legacy_root / "Meetings" / "Meetings for JW Jan. 25.xlsx")

    # When: the combined P-0 file set is discovered.
    roots = resolve_source_roots(new_root)
    files = discover_combined_source_files(roots, legacy_root)
    collection_map = source_collection_by_file(files)

    # Then: CSD stays on the new source root, while Keyword/Meeting include old+new.
    assert [path.name for path in files["csd"]] == ["ChannelDynamics_JW Pharma Regional Report_Dec.25.xlsx"]
    assert [path.name for path in files["keyword"]] == ["Keywords for JW Jan. 25.xlsx", "Keywords for JW Dec. 25.xlsx"]
    assert [path.name for path in files["meeting"]] == ["Meetings for JW Jan. 25.xlsx", "Meetings for JW Dec. 25.xlsx"]
    assert collection_map["Keywords for JW Jan. 25.xlsx"] == "old"
    assert collection_map["Keywords for JW Dec. 25.xlsx"] == "new"


def test_target_market_coverage_splits_old_and_new_contributions() -> None:
    # Given: LIVALO/LIVALOZET rows from old and new Keyword/Meeting sources.
    product_markets = {
        "LIVALO": {"LIVALO Market"},
        "LIVALOZET": {"LIVALOZET Market"},
    }
    keyword_events = [
        _keyword_event(source_row_no=2, source_file="Keywords for JW Jan. 25.xlsx", product_name="LIVALO"),
        _keyword_event(source_row_no=3, source_file="Keywords for JW Dec. 25.xlsx", product_name="LIVALOZET"),
    ]
    meeting_events = [
        _meeting_event(source_row_no=2, source_file="Meetings for JW Jan. 25.xlsx", product_name="LIVALO"),
        _meeting_event(source_row_no=3, source_file="Meetings for JW Dec. 25.xlsx", product_name="LIVALOZET"),
    ]
    collection_map = {
        "Keywords for JW Jan. 25.xlsx": "old",
        "Keywords for JW Dec. 25.xlsx": "new",
        "Meetings for JW Jan. 25.xlsx": "old",
        "Meetings for JW Dec. 25.xlsx": "new",
    }

    # When: 11-market coverage is calculated.
    coverage = target_market_coverage(
        CoverageSources(
            product_markets=product_markets,
            keyword_events=keyword_events,
            meeting_events=meeting_events,
            window=("2025-01", "2025-12"),
            source_collection=collection_map,
        )
    )
    livalo = next(row for row in coverage if row["market"] == "LIVALO+LIVALOZET Market")

    # Then: total coverage and old/new contributions are both visible.
    assert livalo["keyword_rows"] == 2
    assert livalo["keyword_rows_old"] == 1
    assert livalo["keyword_rows_new"] == 1
    assert livalo["meeting_rows"] == 2
    assert livalo["meeting_rows_old"] == 1
    assert livalo["meeting_rows_new"] == 1


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


def _meeting_event(
    source_row_no: int,
    source_file: str = "Meetings for JW July. 25.xlsx",
    product_name: str = "ATOZET",
) -> MeetingEvent:
    """Build a minimal MeetingEvent fixture with meaningful duplicate text."""
    return MeetingEvent(
        meeting_date="2025-07-15",
        period_ym="2025-07",
        meeting_topic="same topic",
        meeting_format="Teleconference",
        pharma_sponsor="JW",
        non_pharma_sponsor="",
        no_at_meeting=10,
        product_name=product_name,
        therapeutic_class="C10C0",
        prescription_frequency="frequently",
        prescription_evolution="increase",
        interest="SOMEWHAT USEFUL",
        verbatim_message="same message",
        other_comments="",
        source_file=source_file,
        source_sheet="Meetings",
        source_row_no=source_row_no,
        source_file_sha256="b" * 64,
    )


def _touch_source(path: Path) -> None:
    """Create a minimal placeholder workbook path for discovery tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"placeholder")
