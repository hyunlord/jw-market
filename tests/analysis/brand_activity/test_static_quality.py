from __future__ import annotations

from pathlib import Path

from pipeline.scripts.analysis.brand_activity.auto_topic.static_quality import inspect_package


def test_inspect_package_reports_non_utf8_files_without_crashing(tmp_path: Path) -> None:
    """Given an AppleDouble metadata file, When inspected, Then it is reported and skipped."""
    package = tmp_path / "auto_topic"
    package.mkdir()
    (package / "valid.py").write_text(
        '"""Valid module."""\n\n'
        "def build_value():\n"
        '    """Build a value."""\n'
        "    return 1\n",
        encoding="utf-8",
    )
    (package / "._valid.py").write_bytes(b"\x00\x05metadata\xa3")

    result = inspect_package(package)

    assert result["non_utf8_files"] == ["._valid.py"]
    assert result["parsed_file_count"] == 1
