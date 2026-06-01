"""General mart UBIST chunking behavior."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline" / "scripts" / "etl"))

from pipeline.scripts.etl import layer3_compute_general_v3 as mod


def test_ubist_insert_builds_brand_rows_per_atc4(monkeypatch):
    """Insert mode should not hand the whole all-ATC4 frame to build_brand_rows."""

    base = pd.DataFrame(
        [
            {
                "source": "ubist",
                "brand_key": "alpha",
                "brand_name": "Alpha",
                "product_name": "Alpha Tab",
                "product_code": "A1",
                "atc4_code": "A10",
                "atc4_desc": "A10 desc",
                "period_yyyymm": "2025-01",
                "channel": "clinic",
                "specialty": "internal",
                "manufacturer": "M1",
                "company": "C1",
                "raw_sales": 100.0,
                "raw_volume": 10.0,
            },
            {
                "source": "ubist",
                "brand_key": "beta",
                "brand_name": "Beta",
                "product_name": "Beta Tab",
                "product_code": "B1",
                "atc4_code": "B20",
                "atc4_desc": "B20 desc",
                "period_yyyymm": "2025-01",
                "channel": "clinic",
                "specialty": "internal",
                "manufacturer": "M2",
                "company": "C2",
                "raw_sales": 200.0,
                "raw_volume": 20.0,
            },
        ]
    )
    seen_chunks: list[tuple[str, tuple[str, ...]]] = []
    deleted: list[str] = []
    inserted: list[tuple[str, int]] = []

    monkeypatch.setattr(mod, "load_catalog_key_map", lambda: {})
    monkeypatch.setattr(mod, "load_ubist_base_frame", lambda max_rows=None, ml=None: base)
    monkeypatch.setattr(mod, "delete_source_rows", lambda table, source: deleted.append(table))

    def fake_build_brand_rows(source, measure, frame, catalog_map):
        atc4_codes = tuple(sorted(frame["atc4_code"].unique().tolist()))
        seen_chunks.append((measure, atc4_codes))
        assert len(atc4_codes) == 1
        return [
            {
                "brand_key": f"{measure}-{atc4_codes[0]}",
                "brand_name": f"{measure}-{atc4_codes[0]}",
                "atc4_code": atc4_codes[0],
                "atc4_desc": None,
                "source": source,
                "measure": measure,
            }
        ]

    def fake_build_market_rows(source, measure, brand_rows):
        return [
            {
                "atc4_code": brand_rows[0]["atc4_code"],
                "atc4_desc": None,
                "source": source,
                "measure": measure,
            }
        ]

    monkeypatch.setattr(mod, "build_brand_rows", fake_build_brand_rows)
    monkeypatch.setattr(mod, "build_market_rows", fake_build_market_rows)
    monkeypatch.setattr(mod, "insert_rows", lambda table, columns, rows: inserted.append((table, len(rows))))

    _brand_rows, _market_rows, stats = mod.compute_general(source="ubist", insert=True)

    assert deleted == ["mart_general_brand_metric", "mart_general_market_metric"]
    assert len(seen_chunks) == 4
    assert all(len(atc4_codes) == 1 for _, atc4_codes in seen_chunks)
    assert stats["measures"]["sales"]["brand_rows"] == 2
    assert stats["measures"]["volume"]["brand_rows"] == 2
    assert inserted
