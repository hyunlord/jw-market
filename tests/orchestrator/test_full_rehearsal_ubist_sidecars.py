from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from pipeline.orchestrator.full_rehearsal import RehearsalContractError
from pipeline.orchestrator.full_rehearsal_ubist_sidecars import install_ubist_sidecars


def _manifest(tmp_path: Path, payload: bytes = b"may") -> Path:
    ubist = tmp_path / "raw-ubist"
    iqvia = tmp_path / "raw-iqvia"
    ubist.mkdir()
    iqvia.mkdir()
    (ubist / "april.xlsx").write_bytes(b"xlsx")
    (iqvia / "q2.csv").write_bytes(b"csv")
    master = tmp_path / "mi-master.xlsx"
    master.write_bytes(b"xlsx")
    sidecar = tmp_path / "may.parquet"
    sidecar.write_bytes(payload)
    manifest = tmp_path / "input_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "ubist_source_dir": str(ubist),
                "iqvia_source_dir": str(iqvia),
                "mi_master": str(master),
                "ubist_parquet_sidecars": [
                    {
                        "path": str(sidecar),
                        "relative_path": "year=2026/month=05/data.parquet",
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return manifest


def test_install_ubist_sidecars_copies_verified_partition(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    target = tmp_path / "work" / "ubist"
    target.mkdir(parents=True)

    assert install_ubist_sidecars(manifest, target) == 0
    assert (target / "year=2026/month=05/data.parquet").read_bytes() == b"may"


def test_install_ubist_sidecars_refuses_existing_partition(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    target = tmp_path / "work" / "ubist"
    existing = target / "year=2026/month=05/data.parquet"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"existing")

    with pytest.raises(RehearsalContractError, match="refuses overwrite"):
        install_ubist_sidecars(manifest, target)

    assert existing.read_bytes() == b"existing"
