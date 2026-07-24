from pathlib import Path

import pytest

from pipeline.orchestrator.iqvia_roles import (
    CANONICAL_NSA_FILENAME,
    IqviaRoleContractError,
    bind_iqvia_sources,
    canonical_nsa_source,
)


def test_role_binding_preserves_physical_source_directory(tmp_path: Path) -> None:
    nsa = tmp_path / "NSA" / CANONICAL_NSA_FILENAME
    chso = tmp_path / "CHSO" / "CHSO_KOR_SellOut_Basic_Feb-19-2026.xlsx"
    nsa.parent.mkdir()
    chso.parent.mkdir()
    nsa.write_bytes(b"nsa")
    chso.write_bytes(b"chso")

    sources = bind_iqvia_sources(tmp_path)

    assert [(source.role, source.relative_path.as_posix()) for source in sources] == [
        ("CHSO", f"CHSO/{chso.name}"),
        ("NSA", f"NSA/{CANONICAL_NSA_FILENAME}"),
    ]
    assert canonical_nsa_source(sources).path == nsa.resolve()


def test_root_level_iqvia_file_is_rejected_as_role_ambiguous(tmp_path: Path) -> None:
    (tmp_path / CANONICAL_NSA_FILENAME).write_bytes(b"nsa")

    with pytest.raises(IqviaRoleContractError, match="role cannot be derived"):
        bind_iqvia_sources(tmp_path)


def test_multiple_nsa_candidates_fail_closed_before_loader(tmp_path: Path) -> None:
    nsa = tmp_path / "NSA"
    nsa.mkdir()
    (nsa / CANONICAL_NSA_FILENAME).write_bytes(b"canonical")
    (nsa / "legacy.xlsx").write_bytes(b"legacy")

    with pytest.raises(IqviaRoleContractError, match="exactly one NSA"):
        canonical_nsa_source(bind_iqvia_sources(tmp_path))


def test_wrong_nsa_filename_fails_closed(tmp_path: Path) -> None:
    nsa = tmp_path / "NSA"
    nsa.mkdir()
    (nsa / "KOR_NSA_old.xlsx").write_bytes(b"old")

    with pytest.raises(IqviaRoleContractError, match=CANONICAL_NSA_FILENAME):
        canonical_nsa_source(bind_iqvia_sources(tmp_path))
