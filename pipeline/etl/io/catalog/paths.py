from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol


CATALOG_BUILD_RELATIVE = Path("parquet")
CATALOG_OUTPUT_RELATIVE = Path("output") / "catalog"
CATALOG_ROOT_ENV = "JW_MARKET_CATALOG_ROOT"
S2_REQUIRED_CATALOGS = frozenset(
    {
        "master_market_definition",
        "master_qa",
        "master_brand_consolidation",
        "master_mapping_table",
        "master_drug",
        "dim_jw_products",
        "dim_brand_group",
        "master_brand_consolidation_members",
        "dim_market_landscape",
        "dim_market_competitive_dynamics",
        "dim_market_target_priority",
        "ml_market",
        "cd_filter",
        "cd_market",
        "strategic_brand",
        "strategic_product",
        "cd_brand",
        "cd_product",
    }
)


class CatalogResult(Protocol):
    name: str
    output_path: Path


@dataclass(frozen=True)
class PublishedCatalog:
    name: str
    source_path: Path
    output_path: Path
    sha256: str


def build_catalog_root(output_root: Path) -> Path:
    return Path(output_root) / CATALOG_BUILD_RELATIVE


def resolve_catalog_root(output_root: Path, configured: Path | None = None) -> Path:
    if configured is not None:
        return Path(configured)
    env_value = os.environ.get(CATALOG_ROOT_ENV)
    if env_value:
        return Path(env_value)
    return Path(output_root) / CATALOG_OUTPUT_RELATIVE


def catalog_file(catalog_root: Path, name: str) -> Path:
    return Path(catalog_root) / name / f"{name}.parquet"


def publish_catalog_outputs(
    results: Iterable[CatalogResult],
    *,
    build_root: Path,
    catalog_root: Path,
    required_names: frozenset[str] | None = None,
) -> tuple[PublishedCatalog, ...]:
    """Promote one complete s2 build tree to the canonical catalog root."""

    build_root = Path(build_root).resolve()
    catalog_root = Path(catalog_root).resolve()
    sources: list[tuple[CatalogResult, Path, Path]] = []
    seen: set[Path] = set()
    for result in results:
        source = Path(result.output_path).resolve()
        try:
            relative = source.relative_to(build_root)
        except ValueError as exc:
            raise ValueError(f"catalog artifact outside catalog build root: {source}") from exc
        if source in seen:
            continue
        seen.add(source)
        if not source.is_file():
            raise FileNotFoundError(f"catalog build artifact not found: {source}")
        sources.append((result, source, catalog_root / relative))

    names = {result.name for result, _, _ in sources}
    missing_names = sorted((required_names or frozenset()) - names)
    if missing_names:
        raise FileNotFoundError(f"required catalog build artifacts not reported: {', '.join(missing_names)}")
    if not sources:
        raise FileNotFoundError("catalog build reported no artifacts")

    published: list[PublishedCatalog] = []
    for result, source, destination in sources:
        source_hash = _sha256(source)
        if destination != source:
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.publishing")
            try:
                shutil.copy2(source, temporary)
                if _sha256(temporary) != source_hash:
                    raise OSError(f"catalog publish SHA256 mismatch: {source} -> {destination}")
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        if _sha256(destination) != source_hash:
            raise OSError(f"catalog publish SHA256 mismatch: {source} -> {destination}")
        published.append(
            PublishedCatalog(
                name=result.name,
                source_path=source,
                output_path=destination,
                sha256=source_hash,
            )
        )
    return tuple(published)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
