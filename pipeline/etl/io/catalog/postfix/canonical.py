from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from pipeline.etl.io.catalog.postfix.text import normalize_brand_name

DIMENSION_COLUMNS = [
    "class", "class_1", "class_2", "molecule", "dosage_form", "strength_pack", "nhi_type",
    "ox_gx", "fish_oil", "판매사", "제조사",
]


@dataclass(frozen=True)
class CanonicalBrand:
    name: str
    ml_id: str
    cd_id: str
    is_target: bool
    source_key: str | None = None
    contains: tuple[str, ...] = ()

    @property
    def general_brand_key(self) -> str:
        return self.source_key or self.name


CANONICAL_BRANDS: tuple[CanonicalBrand, ...] = (
    CanonicalBrand("라베칸", "ml_001", "cd_001", True),
    CanonicalBrand("라베칸듀오", "ml_001", "cd_001", False, source_key="라베칸듀오", contains=("라베칸 듀오",)),
    CanonicalBrand("제이클", "ml_002", "cd_002", True),
    CanonicalBrand("가드렛", "ml_003", "cd_003", True, contains=("GUARDLET",)),
    CanonicalBrand("가드메트", "ml_003", "cd_003", False, contains=("GUARDMET",)),
    CanonicalBrand("타발리스", "ml_004", "cd_004", True),
    CanonicalBrand("시그마트", "ml_005", "cd_005", True),
    CanonicalBrand("리바로", "ml_006", "cd_006", True),
    CanonicalBrand("리바로젯", "ml_006", "cd_006", False, contains=("리바로젯",)),
    CanonicalBrand("리바로페노", "ml_007", "cd_007", True, contains=("리바로페노",)),
    CanonicalBrand("리바로하이", "ml_008", "cd_008", True, contains=("리바로 하이",)),
    CanonicalBrand("리바로브이", "ml_008", "cd_009", False, contains=("리바로 브이",)),
    CanonicalBrand("트루패스", "ml_009", "cd_010", True),
    CanonicalBrand("피나스타", "ml_009", "cd_011", False),
    CanonicalBrand("제이다트", "ml_009", "cd_011", False),
    CanonicalBrand("뉴트로진", "ml_010", "cd_012", True),
    CanonicalBrand("모빌리아", "ml_010", "cd_013", False),
    CanonicalBrand("악템라", "ml_011", "cd_014", True),
    CanonicalBrand("페린젝트", "ml_012", "cd_015", True),
    CanonicalBrand("베노훼럼", "ml_012", "cd_015", False),
    CanonicalBrand("헴리브라", "ml_013", "cd_016", True),
    CanonicalBrand("위너프", "ml_014", "cd_018", True),
    CanonicalBrand("위너프A+", "ml_014", "cd_018", False, source_key="위너프에이플러스", contains=("위너프에이플러스",)),
    CanonicalBrand("엔커버", "ml_015", "cd_017", True),
    CanonicalBrand("플라주오피", "ml_016", "cd_019", True),
)


def _normalize(value: Any) -> str:
    return normalize_brand_name(value)


def _copy_dimensions(target: dict[str, Any], source: pd.Series | None) -> None:
    if source is None:
        return
    for column in DIMENSION_COLUMNS:
        if column in source.index:
            target[column] = source.get(column)


def _base_row(columns: Iterable[str]) -> dict[str, Any]:
    return {column: None for column in columns}


def _candidate_from_table(table: pd.DataFrame, spec: CanonicalBrand, id_col: str) -> pd.Series | None:
    scoped = table.loc[table["ml_id"] == spec.ml_id].copy()
    if id_col in scoped.columns:
        scoped = scoped.loc[scoped[id_col] == getattr(spec, id_col)]
    if scoped.empty:
        return None
    general_key = _normalize(spec.general_brand_key)
    name_key = _normalize(spec.name)
    scoped["_name_key"] = scoped["name"].map(_normalize)
    scoped["_merge_key"] = scoped["merge_name"].map(_normalize) if "merge_name" in scoped.columns else ""
    exact = scoped.loc[(scoped["_name_key"] == general_key) | (scoped["_merge_key"] == general_key)]
    if exact.empty and name_key != general_key:
        exact = scoped.loc[(scoped["_name_key"] == name_key) | (scoped["_merge_key"] == name_key)]
    if not exact.empty:
        return exact.iloc[0]
    for needle in spec.contains:
        contains = scoped.loc[scoped["name"].astype(str).str.contains(needle, case=False, regex=False, na=False)]
        if not contains.empty:
            return contains.iloc[0]
    return None


def _candidate_from_products(products: pd.DataFrame | None, spec: CanonicalBrand, id_col: str) -> pd.Series | None:
    if products is None or products.empty:
        return None
    scoped = products.loc[products["ml_id"] == spec.ml_id].copy()
    if id_col in scoped.columns:
        scoped = scoped.loc[scoped[id_col] == getattr(spec, id_col)]
    for needle in spec.contains:
        contains = scoped.loc[scoped["name"].astype(str).str.contains(needle, case=False, regex=False, na=False)]
        if not contains.empty:
            return contains.iloc[0]
    return None


def _canonical_row(table: pd.DataFrame, products: pd.DataFrame | None, spec: CanonicalBrand, *, id_col: str, row_index: int, brand_id_prefix: str) -> dict[str, Any]:
    row = _base_row(table.columns)
    candidate = _candidate_from_table(table, spec, id_col)
    product_candidate = _candidate_from_products(products, spec, id_col)
    if candidate is not None:
        row.update(candidate.to_dict())
    elif product_candidate is not None:
        _copy_dimensions(row, product_candidate)
    else:
        raise RuntimeError(f"No catalog/product candidate for {spec.name} ({spec.ml_id}/{spec.cd_id})")
    row["brand_id"] = f"{brand_id_prefix}_{row_index:03d}_{_normalize(spec.name)}"
    row["name"] = spec.name
    row["merge_name"] = spec.name
    row["ml_id"] = spec.ml_id
    if id_col in table.columns:
        row[id_col] = getattr(spec, id_col)
    row["is_jw"] = True
    row["is_target"] = spec.is_target
    row["canonical_name"] = spec.name
    row["general_brand_key"] = _normalize(spec.general_brand_key)
    row["strategy_id"] = spec.ml_id.replace("ml_", "strategy_")
    return row


def rebuild_catalog(table_path: Path, output_path: Path, *, id_col: str, products_path: Path | None, brand_id_prefix: str) -> pd.DataFrame:
    table = pd.read_parquet(table_path)
    products = pd.read_parquet(products_path) if products_path and products_path.exists() else None
    result = table.copy()
    result["is_jw"] = False
    result["is_target"] = False
    result["canonical_name"] = ""
    result["general_brand_key"] = result["name"].map(_normalize)
    result["strategy_id"] = result["ml_id"].astype(str).str.replace("ml_", "strategy_", regex=False)
    canonical_rows = [
        _canonical_row(table, products, spec, id_col=id_col, row_index=index, brand_id_prefix=brand_id_prefix)
        for index, spec in enumerate(CANONICAL_BRANDS, start=1)
    ]
    result = pd.concat([pd.DataFrame(canonical_rows, columns=result.columns), result], ignore_index=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_parquet(output_path, index=False)
    return result


def verify_canonical(df: pd.DataFrame) -> None:
    expected = {brand.name for brand in CANONICAL_BRANDS}
    actual = set(df.loc[df["is_jw"] == True, "name"].astype(str))
    missing = expected - actual
    extra = actual - expected
    if missing or extra:
        raise RuntimeError(f"Canonical mismatch. missing={sorted(missing)} extra={sorted(extra)}")
    if int((df["is_jw"] == True).sum()) != 25:
        raise RuntimeError("Expected exactly 25 is_jw=1 canonical rows")


def apply_canonical(catalog_dir: Path) -> dict[str, int]:
    strategic_brand = rebuild_catalog(
        catalog_dir / "strategic_brand" / "strategic_brand.parquet",
        catalog_dir / "strategic_brand" / "strategic_brand.parquet",
        id_col="cd_id",
        products_path=catalog_dir / "strategic_product" / "strategic_product.parquet",
        brand_id_prefix="sb_canonical",
    )
    cd_brand = rebuild_catalog(
        catalog_dir / "cd_brand" / "cd_brand.parquet",
        catalog_dir / "cd_brand" / "cd_brand.parquet",
        id_col="cd_id",
        products_path=catalog_dir / "cd_product" / "cd_product.parquet",
        brand_id_prefix="cb_canonical",
    )
    verify_canonical(strategic_brand)
    verify_canonical(cd_brand)
    return {"strategic_brand": int(len(strategic_brand)), "cd_brand": int(len(cd_brand))}
