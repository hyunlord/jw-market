from __future__ import annotations

import importlib
import json
from pathlib import Path
import subprocess
import sys

import pytest


from pipeline.scripts.api import manufacturer_resolver


@pytest.fixture(autouse=True)
def reset_cache() -> None:
    manufacturer_resolver._manufacturer_cache = None
    yield
    manufacturer_resolver._manufacturer_cache = None


def test_resolve_manufacturer_name_preserves_multi_product_selection_rule() -> None:
    mapping = {
        "LIVALO": frozenset({"JW SHINYAK"}),
        "LIVALOZET": frozenset({"JW PHARMACEUTICAL", "JW SHINYAK"}),
    }

    assert manufacturer_resolver.resolve_manufacturer_name(
        ("livalo", " LIVALOZET "), mapping
    ) == "JW SHINYAK, JW PHARMACEUTICAL"


def test_resolve_manufacturer_name_uses_name_for_equal_hit_tie_break() -> None:
    mapping = {"LIVALO": frozenset({"GAMMA", "BETA"})}

    assert manufacturer_resolver.resolve_manufacturer_name(("LIVALO",), mapping) == "BETA, GAMMA"


def test_resolve_manufacturer_name_returns_none_when_unmapped() -> None:
    assert manufacturer_resolver.resolve_manufacturer_name(("UNKNOWN",), {}) is None


def test_fetch_manufacturer_by_product_uses_korean_source_and_skips_empty(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_fetch_all(sql: str) -> list[dict[str, object]]:
        captured["sql"] = sql
        return [
            {"product": "LIVALO", "manufacturer": "제이더블유중외제약"},
            {"product": " livalo ", "manufacturer": "JW중외제약"},
            {"product": "NOMFR", "manufacturer": ""},
            {"product": None, "manufacturer": "제조사"},
        ]

    monkeypatch.setattr(manufacturer_resolver.db, "fetch_all", fake_fetch_all)

    assert manufacturer_resolver.fetch_manufacturer_by_product() == {
        "LIVALO": frozenset({"JW중외제약", "제이더블유중외제약"})
    }
    assert "iqvia_nsa_quarterly_raw" in captured["sql"]
    assert "MFR NAME KOR" in captured["sql"]


def test_cached_map_fetches_once_within_long_ttl(monkeypatch) -> None:
    calls = 0

    def fake_fetch() -> dict[str, frozenset[str]]:
        nonlocal calls
        calls += 1
        return {"LIVALO": frozenset({"제이더블유중외제약"})}

    monkeypatch.setattr(manufacturer_resolver, "fetch_manufacturer_by_product", fake_fetch)

    assert manufacturer_resolver.get_manufacturer_by_product() == manufacturer_resolver.get_manufacturer_by_product()
    assert calls == 1


def test_cached_map_refreshes_only_after_ttl(monkeypatch) -> None:
    fetches = iter(
        (
            {"LIVALO": frozenset({"OLD"})},
            {"LIVALO": frozenset({"NEW"})},
        )
    )
    now = 100.0

    monkeypatch.setattr(manufacturer_resolver, "fetch_manufacturer_by_product", lambda: next(fetches))
    monkeypatch.setattr(manufacturer_resolver.time, "monotonic", lambda: now)

    assert manufacturer_resolver.get_manufacturer_by_product()["LIVALO"] == frozenset({"OLD"})
    now += manufacturer_resolver.MANUFACTURER_CACHE_TTL_SECONDS - 1
    assert manufacturer_resolver.get_manufacturer_by_product()["LIVALO"] == frozenset({"OLD"})
    now += 1
    assert manufacturer_resolver.get_manufacturer_by_product()["LIVALO"] == frozenset({"NEW"})


def test_public_module_has_revision() -> None:
    importlib.reload(manufacturer_resolver)
    assert manufacturer_resolver.MANUFACTURER_RESOLVER_REVISION == "iqvia-mfr-kor-v1"


def test_public_module_standalone_import_does_not_load_heavy_dependencies() -> None:
    root = Path(__file__).resolve().parents[2]
    script = """
import json
import sys
import pipeline.scripts.api.manufacturer_resolver as resolver
print(json.dumps({
    "revision": resolver.MANUFACTURER_RESOLVER_REVISION,
    "pandas_loaded": "pandas" in sys.modules,
    "pyarrow_loaded": "pyarrow" in sys.modules,
    "duckdb_loaded": "duckdb" in sys.modules,
}, sort_keys=True))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "duckdb_loaded": False,
        "pandas_loaded": False,
        "pyarrow_loaded": False,
        "revision": "iqvia-mfr-kor-v1",
    }
