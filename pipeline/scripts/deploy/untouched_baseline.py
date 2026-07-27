"""Partition an untouched-spec baseline into "must not change" and "is the target".

Why this module exists
----------------------
The 2026-07-27 cache_cause deploy gate carried ``deploy/jw-ingest-hook`` inside its
untouched-spec baseline and had no way to exclude it. That deploy's whole purpose was to
change that Deployment, so the gate went from 26/26 PASS before the deploy to 25 PASS /
1 FAIL after it, every time, by construction.

That is the mirror image of a gate that passes without checking: a gate which always
fails gets ignored, and once it is ignored a real failure rides along unnoticed. So the
deploy target is excluded — but only when the caller names it, and never silently:

  * targets are passed in explicitly; there is no built-in exclusion list, because a
    hardcoded list is wrong again on the next deploy;
  * a named target that is NOT in the baseline is an error, not a no-op — otherwise a
    typo would silently exclude nothing while reading as if it had;
  * excluded refs are returned so the caller can print them. "What was not measured, and
    why" belongs in the output, not in a comment.

Excluding the target does not loosen the judgment. The target's own spec is measured by
the reference-point checks (container image and INGEST_JOB_IMAGE env must equal the
deployed digest), which is a stricter statement than "its spec hash did not change".
Everything else in the baseline is still compared byte-for-byte.
"""
from __future__ import annotations

SHA_MARKER = "spec_sha256="


class BaselineError(RuntimeError):
    """The baseline could not be used as written."""


def parse_baseline(text: str) -> dict[str, str]:
    """Parse ``<ref>\\tspec_sha256=<sha>[\\t...]`` lines into ``{ref: sha}``.

    Lines are stripped before parsing. The baseline's ``deploy/*`` rows carry no trailing
    tab, so a parser that splits on tab without stripping first leaves the newline glued
    to the sha and every deploy row reads as changed. That mistake was made once against
    live output on 2026-07-27; it is fixed here in one place with a test.
    """
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or SHA_MARKER not in line:
            continue
        ref = line.split("\t")[0].strip()
        sha = line.split(SHA_MARKER, 1)[1].split("\t")[0].strip()
        if not ref:
            raise BaselineError(f"baseline line has no ref: {raw!r}")
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise BaselineError(f"baseline sha for {ref!r} is not a sha256 hex digest: {sha!r}")
        if ref in out and out[ref] != sha:
            raise BaselineError(
                f"baseline lists {ref!r} twice with different digests: {out[ref]} vs {sha}"
            )
        out[ref] = sha
    if not out:
        raise BaselineError("baseline contains no spec_sha256 entries")
    return out


def partition(
    baseline: dict[str, str], deploy_targets: list[str] | None = None
) -> tuple[dict[str, str], dict[str, str]]:
    """Split ``baseline`` into (checked, excluded) by the named deploy targets.

    Raises if a named target is absent from the baseline: silently excluding nothing
    would let a typo read as a successful exclusion.
    """
    targets = list(deploy_targets or [])
    missing = [t for t in targets if t not in baseline]
    if missing:
        raise BaselineError(
            f"deploy target(s) {missing} are not in the baseline "
            f"({sorted(baseline)}); refusing to exclude a ref that is not there"
        )
    excluded = {ref: sha for ref, sha in baseline.items() if ref in targets}
    checked = {ref: sha for ref, sha in baseline.items() if ref not in targets}
    if not checked:
        raise BaselineError(
            "excluding the deploy targets would leave nothing to check; "
            "that is not a gate"
        )
    return checked, excluded


def exclusion_report(excluded: dict[str, str], *, measured_by: str) -> list[str]:
    """Lines naming every excluded ref and what measures it instead."""
    if not excluded:
        return ["untouched-set exclusions: none"]
    lines = [f"untouched-set exclusions: {len(excluded)} (deploy target(s), NOT unmeasured)"]
    for ref in sorted(excluded):
        lines.append(f"  EXCLUDED {ref}")
        lines.append(f"           baseline spec_sha256={excluded[ref]}")
        lines.append(f"           expected to change; measured instead by: {measured_by}")
    return lines
