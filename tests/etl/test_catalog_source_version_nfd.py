"""Unit tests: catalog dim validators tolerate NFD Korean strings (R-1 s2 fix).

The MI Master staged on NFS carries NFD-normalized Korean filename/content while
the source-code EXPECTED_* literals are NFC. Validators must compare logical
content (NFC), not raw bytes. Reproduces the retry4 s2 catalog failure.
"""
from __future__ import annotations

import unicodedata

import pytest

from pipeline.etl.io.catalog.dim import brand_group, jw_products


def test_nfd_differs_from_nfc_for_korean_filename() -> None:
    nfc = jw_products.EXPECTED_SOURCE_FILE_VERSION
    nfd = unicodedata.normalize("NFD", nfc)
    # Sanity: the Korean filename genuinely differs byte-wise under NFD (else the
    # test would be vacuous and the old raw comparison would have passed too).
    assert nfd != nfc


def test_jw_products_source_file_version_accepts_nfd() -> None:
    nfd = unicodedata.normalize("NFD", jw_products.EXPECTED_SOURCE_FILE_VERSION)
    records = [
        {"strategic_market_id": "strategy_006", "source_file_version": nfd},
        {"strategic_market_id": "strategy_011", "source_file_version": nfd},
    ]
    out = jw_products._source_file_version_from_market_definition(records)
    assert out == jw_products.EXPECTED_SOURCE_FILE_VERSION  # no raise on NFD


def test_jw_products_source_file_version_rejects_real_mismatch() -> None:
    records = [{"strategic_market_id": "s", "source_file_version": "SOME_OTHER_FILE.xlsx"}]
    with pytest.raises(ValueError, match="source_file_version mismatch"):
        jw_products._source_file_version_from_market_definition(records)


def test_nfc_helpers_normalize() -> None:
    nfd = unicodedata.normalize("NFD", "악템라")
    assert jw_products._nfc(nfd) == "악템라"
    assert brand_group._nfc(nfd) == "악템라"
    assert brand_group._nfc(None) == "None"  # str() coercion preserved
