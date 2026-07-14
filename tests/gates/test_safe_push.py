from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "pipeline" / "scripts" / "gates" / "safe_push.py"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", f"commit {name}")
    return _git(repo, "rev-parse", "HEAD")


def _repository(tmp_path: Path) -> tuple[Path, Path, str]:
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "develop", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.name", "Gate Test")
    _git(repo, "config", "user.email", "gate@example.test")
    _git(repo, "remote", "add", "origin", str(remote))
    base = _commit(repo, "base.txt", "base\n")
    _git(repo, "push", "-u", "origin", "develop")
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/develop")
    return repo, remote, base


def _approved(path: Path, *shas: str) -> Path:
    path.write_text("\n".join(shas) + "\n", encoding="utf-8")
    return path


def _run(repo: Path, approved: Path, refspec: str = "HEAD:refs/heads/develop") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--repo",
            str(repo),
            "--approved-shas",
            str(approved),
            "--refspec",
            refspec,
            "--environment",
            "failure-injection",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_safe_push_fast_forwards_after_all_ancestry_checks(tmp_path: Path) -> None:
    repo, remote, base = _repository(tmp_path)
    head = _commit(repo, "feature.txt", "feature\n")

    result = _run(repo, _approved(tmp_path / "approved.txt", base))

    assert result.returncode == 0
    assert _git(remote, "rev-parse", "refs/heads/develop") == head
    assert "gate=safe_push" in result.stdout
    assert "checked=4" in result.stdout
    assert "population=4" in result.stdout
    assert "failures=0" in result.stdout
    assert "exit_code=0" in result.stdout


def test_safe_push_rejects_head_built_from_stale_develop(tmp_path: Path) -> None:
    repo, remote, base = _repository(tmp_path)
    stale_branch = _git(repo, "rev-parse", "HEAD")

    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(remote), str(other)], check=True, capture_output=True)
    _git(other, "config", "user.name", "Other Writer")
    _git(other, "config", "user.email", "other@example.test")
    _commit(other, "new-develop.txt", "new\n")
    _git(other, "push", "origin", "HEAD:develop")

    _git(repo, "checkout", "-b", "stale-feature", stale_branch)
    _commit(repo, "stale.txt", "stale\n")
    result = _run(repo, _approved(tmp_path / "approved.txt", base))

    assert result.returncode == 1
    assert "origin/develop is not an ancestor of HEAD" in result.stdout
    assert "exit_code=1" in result.stdout


def test_safe_push_rejects_missing_approved_commit(tmp_path: Path) -> None:
    repo, _, base = _repository(tmp_path)
    _git(repo, "checkout", "-b", "approved-side")
    approved_side = _commit(repo, "approved-side.txt", "approved\n")
    _git(repo, "checkout", "develop")
    _commit(repo, "feature.txt", "feature\n")

    result = _run(repo, _approved(tmp_path / "approved.txt", base, approved_side))

    assert result.returncode == 1
    assert f"approved commit {approved_side} is not an ancestor" in result.stdout
    assert "exit_code=1" in result.stdout


def test_safe_push_rejects_force_refspec(tmp_path: Path) -> None:
    repo, _, base = _repository(tmp_path)
    _commit(repo, "feature.txt", "feature\n")

    result = _run(
        repo,
        _approved(tmp_path / "approved.txt", base),
        refspec="+HEAD:refs/heads/develop",
    )

    assert result.returncode == 1
    assert "force refspecs are forbidden" in result.stdout
    assert "exit_code=1" in result.stdout
