"""Unit tests: catalog dim validators tolerate Unicode-normalization differences.

Confirmed on the VM: EXPECTED_SOURCE_FILE_VERSION source literals are stored NFD
(decomposed Korean jamo), while the market_definition value read from the xlsx is
NFC-composed. The old raw set-comparison mismatched on encoding (strings print
identically). The fix NFC-normalizes both sides at the validation sites.
Reproduces the retry4 s2 catalog failure.
"""
from __future__ import annotations

import unicodedata

import pytest

from pipeline.etl.io.catalog.dim import brand_group, jw_products


def test_expected_literal_is_nfd_and_nfc_differs() -> None:
    exp = jw_products.EXPECTED_SOURCE_FILE_VERSION
    nfc = unicodedata.normalize("NFC", exp)
    # The source literal is NFD; its NFC-composed form differs byte-wise (else the
    # test would be vacuous and the old raw comparison would not have failed).
    assert nfc != exp


def test_source_file_version_accepts_nfc_actual_vs_nfd_literal() -> None:
    # Real scenario: xlsx/market_definition provides the NFC-composed filename,
    # while EXPECTED_SOURCE_FILE_VERSION is the NFD source literal.
    nfc_actual = unicodedata.normalize("NFC", jw_products.EXPECTED_SOURCE_FILE_VERSION)
    assert nfc_actual != jw_products.EXPECTED_SOURCE_FILE_VERSION  # genuinely different bytes
    records = [
        {"strategic_market_id": "strategy_006", "source_file_version": nfc_actual},
        {"strategic_market_id": "strategy_011", "source_file_version": nfc_actual},
    ]
    out = jw_products._source_file_version_from_market_definition(records)
    assert out == jw_products.EXPECTED_SOURCE_FILE_VERSION  # no raise on NFC actual


def test_source_file_version_rejects_real_mismatch() -> None:
    records = [{"strategic_market_id": "s", "source_file_version": "SOME_OTHER_FILE.xlsx"}]
    with pytest.raises(ValueError, match="source_file_version mismatch"):
        jw_products._source_file_version_from_market_definition(records)


def test_nfc_helpers_normalize() -> None:
    # "악템라" typed here is NFC; feed its NFD form and expect NFC back.
    nfd = unicodedata.normalize("NFD", "악템라")
    assert nfd != "악템라"
    assert jw_products._nfc(nfd) == "악템라"
    assert brand_group._nfc(nfd) == "악템라"
    assert brand_group._nfc(None) == "None"
