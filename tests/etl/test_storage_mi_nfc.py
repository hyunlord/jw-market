from __future__ import annotations

import unicodedata
from pathlib import Path

from pipeline.etl.lib.storage import MI_MASTER_FILE_NAME, _nfc_path


def test_mi_master_file_name_is_nfc() -> None:
    # Regression gate: the constant must stay NFC. An NFD (decomposed jamo) literal
    # made get_mi_master_path() return a path that does not exist on a byte-exact
    # (Linux) filesystem where git checks the file out in NFC.
    assert unicodedata.is_normalized("NFC", MI_MASTER_FILE_NAME)
    assert not unicodedata.is_normalized("NFD", MI_MASTER_FILE_NAME) or MI_MASTER_FILE_NAME == unicodedata.normalize("NFC", MI_MASTER_FILE_NAME)


def test_nfc_path_normalizes_decomposed_input() -> None:
    nfd = unicodedata.normalize("NFD", "MI팀_시장 분석.xlsx")
    assert not unicodedata.is_normalized("NFC", nfd)
    out = _nfc_path(Path("/data") / nfd)
    assert unicodedata.is_normalized("NFC", str(out))
    assert out.name == unicodedata.normalize("NFC", nfd)


def test_nfc_path_resolves_nfd_literal_to_nfc_file(tmp_path: Path) -> None:
    # An on-disk file written in NFC (as git checks out Korean names) must be found
    # even when the code path was built from an NFD literal.
    nfc_name = unicodedata.normalize("NFC", "리바로 Master.xlsx")
    real = tmp_path / nfc_name
    real.write_text("x", encoding="utf-8")

    nfd_path = tmp_path / unicodedata.normalize("NFD", nfc_name)
    # A byte-exact filesystem does not resolve the NFD path directly...
    if not nfd_path.exists():  # true on Linux; macOS may auto-precompose
        assert _nfc_path(nfd_path).exists()  # ...but normalizing to NFC does.
    assert _nfc_path(nfd_path).name == nfc_name
