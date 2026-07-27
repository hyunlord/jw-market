from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol


CATALOG_BUILD_RELATIVE = Path("parquet")
CATALOG_OUTPUT_RELATIVE = Path("output") / "catalog"
CATALOG_ROOT_ENV = "JW_MARKET_CATALOG_ROOT"
CATALOG_MANIFEST_NAME = "CATALOG_MANIFEST.json"
CATALOG_MANIFEST_SCHEMA_VERSION = 1
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
    rows: int


@dataclass(frozen=True)
class PublishedCatalog:
    name: str
    source_path: Path
    output_path: Path
    sha256: str


class CatalogProvisioningError(RuntimeError):
    """Base class for catalog environment and integrity failures."""


class CatalogEnvironmentError(CatalogProvisioningError):
    """Raised when a required catalog materialization is absent or incomplete."""


class CatalogIntegrityError(CatalogProvisioningError):
    """Raised when catalog bytes do not match their published manifest."""


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
    _write_catalog_manifest(catalog_root, tuple(published), results_by_name={
        result.name: result for result, _, _ in sources
    })
    return tuple(published)


def validate_catalog_materialization(
    catalog_root: Path,
    *,
    required_names: frozenset[str] = frozenset(),
) -> tuple[PublishedCatalog, ...]:
    """Validate a complete checksummed catalog snapshot without modifying it."""

    root = Path(catalog_root).resolve()
    if not root.is_dir():
        raise CatalogEnvironmentError(f"catalog root not found: {root}")
    manifest_path = root / CATALOG_MANIFEST_NAME
    if not manifest_path.is_file():
        raise CatalogEnvironmentError(f"catalog manifest not found: {manifest_path}")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogIntegrityError(f"catalog manifest is unreadable: {manifest_path}") from exc
    if payload.get("schema_version") != CATALOG_MANIFEST_SCHEMA_VERSION:
        raise CatalogIntegrityError(
            f"unsupported catalog manifest schema: {payload.get('schema_version')!r}"
        )
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise CatalogIntegrityError("catalog manifest artifacts must be a non-empty list")

    validated: list[PublishedCatalog] = []
    seen_names: set[str] = set()
    for item in artifacts:
        if not isinstance(item, dict):
            raise CatalogIntegrityError("catalog manifest artifact must be an object")
        name = item.get("name")
        relative = item.get("path")
        expected_sha = item.get("sha256")
        expected_size = item.get("size")
        if (
            not isinstance(name, str)
            or not name
            or name in seen_names
            or relative != f"{name}/{name}.parquet"
            or not isinstance(expected_sha, str)
            or len(expected_sha) != 64
            or not isinstance(expected_size, int)
            or expected_size < 0
        ):
            raise CatalogIntegrityError(f"invalid catalog manifest artifact: {item!r}")
        seen_names.add(name)
        path = (root / relative).resolve()
        if root not in path.parents:
            raise CatalogIntegrityError(f"catalog artifact escapes root: {relative!r}")
        if not path.is_file():
            raise CatalogEnvironmentError(f"missing catalog artifact: {relative}")
        actual_size = path.stat().st_size
        if actual_size != expected_size:
            raise CatalogIntegrityError(
                f"catalog size mismatch: {relative} expected={expected_size} actual={actual_size}"
            )
        actual_sha = _sha256(path)
        if actual_sha != expected_sha:
            raise CatalogIntegrityError(
                f"catalog SHA256 mismatch: {relative} expected={expected_sha} actual={actual_sha}"
            )
        validated.append(
            PublishedCatalog(
                name=name,
                source_path=path,
                output_path=path,
                sha256=actual_sha,
            )
        )

    missing_names = sorted(required_names - seen_names)
    if missing_names:
        raise CatalogEnvironmentError(
            f"required catalog artifacts not listed in manifest: {', '.join(missing_names)}"
        )
    return tuple(validated)


def materialize_catalog(
    *,
    source_root: Path,
    destination_root: Path,
    required_names: frozenset[str] = frozenset(),
) -> tuple[PublishedCatalog, ...]:
    """Materialize one immutable catalog snapshot from checksummed storage."""

    source = Path(source_root).resolve()
    destination = Path(destination_root).resolve()
    source_items = validate_catalog_materialization(
        source,
        required_names=required_names,
    )
    source_manifest = (source / CATALOG_MANIFEST_NAME).read_bytes()
    if destination.exists():
        destination_items = validate_catalog_materialization(
            destination,
            required_names=required_names,
        )
        if (destination / CATALOG_MANIFEST_NAME).read_bytes() != source_manifest:
            raise CatalogIntegrityError(
                f"catalog destination already contains a different snapshot: {destination}"
            )
        return destination_items

    temporary = destination.with_name(f".{destination.name}.materializing-{os.getpid()}")
    if temporary.exists():
        raise CatalogEnvironmentError(f"catalog materialization scratch already exists: {temporary}")
    try:
        for item in source_items:
            relative = item.output_path.relative_to(source)
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item.output_path, target)
        (temporary / CATALOG_MANIFEST_NAME).write_bytes(source_manifest)
        validate_catalog_materialization(temporary, required_names=required_names)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return validate_catalog_materialization(destination, required_names=required_names)


def _write_catalog_manifest(
    catalog_root: Path,
    published: tuple[PublishedCatalog, ...],
    *,
    results_by_name: dict[str, CatalogResult],
) -> None:
    artifacts = [
        {
            "name": item.name,
            "path": item.output_path.relative_to(catalog_root).as_posix(),
            "rows": int(results_by_name[item.name].rows),
            "sha256": item.sha256,
            "size": item.output_path.stat().st_size,
        }
        for item in sorted(published, key=lambda candidate: candidate.name)
    ]
    payload = {
        "schema_version": CATALOG_MANIFEST_SCHEMA_VERSION,
        "artifacts": artifacts,
    }
    manifest = catalog_root / CATALOG_MANIFEST_NAME
    temporary = manifest.with_name(f".{manifest.name}.publishing")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, manifest)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
