"""Source-set discovery and coverage helpers for combined Keyword/Meeting loads."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from pipeline.scripts.etl.brand_activity.km_core import JsonValue, KeywordEvent, MeetingEvent, source_period_from_name
from pipeline.scripts.etl.brand_activity.raw_extract import SourceRoots, discover_source_files


TARGET_MARKETS: dict[str, tuple[str, ...]] = {
    "LIVALO+LIVALOZET Market": ("LIVALO Market", "LIVALOZET Market"),
    "GUARDLET Market": ("GUARDLET Market",),
    "PPI Market": ("PPI Market",),
    "GANAKHAN Market": ("GANAKHAN Market",),
    "TURUPAS Market": ("TURUPAS Market",),
    "FERINJECT Market": ("FERINJECT Market",),
    "FOSRENOL Market": ("FOSRENOL Market",),
    "ENCOVER Market": ("ENCOVER Market",),
    "WINUF Market": ("WINUF Market",),
    "PLAJU OP Market": ("PLAJU OP Market",),
    "LIVALO V Market": ("LIVALO V Market",),
}


@dataclass(frozen=True, slots=True)
class KeywordMeetingRoots:
    """Resolved legacy Keyword/Meeting folders that do not carry CSD scope."""

    keyword: Path
    meeting: Path


@dataclass(frozen=True, slots=True)
class CoverageSources:
    """Inputs required to count market coverage and source contribution."""

    product_markets: Mapping[str, set[str]]
    keyword_events: Sequence[KeywordEvent]
    meeting_events: Sequence[MeetingEvent]
    window: tuple[str, str]
    source_collection: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class EventCoverageFilter:
    """One market/window filter used to count event rows by collection."""

    product_markets: Mapping[str, set[str]]
    market_set: set[str]
    start: str
    end: str
    source_collection: Mapping[str, str]


def discover_combined_source_files(roots: SourceRoots, legacy_root: Path) -> dict[str, list[Path]]:
    """Return CSD new-only files plus new+old Keyword/Meeting files."""
    files = discover_source_files(roots)
    legacy = resolve_legacy_keyword_meeting_roots(legacy_root)
    combined = {
        "csd": files["csd"],
        "keyword": _sorted_km_workbooks((*files["keyword"], *_source_workbooks(legacy.keyword, "Keywords for JW*.xlsx"))),
        "meeting": _sorted_km_workbooks((*files["meeting"], *_source_workbooks(legacy.meeting, "Meetings for JW*.xlsx"))),
    }
    _ensure_unique_event_filenames(combined)
    return combined


def resolve_legacy_keyword_meeting_roots(root: Path) -> KeywordMeetingRoots:
    """Find old monthly Keyword/Meeting folders under the CSD2 source root."""
    if not root.is_dir():
        raise FileNotFoundError(f"legacy Keyword/Meeting source root not found: {root}")
    directories = [path for path in root.iterdir() if path.is_dir()]
    keyword_matches = [path for path in directories if "Keyword" in path.name]
    meeting = root / "Meetings"
    if len(keyword_matches) != 1:
        raise FileNotFoundError(f"expected one legacy Keyword folder under {root}, found {len(keyword_matches)}")
    if not meeting.is_dir():
        raise FileNotFoundError(f"legacy Meeting source folder not found: {meeting}")
    return KeywordMeetingRoots(keyword=keyword_matches[0], meeting=meeting)


def source_collection_by_file(files: Mapping[str, Sequence[Path]]) -> dict[str, str]:
    """Map workbook names to `old` or `new` source collection labels."""
    result: dict[str, str] = {}
    for dataset in ("csd", "keyword", "meeting"):
        for path in files.get(dataset, ()):
            result[path.name] = _collection_label(path)
    return result


def source_collection_counts(files: Mapping[str, Sequence[Path]]) -> dict[str, dict[str, int]]:
    """Count source workbooks by dataset and old/new collection."""
    return {dataset: dict(sorted(Counter(_collection_label(path) for path in paths).items())) for dataset, paths in files.items()}


def target_market_coverage(sources: CoverageSources) -> list[dict[str, JsonValue]]:
    """Count Keyword/Meeting rows that can be joined to the 11 CSD markets."""
    start, end = sources.window
    coverage: list[dict[str, JsonValue]] = []
    for label, markets in TARGET_MARKETS.items():
        market_set = set(markets)
        event_filter = EventCoverageFilter(
            product_markets=sources.product_markets,
            market_set=market_set,
            start=start,
            end=end,
            source_collection=sources.source_collection,
        )
        keyword_counts = _event_collection_counts(sources.keyword_events, event_filter)
        meeting_counts = _event_collection_counts(sources.meeting_events, event_filter)
        coverage.append(
            {
                "market": label,
                "csd_markets": list(markets),
                "keyword_rows": sum(keyword_counts.values()),
                "keyword_rows_old": keyword_counts.get("old", 0),
                "keyword_rows_new": keyword_counts.get("new", 0),
                "keyword_rows_unknown": keyword_counts.get("unknown", 0),
                "meeting_rows": sum(meeting_counts.values()),
                "meeting_rows_old": meeting_counts.get("old", 0),
                "meeting_rows_new": meeting_counts.get("new", 0),
                "meeting_rows_unknown": meeting_counts.get("unknown", 0),
                "has_keyword": sum(keyword_counts.values()) > 0,
                "has_meeting": sum(meeting_counts.values()) > 0,
            }
        )
    return coverage


def _source_workbooks(root: Path, pattern: str) -> tuple[Path, ...]:
    """Return source workbooks, excluding Excel lock files."""
    return tuple(path for path in root.glob(pattern) if not path.name.startswith("~$"))


def _sorted_km_workbooks(paths: Sequence[Path]) -> list[Path]:
    """Sort Keyword/Meeting workbooks by parsed source period and name."""
    return sorted(paths, key=lambda path: (source_period_from_name(path), path.name))


def _ensure_unique_event_filenames(files: Mapping[str, Sequence[Path]]) -> None:
    """Reject source sets whose event filenames would collide in raw dedup keys."""
    for dataset in ("keyword", "meeting"):
        names = [path.name for path in files.get(dataset, ())]
        duplicate_names = sorted(name for name, count in Counter(names).items() if count > 1)
        if duplicate_names:
            raise ValueError(f"{dataset} source filenames collide with raw dedup keys: {duplicate_names}")


def _collection_label(path: Path) -> str:
    """Classify known source roots as old CSD2 or new CSD."""
    if any(parent.name == "CSD2" for parent in path.parents):
        return "old"
    if any(parent.name == "CSD" for parent in path.parents):
        return "new"
    return "unknown"


def _event_collection_counts(
    events: Sequence[KeywordEvent] | Sequence[MeetingEvent],
    event_filter: EventCoverageFilter,
) -> Counter[str]:
    """Count joined event rows by old/new workbook collection."""
    counts: Counter[str] = Counter()
    for event in events:
        if event_filter.start <= event.period_ym <= event_filter.end and event_filter.product_markets.get(event.product_name, set()) & event_filter.market_set:
            counts[event_filter.source_collection.get(event.source_file, "unknown")] += 1
    return counts
