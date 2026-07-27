from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.scripts.gates.canonical_regression_contract import (
    Baseline,
    GitMetadata,
    RegressionSummary,
    RegressionVerdict,
    compare_summary,
    parse_junit as parse_junit_contract,
    verify_baseline_binding,
)

DEFAULT_BASELINE = Path(__file__).with_name("canonical_regression_baseline.json")


def parse_junit(path: Path, *, repo_root: Path = ROOT) -> RegressionSummary:
    return parse_junit_contract(path, repo_root=repo_root)


def _git_output(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _git_metadata(baseline_commit: str) -> GitMetadata:
    head = _git_output("rev-parse", "HEAD")
    head_tree = _git_output("rev-parse", "HEAD^{tree}")
    baseline_tree = _git_output("rev-parse", f"{baseline_commit}^{{tree}}")
    descendant = subprocess.run(
        ["git", "merge-base", "--is-ancestor", baseline_commit, head],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if descendant.returncode not in (0, 1):
        raise RuntimeError(f"git merge-base failed: {descendant.stderr.strip()}")
    return GitMetadata(
        head_commit=head,
        head_tree_digest=head_tree,
        baseline_commit_tree_digest=baseline_tree,
        head_descends_from_baseline=descendant.returncode == 0,
    )


def _require_clean_worktree() -> None:
    status = _git_output("status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise RuntimeError(f"canonical regression requires a clean worktree:\n{status}")


def run_regression(*, baseline_path: Path, output_dir: Path) -> int:
    _require_clean_worktree()
    baseline = Baseline.load(baseline_path)
    git_metadata = _git_metadata(baseline.baseline_commit)
    verify_baseline_binding(baseline, git_metadata)
    output_dir.mkdir(parents=True, exist_ok=False)
    junit_path = output_dir / "junit.xml"
    command = (sys.executable, "-m", "pytest", "-q", "-p", "no:randomly", "tests", f"--junitxml={junit_path}")
    env = os.environ.copy()
    pythonpath = f"{ROOT / 'pipeline/scripts/ingest_hook'}:{ROOT}"
    if env.get("PYTHONPATH"):
        pythonpath = f"{pythonpath}:{env['PYTHONPATH']}"
    env["PYTHONPATH"] = pythonpath
    result = subprocess.run(command, cwd=ROOT, env=env, check=False, capture_output=True, text=True)
    (output_dir / "pytest_stdout.txt").write_text(result.stdout, encoding="utf-8")
    (output_dir / "pytest_stderr.txt").write_text(result.stderr, encoding="utf-8")
    summary = parse_junit(junit_path)
    verdict = compare_summary(summary, baseline)
    payload = {
        "schema_version": 2,
        "worktree": str(ROOT),
        "git": asdict(git_metadata),
        "command": list(command),
        "pytest_returncode": result.returncode,
        "summary": asdict(summary),
        "baseline": asdict(baseline),
        "verdict": asdict(verdict),
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    (output_dir / "verdict.json").write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if verdict.ok else 1


def _default_output_dir() -> Path:
    return Path(tempfile.gettempdir()) / f"jw-market-canonical-regression-{os.getpid()}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run and compare the Stage E canonical root regression suite.")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    output_dir = args.output_dir or _default_output_dir()
    try:
        return run_regression(baseline_path=args.baseline, output_dir=output_dir)
    except (OSError, RuntimeError, ValueError, ET.ParseError, json.JSONDecodeError) as exc:
        print(f"canonical regression gate failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
