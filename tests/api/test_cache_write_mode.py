from __future__ import annotations

from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.config import load_config


def test_cache_write_mode_defaults_to_isolated(monkeypatch) -> None:
    monkeypatch.delenv("CACHE_WRITE_MODE", raising=False)

    assert load_config().cache_write_mode == "isolated"


def test_cache_write_mode_reads_disabled_value(monkeypatch) -> None:
    monkeypatch.setenv("CACHE_WRITE_MODE", "disabled")

    assert load_config().cache_write_mode == "disabled"
