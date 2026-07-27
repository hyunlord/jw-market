from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from pipeline.etl.io.catalog.paths import publish_catalog_outputs
from pipeline.scripts.etl import materialize_catalog as cli


@dataclass(frozen=True)
class _Result:
    name: str
    output_path: Path
    rows: int = 1


def _publish(root: Path, name: str) -> None:
    build = root.parent / "build"
    artifact = build / name / f"{name}.parquet"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"fixture")
    publish_catalog_outputs(
        [_Result(name=name, output_path=artifact)],
        build_root=build,
        catalog_root=root,
    )


def test_local_cli_materializes_required_snapshot(tmp_path: Path, capsys) -> None:
    source = tmp_path / "storage"
    destination = tmp_path / "runtime" / "catalog"
    _publish(source, "strategic_brand")

    assert cli.main(
        [
            "--source-root",
            str(source),
            "--destination-root",
            str(destination),
            "--required-name",
            "strategic_brand",
        ]
    ) == 0

    assert (destination / "strategic_brand" / "strategic_brand.parquet").is_file()
    assert "catalog_materialized" in capsys.readouterr().out


def test_local_cli_requires_explicit_source(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="source-root"):
        cli.main(["--destination-root", str(tmp_path / "catalog")])
