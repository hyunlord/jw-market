from __future__ import annotations

import json
import hashlib
from pathlib import Path
import zipfile

import pytest

from pipeline.scripts.ingest_hook.source_inventory import (
    FileObservation,
    ScanSnapshot,
    SourceInventoryError,
    SourceScanPolicy,
    compare_snapshots,
    classified_source_paths,
    enforce_scan_gates,
    evaluate_period_gates,
    mass_deletion_threshold,
    read_scan_snapshot,
    run_full_scan,
    scan_source,
    write_inventory_snapshot,
)
from pipeline.scripts.ingest_hook.workbook_contracts import WorkbookSummary
from pipeline.scripts.ingest_hook.workbook_source_validation import SourceValidationError
from pipeline.scripts.ingest_hook.workbook_contracts import summarize as summarize_workbook


def _write(root: Path, relative: str, content: bytes = b"xlsx") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _summary(periods: set[str]) -> WorkbookSummary:
    return WorkbookSummary(rows=10, periods=frozenset(periods), detail="fixture")


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_scan_reuses_unchanged_sha_without_reparsing(tmp_path: Path) -> None:
    source = _write(tmp_path, "source.xlsx", b"unchanged")
    previous = ScanSnapshot(
        "1",
        "ubist",
        "2026-06",
        "a" * 64,
        "previous-run",
        "2026-08-07T00:00:00+00:00",
        (
            FileObservation(
                "source.xlsx",
                _file_sha256(source),
                source.stat().st_size,
                "classified",
                category="ubist",
                rows=123,
                periods=("2026-06",),
                reason="previous exact scan",
                first_observed_at="2026-08-07T00:00:00+00:00",
                last_observed_at="2026-08-07T00:00:00+00:00",
            ),
        ),
    )

    current = scan_source(
        SourceScanPolicy("ubist", tmp_path, "month"),
        epoch="2026-06",
        manifest_sha="b" * 64,
        run_id="current-run",
        previous=previous,
        classify=lambda _path: pytest.fail("unchanged SHA must not be classified again"),
        summarize=lambda *_args: pytest.fail("unchanged SHA must not be parsed again"),
    )

    assert current.files[0].rows == 123
    assert current.files[0].periods == ("2026-06",)
    assert current.files[0].reason == "previous exact scan"


def test_full_scan_bootstrap_rebuilds_only_manifest_files(tmp_path: Path) -> None:
    old = _write(tmp_path, "old.xlsx", b"old")
    current = _write(tmp_path, "current.xlsx", b"current")
    rebuilt: list[tuple[Path, ...]] = []

    run_full_scan(
        SourceScanPolicy("ubist", tmp_path, "month"),
        epoch="2026-06",
        manifest_sha="c" * 64,
        run_id="bootstrap-run",
        output_root=tmp_path / "inventory",
        classify=lambda _path: "ubist",
        summarize=lambda _category, path, _epoch: _summary(
            {"2026-05" if path == old else "2026-06"}
        ),
        rebuild=lambda paths: rebuilt.append(paths) or {"files": len(paths)},
        bootstrap_files=(current,),
    )

    assert rebuilt == [(current.resolve(),)]


def test_full_scan_unchanged_sha_never_reaches_rebuild_parser(tmp_path: Path) -> None:
    source = _write(tmp_path, "source.xlsx", b"already-loaded")
    previous = ScanSnapshot(
        "1",
        "ubist",
        "2026-06",
        "a" * 64,
        "previous-run",
        "2026-08-07T00:00:00+00:00",
        (
            FileObservation(
                "source.xlsx",
                _file_sha256(source),
                source.stat().st_size,
                "classified",
                category="ubist",
                rows=100,
                periods=("2026-06",),
                first_observed_at="2026-08-07T00:00:00+00:00",
                last_observed_at="2026-08-07T00:00:00+00:00",
            ),
        ),
    )
    rebuilt: list[tuple[Path, ...]] = []

    run_full_scan(
        SourceScanPolicy("ubist", tmp_path, "month", rebuild_periods=61),
        epoch="2026-06",
        manifest_sha="e" * 64,
        run_id="incremental-run",
        output_root=tmp_path / "inventory",
        previous=previous,
        classify=lambda _path: pytest.fail("unchanged SHA must not be classified"),
        summarize=lambda *_args: pytest.fail("unchanged SHA must not be parsed"),
        rebuild=lambda paths: rebuilt.append(paths) or {"files": len(paths)},
    )

    assert rebuilt == [()]


def test_full_scan_filters_outside_rebuild_window_before_loader(tmp_path: Path) -> None:
    old = _write(tmp_path, "old.xlsx", b"old")
    current = _write(tmp_path, "current.xlsx", b"current")
    rebuilt: list[tuple[Path, ...]] = []

    run_full_scan(
        SourceScanPolicy("ubist", tmp_path, "month", rebuild_periods=1),
        epoch="2026-06",
        manifest_sha="d" * 64,
        run_id="window-run",
        output_root=tmp_path / "inventory",
        classify=lambda _path: "ubist",
        summarize=lambda _category, path, _epoch: _summary(
            {"2025-01" if path == old else "2026-06"}
        ),
        rebuild=lambda paths: rebuilt.append(paths) or {"files": len(paths)},
        permissive=True,
    )

    assert rebuilt == [(current.resolve(),)]


def test_scan_classifies_by_content_and_excludes_demo_root(tmp_path: Path) -> None:
    _write(tmp_path, "production/misleading-ubist-name.xlsx", b"nsa")
    _write(tmp_path, "demo_rehearsal_1/demo.xlsx", b"nsa")
    classified: list[str] = []

    def classify(path: Path) -> str:
        classified.append(path.name)
        return "iqvia_nsa"

    snapshot = scan_source(
        SourceScanPolicy(
            category="iqvia_nsa",
            root=tmp_path,
            period_unit="quarter",
            excluded_relative_roots=("demo_rehearsal_1",),
        ),
        epoch="2026-Q2",
        manifest_sha="a" * 64,
        run_id="run-1",
        classify=classify,
        summarize=lambda _category, _path, _epoch: _summary({"2026-Q2"}),
    )

    assert classified == ["misleading-ubist-name.xlsx"]
    assert snapshot.classified_count == 1
    assert snapshot.excluded_count == 1
    classified_file = next(item for item in snapshot.files if item.state == "classified")
    assert classified_file.category == "iqvia_nsa"
    assert classified_file.relative_path == "production/misleading-ubist-name.xlsx"


def test_appledouble_is_rejected_and_never_becomes_source_input(tmp_path: Path) -> None:
    _write(tmp_path, "._source.xlsx", b"appledouble")

    snapshot = scan_source(
        SourceScanPolicy("iqvia_nsa", tmp_path, "quarter"),
        epoch="2026-Q2",
        manifest_sha="b" * 64,
        run_id="run-2",
        classify=lambda _path: pytest.fail("AppleDouble must not reach classifier"),
        summarize=lambda *_args: pytest.fail("AppleDouble must not be summarized"),
    )

    assert snapshot.classified_count == 0
    assert snapshot.excluded_count == 1
    assert snapshot.rejected_count == 0
    assert snapshot.files[0].state == "excluded"
    assert snapshot.files[0].reason == "AppleDouble metadata is not an ingest workbook"


def test_rejected_operating_file_fails_closed(tmp_path: Path) -> None:
    _write(tmp_path, "production/broken.xlsx", b"broken")

    def reject(_path: Path) -> str:
        raise SourceValidationError("invalid XLSX structure: BadZipFile")

    current = scan_source(
        SourceScanPolicy("iqvia_nsa", tmp_path, "quarter"),
        epoch="2026-Q2",
        manifest_sha="b" * 64,
        run_id="run-broken",
        classify=reject,
        summarize=lambda *_args: pytest.fail("rejected file must not be summarized"),
    )
    previous = _snapshot("previous", (_observed("old.xlsx", "1" * 64, {"2026-Q1"}),))

    with pytest.raises(SourceInventoryError, match="rejected operating workbooks"):
        enforce_scan_gates(previous, current, compare_snapshots(previous, current))


def test_scan_rejects_symlink_that_escapes_approved_root(tmp_path: Path) -> None:
    source_root = tmp_path / "approved"
    source_root.mkdir()
    outside = _write(tmp_path, "outside.xlsx", b"outside")
    (source_root / "linked.xlsx").symlink_to(outside)

    with pytest.raises(SourceInventoryError, match="escapes approved root"):
        scan_source(
            SourceScanPolicy("iqvia_nsa", source_root, "quarter"),
            epoch="2026-Q2",
            manifest_sha="b" * 64,
            run_id="run-symlink",
            classify=lambda _path: pytest.fail("escaped file must not be classified"),
            summarize=lambda *_args: pytest.fail("escaped file must not be summarized"),
        )


def test_first_snapshot_still_enforces_pg4_continuity() -> None:
    current = _snapshot(
        "first",
        (_observed("source.xlsx", "1" * 64, {"2026-Q1", "2026-Q3"}),),
    )

    with pytest.raises(SourceInventoryError, match="PG-4=2026-Q2"):
        enforce_scan_gates(None, current, None, period_unit="quarter")


def test_ubist_summary_uses_canonical_loader_period_counts(monkeypatch, tmp_path: Path) -> None:
    workbook = _write(tmp_path, "arbitrary.xlsx")
    observed: list[Path] = []

    def count_rows(path: Path) -> dict[str, int]:
        observed.append(path)
        return {"2026-05": 10, "2026-06": 12}

    monkeypatch.setattr(
        "pipeline.etl.io.ubist_loader.count_source_rows_by_period", count_rows
    )

    summary = summarize_workbook("ubist", workbook, "2026-06")

    assert observed == [workbook]
    assert summary.rows == 22
    assert summary.periods == frozenset({"2026-05", "2026-06"})
    assert summary.detail == "ubist_loader.count_source_rows_by_period"


def test_pg4_fails_on_internal_month_gap() -> None:
    result = evaluate_period_gates(
        period_unit="month",
        current_periods={"2026-03", "2026-05"},
        previous_periods={"2026-03", "2026-04", "2026-05"},
        removed_files=(),
        surviving_file_periods={"current.xlsx": {"2026-03", "2026-05"}},
    )

    assert result.pg4.status == "fail"
    assert result.pg4.periods == ("2026-04",)


def test_pg5_allows_only_loss_fully_explained_by_removed_files() -> None:
    removed = (
        FileObservation.removed(
            relative_path="old.xlsx",
            sha256="c" * 64,
            size=100,
            periods={"2026-04"},
        ),
    )
    allowed = evaluate_period_gates(
        period_unit="month",
        current_periods={"2026-03", "2026-05"},
        previous_periods={"2026-03", "2026-04", "2026-05"},
        removed_files=removed,
        surviving_file_periods={"current.xlsx": {"2026-03", "2026-05"}},
    )
    blocked = evaluate_period_gates(
        period_unit="month",
        current_periods={"2026-03", "2026-05"},
        previous_periods={"2026-03", "2026-04", "2026-05"},
        removed_files=(),
        surviving_file_periods={"current.xlsx": {"2026-03", "2026-05"}},
    )

    assert allowed.pg5.status == "pass"
    assert blocked.pg5.status == "fail"
    assert blocked.pg5.periods == ("2026-04",)


def test_pg5_rejects_loss_still_covered_by_surviving_file() -> None:
    removed = (
        FileObservation.removed(
            relative_path="old.xlsx",
            sha256="d" * 64,
            size=100,
            periods={"2026-04"},
        ),
    )
    result = evaluate_period_gates(
        period_unit="month",
        current_periods={"2026-03", "2026-05"},
        previous_periods={"2026-03", "2026-04", "2026-05"},
        removed_files=removed,
        surviving_file_periods={"survivor.xlsx": {"2026-04", "2026-05"}},
    )

    assert result.pg5.status == "fail"
    assert result.pg5.periods == ("2026-04",)


def test_newest_period_loss_stays_an_independent_hard_stop() -> None:
    previous = _snapshot(
        "previous",
        (
            _observed("older.xlsx", "1" * 64, {"2026-Q1"}),
            _observed("removed.xlsx", "2" * 64, {"2026-Q2"}),
        ),
    )
    current = _snapshot(
        "current",
        (_observed("older.xlsx", "1" * 64, {"2026-Q1"}),),
    )

    with pytest.raises(SourceInventoryError, match="newest previous content period"):
        enforce_scan_gates(
            previous,
            current,
            compare_snapshots(previous, current),
            period_unit="quarter",
        )


@pytest.mark.parametrize(
    ("previous_count", "expected"),
    [(1, 2), (5, 2), (10, 2), (11, 3), (25, 5), (100, 5)],
)
def test_mass_deletion_threshold_contract(previous_count: int, expected: int) -> None:
    assert mass_deletion_threshold(previous_count) == expected


def test_inventory_snapshot_is_immutable_and_atomically_published(tmp_path: Path) -> None:
    source = _write(tmp_path, "source.xlsx")
    snapshot = scan_source(
        SourceScanPolicy("iqvia_nsa", tmp_path, "quarter"),
        epoch="2026-Q2",
        manifest_sha="e" * 64,
        run_id="run-3",
        classify=lambda path: "iqvia_nsa" if path == source else "wrong",
        summarize=lambda *_args: _summary({"2026-Q2"}),
    )
    output_root = tmp_path / "inventory"

    path = write_inventory_snapshot(snapshot, output_root)

    assert path == output_root / "iqvia_nsa/2026-Q2" / ("e" * 64) / "run-3.json"
    assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == "run-3"
    with pytest.raises(SourceInventoryError, match="already exists"):
        write_inventory_snapshot(snapshot, output_root)

    restored = read_scan_snapshot(path)
    assert restored == snapshot


def test_only_classified_files_become_cache_rebuild_inputs(tmp_path: Path) -> None:
    classified = _write(tmp_path, "production/source.xlsx")
    _write(tmp_path, "demo/demo.xlsx")
    _write(tmp_path, "._source.xlsx")
    snapshot = ScanSnapshot(
        "1",
        "iqvia_nsa",
        "2026-Q2",
        "e" * 64,
        "run-cache",
        "2026-08-07T00:00:00Z",
        (
            FileObservation(
                "production/source.xlsx",
                "1" * 64,
                1,
                "classified",
                "iqvia_nsa",
                10,
                ("2026-Q2",),
            ),
            FileObservation("demo/demo.xlsx", "2" * 64, 1, "excluded"),
            FileObservation("._source.xlsx", "3" * 64, 1, "rejected"),
        ),
    )

    assert classified_source_paths(snapshot, tmp_path) == (classified.resolve(),)


def test_cache_rebuild_input_rejects_snapshot_path_escape(tmp_path: Path) -> None:
    snapshot = ScanSnapshot(
        "1",
        "iqvia_nsa",
        "2026-Q2",
        "e" * 64,
        "run-cache",
        "2026-08-07T00:00:00Z",
        (FileObservation("../outside.xlsx", "1" * 64, 1, "classified"),),
    )

    with pytest.raises(SourceInventoryError, match="escapes source root"):
        classified_source_paths(snapshot, tmp_path)


def _snapshot(run_id: str, files: tuple[FileObservation, ...]) -> ScanSnapshot:
    return ScanSnapshot(
        "1",
        "iqvia_nsa",
        "2026-Q2",
        "f" * 64,
        run_id,
        "2026-08-07T00:00:00Z",
        files,
    )


def _observed(path: str, sha: str, periods: set[str]) -> FileObservation:
    return FileObservation(path, sha, 100, "classified", "iqvia_nsa", 10, tuple(sorted(periods)))


def test_snapshot_diff_preserves_removed_file_evidence() -> None:
    previous = _snapshot(
        "previous",
        (
            _observed("old.xlsx", "1" * 64, {"2026-Q1"}),
            _observed("same.xlsx", "2" * 64, {"2026-Q2"}),
        ),
    )
    current = _snapshot("current", (_observed("same.xlsx", "2" * 64, {"2026-Q2"}),))

    diff = compare_snapshots(previous, current)

    assert diff.removed_count == 1
    assert diff.removed_files[0].relative_path == "old.xlsx"
    assert diff.removed_files[0].sha256 == "1" * 64
    assert diff.removed_files[0].periods == ("2026-Q1",)


def test_mass_deletion_gate_stops_at_approved_formula() -> None:
    previous = _snapshot(
        "previous",
        tuple(
            _observed(f"{index}.xlsx", str(index) * 64, {f"202{index}-Q1"})
            for index in range(1, 6)
        ),
    )
    current = _snapshot("current", previous.files[2:])
    diff = compare_snapshots(previous, current)

    with pytest.raises(SourceInventoryError, match="mass deletion"):
        enforce_scan_gates(previous, current, diff)


def test_pg6_pg7_pass_without_drift_and_warn_without_blocking_on_drift() -> None:
    previous = _snapshot("previous", (_observed("same.xlsx", "2" * 64, {"2026-Q1", "2026-Q2"}),))
    current = _snapshot("current", previous.files)
    unchanged = enforce_scan_gates(previous, current, compare_snapshots(previous, current))
    advanced = _snapshot(
        "advanced",
        (FileObservation(
            "same.xlsx",
            "3" * 64,
            100,
            "classified",
            "iqvia_nsa",
            20,
            ("2026-Q1", "2026-Q2", "2026-Q3"),
        ),),
    )
    drift = enforce_scan_gates(previous, advanced, compare_snapshots(previous, advanced))

    assert unchanged.pg4.status == "pass"
    assert unchanged.pg5.status == "pass"
    assert unchanged.pg6.status == "pass"
    assert unchanged.pg7.status == "pass"
    assert drift.pg6.status == "warning"
    assert "10 to 20" in drift.pg6.reason
    assert drift.pg7.status == "warning"
    assert drift.pg7.periods == ("2026-Q3",)


def test_full_scan_rebuilds_only_after_hard_gates_and_publishes_removed_evidence(
    tmp_path: Path,
) -> None:
    source = _write(tmp_path, "current.xlsx")
    previous = ScanSnapshot(
        "1",
        "iqvia_nsa",
        "2026-Q2",
        "e" * 64,
        "previous-run",
        "2026-08-06T00:00:00Z",
        (
            _observed("old.xlsx", "1" * 64, {"2026-Q1"}),
            _observed("current.xlsx", "2" * 64, {"2026-Q2"}),
        ),
    )
    rebuilt: list[tuple[Path, ...]] = []

    outcome = run_full_scan(
        SourceScanPolicy("iqvia_nsa", tmp_path, "quarter"),
        epoch="2026-Q2",
        manifest_sha="f" * 64,
        run_id="current-run",
        output_root=tmp_path / "inventory",
        previous=previous,
        classify=lambda path: "iqvia_nsa" if path == source else "wrong",
        summarize=lambda *_args: _summary({"2026-Q2"}),
        rebuild=lambda paths: rebuilt.append(paths) or {"artifacts": 1},
    )

    assert rebuilt == [(source.resolve(),)]
    assert outcome.gates.pg4.status == "pass"
    assert outcome.gates.pg5.status == "pass"
    assert outcome.rebuild_result == {"artifacts": 1}
    restored = read_scan_snapshot(outcome.snapshot_path)
    removed = [item for item in restored.files if item.state == "removed"]
    assert [(item.relative_path, item.sha256, item.periods) for item in removed] == [
        ("old.xlsx", "1" * 64, ("2026-Q1",))
    ]


def test_full_scan_does_not_rebuild_or_publish_when_pg4_fails(tmp_path: Path) -> None:
    source = _write(tmp_path, "source.xlsx")
    rebuilt = False

    def rebuild(_paths: tuple[Path, ...]) -> dict[str, int]:
        nonlocal rebuilt
        rebuilt = True
        return {}

    with pytest.raises(SourceInventoryError, match="PG-4"):
        run_full_scan(
            SourceScanPolicy("iqvia_csd_channel", tmp_path, "month"),
            epoch="2026-03",
            manifest_sha="f" * 64,
            run_id="blocked-run",
            output_root=tmp_path / "inventory",
            classify=lambda path: "iqvia_csd_channel" if path == source else "wrong",
            summarize=lambda *_args: _summary({"2026-01", "2026-03"}),
            rebuild=rebuild,
        )

    assert rebuilt is False
    assert not (tmp_path / "inventory").exists()


def test_full_scan_does_not_publish_snapshot_when_rebuild_fails(tmp_path: Path) -> None:
    source = _write(tmp_path, "source.xlsx")

    def rebuild(_paths: tuple[Path, ...]) -> dict[str, int]:
        raise RuntimeError("cache rebuild failed")

    with pytest.raises(RuntimeError, match="cache rebuild failed"):
        run_full_scan(
            SourceScanPolicy("iqvia_nsa", tmp_path, "quarter"),
            epoch="2026-Q2",
            manifest_sha="f" * 64,
            run_id="failed-rebuild",
            output_root=tmp_path / "inventory",
            classify=lambda path: "iqvia_nsa" if path == source else "wrong",
            summarize=lambda *_args: _summary({"2026-Q1", "2026-Q2"}),
            rebuild=rebuild,
        )

    assert not (tmp_path / "inventory").exists()


def test_full_scan_expands_nested_xlsx_from_zip_for_rebuild(tmp_path: Path) -> None:
    archive = tmp_path / "submission.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("nested/source.xlsx", b"real workbook bytes")
        handle.writestr("nested/ignore.txt", b"not an input")
    rebuilt: list[tuple[str, bytes]] = []

    outcome = run_full_scan(
        SourceScanPolicy("iqvia_nsa", tmp_path, "quarter"),
        epoch="2026-Q2",
        manifest_sha="a" * 64,
        run_id="zip-run",
        output_root=tmp_path / "inventory",
        classify=lambda _path: "iqvia_nsa",
        summarize=lambda *_args: _summary({"2026-Q1", "2026-Q2"}),
        rebuild=lambda paths: rebuilt.extend(
            (path.name, path.read_bytes()) for path in paths
        )
        or {"files": len(paths)},
    )

    assert rebuilt == [("source.xlsx", b"real workbook bytes")]
    assert outcome.snapshot.classified_count == 1
    assert outcome.snapshot.files[0].relative_path == "submission.zip!/nested/source.xlsx"


def test_commissioning_scan_records_gate_failures_but_rebuilds_classified_inputs(
    tmp_path: Path,
) -> None:
    good = _write(tmp_path, "good.xlsx", b"good")
    _write(tmp_path, "broken.xlsx", b"broken")
    rebuilt: list[tuple[Path, ...]] = []

    def classify(path: Path) -> str:
        if path.name == "broken.xlsx":
            raise SourceValidationError("broken fixture")
        return "iqvia_csd_channel"

    outcome = run_full_scan(
        SourceScanPolicy("iqvia_csd_channel", tmp_path, "month"),
        epoch="2026-03",
        manifest_sha="b" * 64,
        run_id="commissioning-run",
        output_root=tmp_path / "inventory",
        classify=classify,
        summarize=lambda *_args: _summary({"2026-01", "2026-03"}),
        rebuild=lambda paths: rebuilt.append(paths) or {"files": len(paths)},
        permissive=True,
    )

    assert rebuilt == [(good.resolve(),)]
    assert outcome.gates.pg4.status == "fail"
    assert outcome.snapshot.rejected_count == 1
    assert any("rejected operating workbooks=1" in item for item in outcome.commissioning_warnings)
    assert any("PG-4" in item for item in outcome.commissioning_warnings)
