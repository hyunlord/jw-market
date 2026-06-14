from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from pipeline.etl.io.catalog.postfix.canonical import apply_canonical
from pipeline.etl.io.catalog.postfix.ml003 import apply_ml003
from pipeline.etl.io.catalog.postfix.molecule import apply_molecule_worklist
from pipeline.etl.io.catalog.postfix.oxgx import apply_ox_gx
from pipeline.etl.io.catalog.postfix.rebuild_cd import rebuild_cd_brand
from pipeline.etl.io.catalog.postfix.rebuild_strategic import rebuild_strategic_brand


@dataclass(frozen=True)
class PostfixResult:
    name: str
    rows: int
    columns: list[str]
    output_path: Path
    stats: dict[str, Any]


def _catalog_file(catalog_dir: Path, name: str) -> Path:
    return catalog_dir / name / f"{name}.parquet"


def _result(catalog_dir: Path, name: str, stats: dict[str, Any]) -> PostfixResult:
    path = _catalog_file(catalog_dir, name)
    frame = pd.read_parquet(path)
    return PostfixResult(name=name, rows=int(len(frame)), columns=list(frame.columns), output_path=path, stats=stats)


def run_postfix(*, output_root: Path) -> list[PostfixResult]:
    """Run the six archive run_layer0_postfix stages against parquet catalog."""
    catalog_dir = output_root / "parquet"
    ubist_dir = output_root / "output" / "ubist"
    worklist_path = output_root / "inputs" / "molecule_v4_worklist.csv"
    if not worklist_path.exists():
        raise FileNotFoundError(f"required molecule worklist not found: {worklist_path}")
    results: list[PostfixResult] = []
    stats = apply_canonical(catalog_dir)
    results.append(_result(catalog_dir, "strategic_brand", {"step": "canonical", **stats}))
    results.append(_result(catalog_dir, "cd_brand", {"step": "canonical", **stats}))
    stats = apply_ml003(catalog_dir, ubist_dir)
    results.append(_result(catalog_dir, "strategic_brand", {"step": "fix_ml003", **stats}))
    stats = rebuild_strategic_brand(_catalog_file(catalog_dir, "strategic_brand"))
    results.append(_result(catalog_dir, "strategic_brand", {"step": "rebuild_strategic_brand", **stats}))
    stats = apply_ox_gx(catalog_dir, ubist_dir)
    results.append(_result(catalog_dir, "ml_market", {"step": "apply_oxgx", **stats}))
    results.append(_result(catalog_dir, "strategic_brand", {"step": "apply_oxgx", **stats}))
    stats = rebuild_cd_brand(catalog_dir)
    results.append(_result(catalog_dir, "cd_brand", {"step": "rebuild_cd_brand", **stats}))
    stats = apply_molecule_worklist(catalog_dir, worklist_path)
    results.append(_result(catalog_dir, "strategic_brand", {"step": "apply_molecule_worklist", **stats}))
    results.append(_result(catalog_dir, "strategic_product", {"step": "apply_molecule_worklist", **stats}))
    results.append(_result(catalog_dir, "cd_brand", {"step": "apply_molecule_worklist", **stats}))
    results.append(_result(catalog_dir, "cd_product", {"step": "apply_molecule_worklist", **stats}))
    return results
