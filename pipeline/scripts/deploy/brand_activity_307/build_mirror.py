#!/usr/bin/env python3
# /// script
# dependencies = [
#   "typer>=0.12.0",
# ]
# ///
# ─── How to run ───
# uv run --script pipeline/scripts/deploy/brand_activity_307/build_mirror.py --output /tmp/llmops_307_mirror
"""Build the deployable gitea llmops/307 mirror from jw-market sources."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Final, NewType

import typer

REPO_ROOT: Final = Path(__file__).resolve().parents[4]

ModuleName = NewType("ModuleName", str)


@dataclass(frozen=True, slots=True)
class MirrorPlan:
    """Mirror inputs copied into the llmops/307 deployment repo."""

    entry_modules: tuple[ModuleName, ...]
    source_files: tuple[Path, ...]


class MirrorPlanError(RuntimeError):
    """Raised when the mirror manifest contains an unsupported support file."""


PLAN: Final = MirrorPlan(
    entry_modules=(
        ModuleName("pipeline.scripts.serving.brand_activity.topic_server"),
        ModuleName("pipeline.scripts.analysis.brand_activity.auto_topic.run_auto_topic"),
    ),
    source_files=(
        Path("pipeline/scripts/deploy/brand_activity_307/requirements.txt"),
        Path("pipeline/scripts/deploy/brand_activity_307/DEPLOY_NOTES.md"),
        Path("pipeline/etl/config/expected_row_counts.yaml"),
    ),
)

VERIFY_MODULES: Final = (
    ModuleName("pipeline.scripts.serving.brand_activity.topic_server"),
    ModuleName("pipeline.scripts.etl.brand_activity.brand_activity_replay"),
    ModuleName("pipeline.etl.io.catalog.master.qa"),
    ModuleName("pipeline.etl.io.catalog.master.mapping_table"),
)

SERVICE_CONTRACT_SHIM: Final = """from typing import Any, Dict


async def service(config: Dict[str, Any], data: Dict[str, Any]):
    data.update(config=config)
    return {
        "status": "ok",
        "message": "brand-activity topic runner is exposed through the MCP relay",
    }
"""


def build_mirror(output: Path, *, verify: bool = False) -> dict[str, int]:
    """Copy the code-serving 307 source subset into an output directory."""
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    copied = 0
    for path in _collect_python_sources(PLAN.entry_modules):
        _copy_file(path, output / path.relative_to(REPO_ROOT))
        copied += 1
    for file_path in PLAN.source_files:
        _copy_file(REPO_ROOT / file_path, output / _mirror_file_name(file_path))
        copied += 1
    _write_service_contract_shim(output)
    copied += 1
    if verify:
        verify_mirror_imports(output)
    return {"files": copied}


def verify_mirror_imports(output: Path) -> None:
    """Import deploy-critical modules and the template service contract."""
    _verify_pipeline_imports(output)
    _verify_template_service_contract(output)


def _verify_pipeline_imports(output: Path) -> None:
    """Import deploy-critical pipeline modules with only the mirror on PYTHONPATH."""
    imports = "\n".join(f"import {module}" for module in VERIFY_MODULES)
    script = f"{imports}\nprint('isolated import ok')\n"
    with tempfile.TemporaryDirectory(prefix="mirror-import-") as directory:
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=directory,
            env={
                "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(output),
            },
            check=False,
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        raise MirrorPlanError(
            "isolated mirror import failed\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


def _verify_template_service_contract(output: Path) -> None:
    """Recreate the template `/app/src/service` layout and import its shim."""
    script = (
        "from service import service\n"
        "import inspect\n"
        "assert hasattr(service, 'service')\n"
        "assert inspect.iscoroutinefunction(service.service)\n"
        "print('service contract import ok')\n"
    )
    with tempfile.TemporaryDirectory(prefix="mirror-template-contract-") as directory:
        root = Path(directory)
        service_root = root / "src" / "service"
        shutil.copytree(output, service_root)
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=root,
            env={
                "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(root / "src"),
            },
            check=False,
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        raise MirrorPlanError(
            "template service contract import failed\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


def _collect_python_sources(entries: tuple[ModuleName, ...]) -> tuple[Path, ...]:
    pending = list(entries)
    seen: set[ModuleName] = set()
    paths: set[Path] = set()
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        module_path = _module_path(module)
        if module_path is None:
            continue
        paths.add(module_path)
        for init_path in _package_init_paths(module_path):
            paths.add(init_path)
            init_module = _module_name_for_path(init_path)
            if init_module not in seen:
                pending.append(init_module)
        pending.extend(imported for imported in _pipeline_imports(module, module_path) if imported not in seen)
    return tuple(sorted(paths))


def _pipeline_imports(module: ModuleName, path: Path) -> tuple[ModuleName, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[ModuleName] = set()
    for node in ast.walk(tree):
        match node:
            case ast.Import(names=names):
                for alias in names:
                    _add_pipeline_module(imports, ModuleName(alias.name))
            case ast.ImportFrom(module=imported_module, names=names, level=level):
                base = _import_from_base(module, path, imported_module, level)
                if base is None:
                    continue
                _add_pipeline_module(imports, base)
                for alias in names:
                    if alias.name == "*":
                        continue
                    _add_pipeline_module(imports, ModuleName(f"{base}.{alias.name}"))
            case _:
                continue
    return tuple(sorted(imports))


def _add_pipeline_module(imports: set[ModuleName], module: ModuleName) -> None:
    if not str(module).startswith("pipeline."):
        return
    if _module_path(module) is not None:
        imports.add(module)


def _import_from_base(
    current_module: ModuleName,
    current_path: Path,
    imported_module: str | None,
    level: int,
) -> ModuleName | None:
    if level == 0:
        if imported_module is None:
            return None
        return ModuleName(imported_module)
    package = _current_package(current_module, current_path)
    parts = package.split(".")
    if level > len(parts):
        return None
    base_parts = parts[: len(parts) - level + 1]
    if imported_module:
        base_parts.extend(imported_module.split("."))
    return ModuleName(".".join(base_parts))


def _current_package(module: ModuleName, path: Path) -> str:
    module_text = str(module)
    if path.name == "__init__.py":
        return module_text
    return module_text.rsplit(".", maxsplit=1)[0]


def _module_path(module: ModuleName) -> Path | None:
    relative = Path(*str(module).split("."))
    module_file = REPO_ROOT / relative.with_suffix(".py")
    if module_file.is_file():
        return module_file
    package_file = REPO_ROOT / relative / "__init__.py"
    if package_file.is_file():
        return package_file
    return None


def _package_init_paths(module_path: Path) -> set[Path]:
    paths: set[Path] = set()
    relative_parent = module_path.relative_to(REPO_ROOT).parent
    for index in range(1, len(relative_parent.parts) + 1):
        init_path = REPO_ROOT / Path(*relative_parent.parts[:index]) / "__init__.py"
        if init_path.is_file():
            paths.add(init_path)
    return paths


def _module_name_for_path(path: Path) -> ModuleName:
    relative = path.relative_to(REPO_ROOT)
    if relative.name == "__init__.py":
        relative = relative.parent
    else:
        relative = relative.with_suffix("")
    return ModuleName(".".join(relative.parts))


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _write_service_contract_shim(output: Path) -> None:
    """Write the root module expected by template-code-serving."""
    (output / "service.py").write_text(SERVICE_CONTRACT_SHIM, encoding="utf-8")


def _mirror_file_name(path: Path) -> Path:
    if path.name == "requirements.txt":
        return Path("requirements.txt")
    if path.name == "DEPLOY_NOTES.md":
        return Path("DEPLOY_NOTES.md")
    return path


def main(
    output: Path = typer.Option(..., "--output", "--out", help="Mirror output directory."),
    verify: bool = typer.Option(False, "--verify", help="Run isolated import verification after building."),
) -> None:
    """Create a fresh local mirror directory."""
    summary = build_mirror(output, verify=verify)
    typer.echo(f"mirror={output}")
    typer.echo(f"files={summary['files']}")
    if verify:
        typer.echo("verify=isolated-import-ok")
        typer.echo("verify=service-contract-import-ok")


if __name__ == "__main__":
    typer.run(main)
