# Phase 0B characterization fixtures

These fixtures preserve observed behavior for refactoring checks. They are not
correctness assertions: known defects in the captured answers are intentionally
retained.

## Fixture set

- `corpus.v1.json`: deduplicated index over the 102-question R8 export, the
  69-question R3 gate, and ten named deployment cases.
- `observed_snapshots.v1.json`: one path-and-value snapshot per source case.
- `external_calls.v1.json`: exact request/response cassettes for GenOS, MCP,
  web search, news search, and workflow 301 file search.
- `representative_snapshot.v1.json`: compact expected snapshot used by the
  mutation checks.

The replay key is `(dependency, operation, request_sha256)`. A missing key raises
`MissingCassetteError`; replay never falls through to a live dependency.

Historical exports did not retain every tool argument or every individual
`EvidenceFact`. Such fields are marked `not_recorded_in_source_capture` instead
of being reconstructed. The exact request arguments are available for the
recorded external calls in `external_calls.v1.json`.

Run the safety net with:

```bash
PYTHONPATH=. python3 -m pytest -q tests/characterization/test_phase0b_contract.py
```
