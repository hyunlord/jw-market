from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pipeline.scripts.analysis.brand_activity.recheck.inventory import (  # noqa: E402
    FileRecord,
    compare_manifests,
    infer_folder_kind,
    month_from_filename,
)
from pipeline.scripts.analysis.brand_activity.recheck.safety import require_stage_schema  # noqa: E402


def test_month_from_filename_accepts_observed_iqvia_variants() -> None:
    assert month_from_filename("CSD_ChannelDynamics_JW Pharma Regional Report_Oct.25.xlsx") == "2025-10"
    assert month_from_filename("ChannelDynamics_JW Pharma Regional Report_July25.xlsx") == "2025-07"
    assert month_from_filename("Keywords for JW Sep. 25.xlsx") == "2025-09"
    assert month_from_filename("Meetings for JW Jan. 25.xlsx") == "2025-01"
    assert month_from_filename("unknown_source.xlsx") is None


def test_infer_folder_kind_uses_path_and_filename_without_raw_content() -> None:
    assert infer_folder_kind(Path("data/IQVIA/CSD/Meetings/Meetings for JW Oct. 25.xlsx")) == "meeting"
    assert infer_folder_kind(Path("data/IQVIA/CSD/Keyword (x)/Keywords for JW Oct. 25.xlsx")) == "keyword"
    assert infer_folder_kind(Path("data/IQVIA/CSD/ChannelDynamics (x)/ChannelDynamics_JW Pharma Regional Report_Apr.25.xlsx")) == "csd"


def test_compare_manifests_classifies_new_changed_deleted_and_unchanged() -> None:
    previous = [
        FileRecord("csd", "old.xlsx", Path("old.xlsx"), 100, "aaa", "2025-01", ("Sheet1",)),
        FileRecord("keyword", "changed.xlsx", Path("changed.xlsx"), 200, "bbb", "2025-02", ("Keywords",)),
        FileRecord("meeting", "gone.xlsx", Path("gone.xlsx"), 300, "ccc", "2025-03", ("Meetings",)),
    ]
    current = [
        FileRecord("csd", "old.xlsx", Path("old.xlsx"), 100, "aaa", "2025-01", ("Sheet1",)),
        FileRecord("keyword", "changed.xlsx", Path("changed.xlsx"), 201, "ddd", "2025-02", ("Keywords",)),
        FileRecord("meeting", "new.xlsx", Path("new.xlsx"), 400, "eee", "2025-04", ("Meetings",)),
    ]

    diff = compare_manifests(previous, current)

    assert [row.file_name for row in diff["unchanged"]] == ["old.xlsx"]
    assert [row.file_name for row in diff["changed"]] == ["changed.xlsx"]
    assert [row.file_name for row in diff["deleted"]] == ["gone.xlsx"]
    assert [row.file_name for row in diff["new"]] == ["new.xlsx"]


def test_require_stage_schema_rejects_near_misses() -> None:
    assert require_stage_schema("jw_brand_activity_stage") == "jw_brand_activity_stage"
    for schema in ("jw_brand_activity", "jw_brand_activity_stage_backup", "other_stage"):
        try:
            require_stage_schema(schema)
        except ValueError as error:
            assert "jw_brand_activity_stage" in str(error)
        else:
            raise AssertionError(f"schema should have been rejected: {schema}")
