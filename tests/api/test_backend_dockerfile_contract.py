from __future__ import annotations

import ast
import importlib.util
import re
import shlex
from collections import deque
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DockerImportCoverage:
    reachable_modules: tuple[str, ...]
    unresolved_pipeline_imports: tuple[str, ...]
    uncovered_modules: tuple[str, ...]
    missing_copy_sources: tuple[str, ...]
    dynamic_import_unknowns: tuple[str, ...]


def _module_file(root: Path, module: str) -> Path | None:
    module_path = root.joinpath(*module.split("."))
    source_file = module_path.with_suffix(".py")
    if source_file.is_file():
        return source_file

    package_file = module_path / "__init__.py"
    if package_file.is_file():
        return package_file

    if module_path.is_dir():
        return module_path

    return None


def _resolve_import_from(
    *,
    current_module: str,
    current_file: Path,
    node: ast.ImportFrom,
) -> str | None:
    if node.level == 0:
        return node.module

    current_package = (
        current_module
        if current_file.name == "__init__.py"
        else current_module.rpartition(".")[0]
    )
    relative_name = f"{'.' * node.level}{node.module or ''}"
    try:
        return importlib.util.resolve_name(relative_name, current_package)
    except (ImportError, ValueError):
        return None


def _dynamic_import_calls(tree: ast.AST, source_file: Path, root: Path) -> list[str]:
    dynamic_call_names = {
        "__import__",
        "importlib.import_module",
        "importlib.util.spec_from_file_location",
    }
    unknowns: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        call_name = ast.unparse(node.func)
        if call_name not in dynamic_call_names:
            continue

        expression = ast.unparse(node.args[0]) if node.args else "<missing>"
        unknowns.append(
            f"{source_file.relative_to(root)}:{node.lineno}: "
            f"{call_name}({expression})"
        )
    return unknowns


def _static_pipeline_imports(
    *,
    root: Path,
    current_module: str,
    current_file: Path,
    tree: ast.AST,
) -> set[str]:
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(
                alias.name for alias in node.names if alias.name.startswith("pipeline")
            )
            continue

        if not isinstance(node, ast.ImportFrom):
            continue

        imported_base = _resolve_import_from(
            current_module=current_module,
            current_file=current_file,
            node=node,
        )
        if imported_base and imported_base.startswith("pipeline"):
            imported_modules.add(imported_base)
            for alias in node.names:
                candidate = f"{imported_base}.{alias.name}"
                if _module_file(root, candidate) is not None:
                    imported_modules.add(candidate)

    return imported_modules


def _reachable_pipeline_modules(
    root: Path, entrypoint_module: str
) -> tuple[dict[str, Path], tuple[str, ...], tuple[str, ...]]:
    queue = deque([entrypoint_module])
    reachable: dict[str, Path] = {}
    unresolved: set[str] = set()
    dynamic_unknowns: set[str] = set()

    while queue:
        module = queue.popleft()
        if module in reachable or module in unresolved:
            continue
        if module != "pipeline" and not module.startswith("pipeline."):
            continue

        source_file = _module_file(root, module)
        if source_file is None:
            unresolved.add(module)
            continue

        reachable[module] = source_file
        if source_file.is_dir():
            continue
        tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
        dynamic_unknowns.update(_dynamic_import_calls(tree, source_file, root))
        queue.extend(
            sorted(
                _static_pipeline_imports(
                    root=root,
                    current_module=module,
                    current_file=source_file,
                    tree=tree,
                )
            )
        )

    return (
        reachable,
        tuple(sorted(unresolved)),
        tuple(sorted(dynamic_unknowns)),
    )


def _dockerfile_logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    pending = ""
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pending = f"{pending} {stripped}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        lines.append(pending)
        pending = ""
    if pending:
        lines.append(pending)
    return lines


def _docker_copy_sources(dockerfile_text: str) -> tuple[str, ...]:
    sources: list[str] = []
    for line in _dockerfile_logical_lines(dockerfile_text):
        if not line.upper().startswith("COPY "):
            continue

        tokens = shlex.split(line[5:].strip())
        while tokens and tokens[0].startswith("--"):
            tokens.pop(0)
        if len(tokens) >= 2:
            sources.extend(token.removeprefix("./").rstrip("/") for token in tokens[:-1])
    return tuple(sources)


def _docker_entrypoint_module(dockerfile_text: str) -> str:
    match = re.search(r"\buvicorn\s+([A-Za-z_][A-Za-z0-9_.]*):", dockerfile_text)
    if match is None:
        raise AssertionError("Dockerfile does not expose a uvicorn module entrypoint")
    return match.group(1)


def _analyze_docker_import_coverage(
    *, root: Path, dockerfile: Path
) -> DockerImportCoverage:
    dockerfile_text = dockerfile.read_text(encoding="utf-8")
    copy_sources = _docker_copy_sources(dockerfile_text)
    missing_copy_sources = tuple(
        sorted(source for source in copy_sources if not (root / source).exists())
    )
    reachable, unresolved, dynamic_unknowns = _reachable_pipeline_modules(
        root, _docker_entrypoint_module(dockerfile_text)
    )

    uncovered_modules = []
    for module, source_file in reachable.items():
        relative_file = source_file.relative_to(root)
        if not any(
            relative_file == Path(copy_source)
            or Path(copy_source) in relative_file.parents
            for copy_source in copy_sources
        ):
            uncovered_modules.append(f"{module} ({relative_file})")

    return DockerImportCoverage(
        reachable_modules=tuple(sorted(reachable)),
        unresolved_pipeline_imports=unresolved,
        uncovered_modules=tuple(sorted(uncovered_modules)),
        missing_copy_sources=missing_copy_sources,
        dynamic_import_unknowns=dynamic_unknowns,
    )


def test_backend_image_covers_static_pipeline_import_graph() -> None:
    """Every statically reachable in-repo import must be present in the image."""
    report = _analyze_docker_import_coverage(
        root=Path(".").resolve(),
        dockerfile=Path("api/Dockerfile").resolve(),
    )

    assert not report.missing_copy_sources, (
        "Docker COPY sources do not exist: "
        f"{', '.join(report.missing_copy_sources)}"
    )
    assert not report.unresolved_pipeline_imports, (
        "Statically imported pipeline modules do not exist in the build context: "
        f"{', '.join(report.unresolved_pipeline_imports)}"
    )
    assert not report.uncovered_modules, (
        "Statically reachable pipeline modules are not covered by Docker COPY:\n"
        + "\n".join(report.uncovered_modules)
    )


def test_backend_requirements_include_import_time_etl_dependencies() -> None:
    """The entrypoint imports these ETL libraries before Uvicorn can start."""
    requirements = Path("pipeline/scripts/api/requirements.txt").read_text(
        encoding="utf-8"
    )

    missing = _missing_import_time_dependencies(requirements)
    assert not missing, (
        "API entrypoint dependencies are absent from its image requirements: "
        f"{', '.join(missing)}"
    )


def _missing_import_time_dependencies(requirements: str) -> tuple[str, ...]:
    required = ("openpyxl>=3.1", "pyarrow==24.0.0", "duckdb==1.5.4")
    return tuple(dependency for dependency in required if dependency not in requirements)


def test_contract_names_missing_import_time_dependency() -> None:
    requirements = "openpyxl>=3.1\npyarrow==24.0.0\n"

    assert _missing_import_time_dependencies(requirements) == ("duckdb==1.5.4",)


def _write_synthetic_api(
    root: Path,
    *,
    main_source: str,
    dockerfile_copies: tuple[str, ...] = ("pipeline/api",),
) -> Path:
    api_dir = root / "pipeline" / "api"
    api_dir.mkdir(parents=True)
    (api_dir / "main.py").write_text(main_source, encoding="utf-8")
    dockerfile = root / "Dockerfile"
    copy_lines = "\n".join(f"COPY {source} /app/{source}" for source in dockerfile_copies)
    dockerfile.write_text(
        f"{copy_lines}\n"
        "CMD uvicorn pipeline.api.main:app\n",
        encoding="utf-8",
    )
    return dockerfile


def test_contract_names_new_static_import_omitted_from_copy(tmp_path: Path) -> None:
    extra_dir = tmp_path / "pipeline" / "new_contract"
    extra_dir.mkdir(parents=True)
    (extra_dir / "__init__.py").write_text("", encoding="utf-8")
    (extra_dir / "rules.py").write_text("VALUE = 1\n", encoding="utf-8")
    dockerfile = _write_synthetic_api(
        tmp_path,
        main_source="from pipeline.new_contract import rules\napp = object()\n",
    )

    report = _analyze_docker_import_coverage(root=tmp_path, dockerfile=dockerfile)

    assert any("pipeline.new_contract.rules" in item for item in report.uncovered_modules)


def test_contract_rejects_copy_source_that_does_not_exist(tmp_path: Path) -> None:
    dockerfile = _write_synthetic_api(
        tmp_path,
        main_source="app = object()\n",
        dockerfile_copies=("pipeline/api", "pipeline/missing"),
    )

    report = _analyze_docker_import_coverage(root=tmp_path, dockerfile=dockerfile)

    assert report.missing_copy_sources == ("pipeline/missing",)


def test_contract_reports_dynamic_import_as_statically_unknown(tmp_path: Path) -> None:
    dynamic_dir = tmp_path / "pipeline" / "dynamic_only"
    dynamic_dir.mkdir(parents=True)
    (dynamic_dir / "__init__.py").write_text("", encoding="utf-8")
    dockerfile = _write_synthetic_api(
        tmp_path,
        main_source=(
            "import importlib\n"
            "name = 'pipeline.dynamic_only'\n"
            "importlib.import_module(name)\n"
            "app = object()\n"
        ),
    )

    report = _analyze_docker_import_coverage(root=tmp_path, dockerfile=dockerfile)

    assert "pipeline.dynamic_only" not in report.reachable_modules
    assert any(
        "importlib.import_module(name)" in item
        for item in report.dynamic_import_unknowns
    )


def test_contract_follows_lazy_conditional_and_guarded_imports(tmp_path: Path) -> None:
    for package in ("lazy", "conditional", "guarded"):
        package_dir = tmp_path / "pipeline" / package
        package_dir.mkdir(parents=True)
        (package_dir / "__init__.py").write_text("", encoding="utf-8")

    dockerfile = _write_synthetic_api(
        tmp_path,
        main_source=(
            "def load():\n"
            "    import pipeline.lazy\n"
            "if False:\n"
            "    import pipeline.conditional\n"
            "try:\n"
            "    import pipeline.guarded\n"
            "except ImportError:\n"
            "    pass\n"
            "app = object()\n"
        ),
    )

    report = _analyze_docker_import_coverage(root=tmp_path, dockerfile=dockerfile)

    assert {
        "pipeline.lazy",
        "pipeline.conditional",
        "pipeline.guarded",
    }.issubset(report.reachable_modules)
