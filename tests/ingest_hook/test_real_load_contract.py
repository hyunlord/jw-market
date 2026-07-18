"""Fail-closed contracts for the real UBIST ingest load boundary."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from pipeline.etl.run import parse_args
from pipeline.scripts.ingest_hook.category_map import resolve_category
from pipeline.scripts.ingest_hook.contract import load_manifest
from pipeline.scripts.ingest_hook.job_runner import _build_load_argv


def _write_workbook_manifest(root: Path, names: tuple[str, ...]) -> Path:
    files = []
    for name in names:
        path = root / "ubist" / "2026-05" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"workbook:{name}".encode())
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "period_start": "2026-05",
                "period_end": "2026-05",
            }
        )
    manifest = {
        "contract_version": "v2",
        "epoch": "2026-05",
        "category": "ubist",
        "complete": True,
        "submitted_at": "2026-07-18T09:00:00+09:00",
        "files": files,
    }
    manifest_path = root / "ubist" / "2026-05" / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def test_ubist_real_load_uses_only_every_g3_validated_workbook(tmp_path, monkeypatch):
    manifest_path = _write_workbook_manifest(
        tmp_path,
        ("clinic-internal.xlsx", "clinic-other.xlsx", "non-clinic.xlsx"),
    )
    manifest = load_manifest(manifest_path)
    target = tmp_path / "existing-full" / "ubist"
    monkeypatch.setenv("INGEST_UBIST_TARGET_DIR", str(target))

    argv = _build_load_argv(resolve_category("ubist"), manifest, tmp_path)

    assert argv[:7] == (
        sys.executable,
        "-m",
        "pipeline.etl.run",
        "--stage",
        "s1",
        "--source",
        "ubist",
    )
    assert argv.count("--ubist-file") == 3
    loaded = [Path(argv[index + 1]) for index, value in enumerate(argv) if value == "--ubist-file"]
    expected = [(tmp_path / entry.path).resolve() for entry in manifest.files]
    assert loaded == expected
    assert argv[argv.index("--target-dir") + 1] == str(target.resolve())
    assert "--incremental" in argv


def test_ubist_real_load_fails_closed_without_explicit_target(tmp_path, monkeypatch):
    manifest_path = _write_workbook_manifest(tmp_path, ("may.xlsx",))
    monkeypatch.delenv("INGEST_UBIST_TARGET_DIR", raising=False)

    with pytest.raises(RuntimeError, match="INGEST_UBIST_TARGET_DIR"):
        _build_load_argv(resolve_category("ubist"), load_manifest(manifest_path), tmp_path)


def test_etl_cli_accepts_repeated_ubist_file_arguments():
    args = parse_args(
        [
            "--stage",
            "s1",
            "--source",
            "ubist",
            "--incremental",
            "--target-dir",
            "/tmp/ubist",
            "--ubist-file",
            "/tmp/a.xlsx",
            "--ubist-file",
            "/tmp/b.xlsx",
        ]
    )

    assert args.ubist_files == ["/tmp/a.xlsx", "/tmp/b.xlsx"]
