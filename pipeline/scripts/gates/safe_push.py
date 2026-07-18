from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import sys
from typing import Sequence


DEFAULT_APPROVED_SHAS = Path(__file__).with_name("approved_shas.txt")
SAFE_REFSPEC = re.compile(r"^HEAD:refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*$")


@dataclass(frozen=True)
class PushGateResult:
    checked: int
    population: int
    failures: int
    environment: str
    details: tuple[str, ...]

    @property
    def exit_code(self) -> int:
        return 1 if self.failures else 0

    def render(self) -> str:
        fields = (
            "gate=safe_push",
            "classification=census",
            f"checked={self.checked}",
            f"population={self.population}",
            "missing=fail",
            "tolerance=exact git ancestry",
            f"failures={self.failures}",
            f"exit_code={self.exit_code}",
            f"environment={self.environment}",
        )
        return "\n".join((*self.details, *fields))


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )


def _approved_shas(path: Path) -> list[str]:
    shas: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.partition("#")[0].strip()
        if line:
            shas.append(line)
    if not shas:
        raise ValueError("approved SHA list must not be empty")
    return shas


def _valid_refspec(refspec: str) -> bool:
    if refspec.startswith("+") or not SAFE_REFSPEC.fullmatch(refspec):
        return False
    destination = refspec.partition(":")[2]
    return not any(token in destination for token in ("..", "//", "@{")) and not destination.endswith(
        ("/", ".", ".lock")
    )


def run_gate(
    *,
    repo: Path,
    remote: str,
    base_branch: str,
    approved_path: Path,
    refspec: str,
    environment: str,
) -> PushGateResult:
    details: list[str] = []
    failures = 0
    checked = 0

    try:
        approved = _approved_shas(approved_path)
    except (OSError, ValueError) as exc:
        return PushGateResult(0, 1, 1, environment, (str(exc),))

    population = len(approved) + 3
    checked += 1
    if not _valid_refspec(refspec):
        details.append(f"force refspecs are forbidden; expected HEAD:refs/heads/<branch>, got {refspec}")
        failures += 1

    fetch = _git(repo, "fetch", remote)
    if fetch.returncode != 0:
        details.append(f"git fetch {remote} failed: {fetch.stderr.strip()}")
        return PushGateResult(checked, population, failures + 1, environment, tuple(details))

    remote_base = f"{remote}/{base_branch}"
    checked += 1
    base_check = _git(repo, "merge-base", "--is-ancestor", remote_base, "HEAD")
    if base_check.returncode != 0:
        remote_sha = _git(repo, "rev-parse", remote_base).stdout.strip()
        head_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        details.append(
            f"{remote_base} is not an ancestor of HEAD; {remote_base}={remote_sha} HEAD={head_sha}"
        )
        failures += 1

    for sha in approved:
        checked += 1
        resolve = _git(repo, "rev-parse", "--verify", f"{sha}^{{commit}}")
        if resolve.returncode != 0:
            details.append(f"approved commit {sha} cannot be resolved")
            failures += 1
            continue
        ancestry = _git(repo, "merge-base", "--is-ancestor", sha, "HEAD")
        if ancestry.returncode != 0:
            details.append(f"approved commit {sha} is not an ancestor of HEAD")
            failures += 1

    if failures:
        return PushGateResult(checked, population, failures, environment, tuple(details))

    checked += 1
    push = _git(repo, "push", remote, refspec)
    if push.returncode != 0:
        details.append(f"non-force git push failed: {push.stderr.strip()}")
        failures += 1
    else:
        destination = refspec.partition(":")[2]
        local_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        remote_output = _git(repo, "ls-remote", remote, destination).stdout.split(maxsplit=1)
        remote_sha = remote_output[0] if remote_output else ""
        if remote_sha != local_sha:
            details.append(f"remote SHA mismatch: local={local_sha} remote={remote_sha}")
            failures += 1
        else:
            details.append(f"push verified: {destination}={remote_sha}")

    return PushGateResult(checked, population, failures, environment, tuple(details))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch, verify approved ancestry, and push without force")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--base-branch", default="develop")
    parser.add_argument("--approved-shas", type=Path, default=DEFAULT_APPROVED_SHAS)
    parser.add_argument("--refspec", required=True)
    parser.add_argument("--environment", default="local")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = run_gate(
        repo=args.repo.resolve(),
        remote=args.remote,
        base_branch=args.base_branch,
        approved_path=args.approved_shas.resolve(),
        refspec=args.refspec,
        environment=args.environment,
    )
    print(result.render())
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
