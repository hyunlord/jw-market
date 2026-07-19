"""Install verified UBIST parquet sidecars into an isolated rehearsal tree."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from pipeline.orchestrator.full_rehearsal import RehearsalContractError, load_input_manifest


def install_ubist_sidecars(manifest_path: Path, target_dir: Path) -> int:
    manifest = load_input_manifest(manifest_path)
    root = target_dir.resolve()
    if not root.is_dir():
        raise RehearsalContractError(f"UBIST parquet target does not exist: {root}")

    for sidecar in manifest.ubist_parquet_sidecars:
        target = (root / sidecar.relative_path).resolve()
        if root not in target.parents:
            raise RehearsalContractError(
                f"UBIST sidecar destination escapes target: {sidecar.relative_path}"
            )
        if target.exists():
            raise RehearsalContractError(f"UBIST sidecar refuses overwrite: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with sidecar.path.open("rb") as source, target.open("xb") as destination:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    destination.write(chunk)
            actual_sha = _sha256_file(target)
            if actual_sha != sidecar.sha256:
                raise RehearsalContractError(
                    f"installed UBIST sidecar SHA256 mismatch: expected {sidecar.sha256}, "
                    f"got {actual_sha}"
                )
        except Exception:
            target.unlink(missing_ok=True)
            raise
    return 0


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--target-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        return install_ubist_sidecars(args.manifest, args.target_dir)
    except RehearsalContractError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
