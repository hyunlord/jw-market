from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from pipeline.etl.io.mart import strategic_cd, strategic_constants, strategic_ml


@pytest.mark.parametrize(
    ("module", "catalog_names"),
    (
        (strategic_ml, ("ml_market", "strategic_brand", "strategic_product")),
        (strategic_cd, ("cd_market", "cd_brand", "cd_filter")),
    ),
)
def test_s5_catalog_consumers_use_canonical_runtime_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module: object,
    catalog_names: tuple[str, ...],
) -> None:
    root = tmp_path / "catalog"
    for name in catalog_names:
        path = root / name / f"{name}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    observed: list[Path] = []

    def read_parquet(path: Path) -> pd.DataFrame:
        observed.append(Path(path))
        if Path(path).stem in {"strategic_brand", "cd_brand"}:
            return pd.DataFrame({"name": []})
        return pd.DataFrame()

    monkeypatch.delenv("S5_CATALOG_DIR", raising=False)
    monkeypatch.setenv("JW_MARKET_CATALOG_ROOT", str(root))
    monkeypatch.setattr(module.pd, "read_parquet", read_parquet)
    monkeypatch.setattr(module, "drop_strict_excluded_rows", lambda frame, _label: frame)

    module.load_catalogs()

    assert observed == [root / name / f"{name}.parquet" for name in catalog_names]


def test_s5_catalog_root_fails_closed_without_configuration_or_existing_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("S5_CATALOG_DIR", raising=False)
    monkeypatch.delenv("JW_MARKET_CATALOG_ROOT", raising=False)
    monkeypatch.setattr(strategic_constants, "PROJECT_ROOT", tmp_path / "missing-project")

    with pytest.raises(FileNotFoundError, match="S5 catalog root not found"):
        strategic_constants.catalog_dir()


def test_s5_catalog_file_fails_closed_when_artifact_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "catalog"
    root.mkdir()
    monkeypatch.setenv("S5_CATALOG_DIR", str(root))

    with pytest.raises(FileNotFoundError, match=r"S5 catalog artifact not found: .*ml_market\.parquet"):
        strategic_constants.catalog_file("ml_market")


def test_s5_catalog_root_is_resolved_after_import(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    monkeypatch.setenv("S5_CATALOG_DIR", str(first))
    assert strategic_constants.catalog_dir() == first

    monkeypatch.setenv("S5_CATALOG_DIR", str(second))
    assert strategic_constants.catalog_dir() == second
