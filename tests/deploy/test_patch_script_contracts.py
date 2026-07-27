"""Contracts every cluster-mutating patch script in this repo must satisfy.

The 2026-07-27 near-miss came from a script that was never in the repo, so nothing could
have caught it. These tests are the repo-level guard: any tracked script that issues a
JSON Patch against a container array must resolve its index by NAME and assert that
resolution with a ``test`` op, and no tracked script may hardcode an array index into a
patch path.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

HOOK_SCRIPT = REPO / "deploy/k8s/ingest-hook/apply-hook-image-refs.sh"
BACKEND_SCRIPT = REPO / "deploy/k8s/jw-market/apply-backend-api-resources.sh"
GATE = REPO / "pipeline/scripts/deploy/cache_cause_deploy_gate.py"

# A patch path with a literal integer index, e.g. /containers/0/ or /env/6/value
HARDCODED_INDEX = re.compile(
    r'"?/spec/template/spec/containers/[0-9]+|'
    r'/(env|volumeMounts|volumes|initContainers|ports)/[0-9]+/'
)


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "-C", str(REPO), "ls-files"],
                         capture_output=True, text=True, check=True).stdout
    return [REPO / line for line in out.splitlines() if line]


# Where executable deploy tooling lives. tests/ is deliberately excluded: the RED
# reproduction in test_k8s_env_patch.py must contain the forbidden pattern in order to
# demonstrate it, and its own docstring mentions "kubectl patch", which is what made a
# first version of this guard flag it. Excluding tests keeps the guard about scripts that
# can actually mutate a cluster.
SCRIPT_ROOTS = ("deploy/", "pipeline/scripts/", "scripts/", "ops/")


def mutating_scripts() -> list[Path]:
    """Tracked, non-test scripts under a script root that issue a cluster mutation."""
    found = []
    for path in tracked_files():
        rel = path.relative_to(REPO).as_posix()
        if not rel.startswith(SCRIPT_ROOTS):
            continue
        if "/tests/" in f"/{rel}" or Path(rel).name.startswith("test_"):
            continue
        if path.suffix not in {".sh", ".py"} or not path.is_file():
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if re.search(r"kubectl[^\n]*\b(patch|apply|replace)\b", body):
            found.append(path)
    return found


def test_the_guard_actually_selects_the_scripts_it_claims_to():
    """A guard that selects nothing would pass vacuously."""
    selected = {p.relative_to(REPO).as_posix() for p in mutating_scripts()}
    assert "deploy/k8s/ingest-hook/apply-hook-image-refs.sh" in selected
    assert "deploy/k8s/jw-market/apply-backend-api-resources.sh" in selected
    assert not any(s.startswith("tests/") for s in selected)
    assert len(selected) >= 2


def test_the_two_scripts_under_contract_exist():
    assert HOOK_SCRIPT.is_file()
    assert BACKEND_SCRIPT.is_file()
    assert GATE.is_file()


def test_no_tracked_script_hardcodes_a_container_or_env_index_in_a_patch_path():
    """The defect class, forbidden repo-wide.

    ``$idx``/``{ci}`` style interpolation is fine — that is a resolved value. A literal
    integer in a patch path is not.
    """
    offenders = []
    for path in mutating_scripts():
        body = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(body.splitlines(), 1):
            if line.lstrip().startswith(("#", "//")):
                continue          # commentary about the defect is allowed
            if HARDCODED_INDEX.search(line):
                offenders.append(f"{path.relative_to(REPO)}:{lineno}: {line.strip()[:100]}")
    assert offenders == [], "hardcoded array index in a patch path:\n" + "\n".join(offenders)


def test_hook_script_resolves_by_name_and_never_names_an_index():
    body = HOOK_SCRIPT.read_text(encoding="utf-8")
    # delegates resolution to the tested module
    assert "from pipeline.scripts.deploy.k8s_env_patch import build_patch" in body
    assert "INGEST_JOB_IMAGE" in body and "APP_VERSION" in body
    # single atomic patch, applied once
    assert body.count("kubectl -n \"$namespace\" patch deploy") == 1
    assert "--type=json" in body
    # post-apply it proves BOTH reference points carry the digest
    assert "container image  =" in body and "INGEST_JOB_IMAGE =" in body
    assert body.count("exit 1") >= 2
    # refuses a mutable tag
    assert "DIGEST must be a registry digest" in body
    # dry-run path exists and applies nothing
    assert "no patch applied" in body


def test_backend_script_asserts_the_resolved_container_before_writing():
    body = BACKEND_SCRIPT.read_text(encoding="utf-8")
    assert "이름으로 인덱스를 조회" in body or "이름 조회" in body
    # the guard added by the deploy-script-index-safety round
    assert '{"op":"test","path":"/spec/template/spec/containers/' in body
    assert '/name","value":"' in body
    # the guard is the first op in the patch body
    patch_line = next(l for l in body.splitlines() if l.startswith("patch='["))
    assert "$guard" in patch_line
    assert patch_line.index("$guard") < patch_line.index("$pre")


def test_gate_takes_the_deploy_target_as_an_argument():
    body = GATE.read_text(encoding="utf-8")
    assert '"--deploy-target"' in body
    assert 'action="append"' in body
    # no built-in exclusion list
    assert "jw-ingest-hook" not in body.split("--- U: untouched targets")[1]
    # exclusions are printed
    assert "exclusion_report(" in body
    # shared, tested parser rather than an inline one
    assert "from pipeline.scripts.deploy.untouched_baseline import" in body
    assert 'line.split("spec_sha256=")' not in body


def test_gate_still_treats_an_unusable_baseline_as_a_failure():
    body = GATE.read_text(encoding="utf-8")
    u_section = body.split("--- U: untouched targets")[1]
    assert "U0 untouched baseline usable" in u_section
    assert "record(" in u_section          # a FAIL is recorded, not a skip
    assert "return 1 if failed else 0" in body


@pytest.mark.parametrize("script", [HOOK_SCRIPT, BACKEND_SCRIPT])
def test_scripts_are_shell_syntax_clean(script):
    result = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
