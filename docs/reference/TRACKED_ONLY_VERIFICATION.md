# Tracked-only verification

## Why this gate exists

Tests can import files that exist in a developer worktree even when Git does
not track them. In that state the local suite passes, while a clean clone is
missing runtime code or data.

This failure occurred when broad ignore rules hid 11 Python files under
`pipeline/scripts/ai_analysis/phase_zeta_runner/` and the runtime catalog input
`docs/crawl/search_keywords.json`. The local short/long suite passed against
those ignored files, but the remote commit could not reproduce that result.

## Required verification

Before a feature branch that adds or restores source is pushed:

1. Scan ignored paths with
   `git ls-files --others --ignored --exclude-standard`.
2. Classify every source-like result, including Python, SQL, YAML, and runtime
   JSON. Runtime inputs are source even when stored below `docs/`.
3. Run the relevant suite from a clean clone or detached worktree containing
   only tracked content from the candidate commit.
4. Verify the candidate commit, rather than the original dirty worktree, is the
   object being pushed.

The regression test `tests/test_gitignore_source_hygiene.py` enforces the
ignored-source scan in Git-backed test environments.

## Ignore-pattern standard

Do not add broad unanchored patterns such as `phase_*/` or `docs/*` without
proving they cannot match source packages or runtime inputs. Prefer explicit
output directories or root-anchored generated paths. When an exception is
unavoidable, re-ignore sibling content narrowly so unrelated artifacts do not
become visible.

## Lineage standard

Before citing a baseline value, verify that the commit which produced it is an
ancestor of the active `develop` lineage. A value from a detached branch can
look numerically plausible while representing a different policy definition.
Record the ancestry command and policy version alongside the value.
