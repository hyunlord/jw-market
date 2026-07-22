"""Guard: the serving API must import without pyarrow.

pyarrow is an ETL-only dependency and is NOT installed in the slim API container image.
services.py does a top-level ``import pyarrow.parquet``; any serving module that imports
services (or pyarrow directly) crashes the API container at startup (CrashLoopBackOff) even
though offline pytest passes on a dev machine that happens to have pyarrow installed.

This test reproduces the container condition — pyarrow absent — in a clean subprocess and
asserts the FastAPI app imports and registers routes. It fails fast in CI if a serving-path
module reintroduces an eager pyarrow/services import.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import textwrap


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_app_imports_without_pyarrow() -> None:
    script = textwrap.dedent(
        """
        import sys, importlib.abc
        class _BlockPyarrow(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name == "pyarrow" or name.startswith("pyarrow."):
                    raise ModuleNotFoundError("No module named 'pyarrow'")
                return None
        sys.meta_path.insert(0, _BlockPyarrow())
        import pipeline.scripts.api.main as main
        assert hasattr(main, "app"), "app missing"
        n = len(main.app.routes)
        assert n > 0, "no routes registered"
        print("IMPORT_OK", n)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "serving app failed to import with pyarrow blocked — a serving-path module reintroduced "
        f"an eager pyarrow/services import.\nSTDERR:\n{result.stderr[-2000:]}"
    )
    assert "IMPORT_OK" in result.stdout, result.stdout
