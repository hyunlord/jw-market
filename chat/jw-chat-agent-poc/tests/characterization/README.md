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
- `routing_inputs.v2.json`: test-only raw input capture for the four routing
  points. It preserves `corpus.v1.json` unchanged and records `captured`,
  `unfired`, and `missing_input` separately. Missing historical conversation
  and planner inputs are never reconstructed with defaults.

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

Regenerate and verify the routing-input fixture without live dependencies:

```bash
PYTHONPATH=.:tests python3 -m scripts.phase5a_routing_input_capture
PYTHONPATH=.:tests python3 -m pytest -q tests/characterization/test_phase5a_corpus_capture.py
```

The generator validates the existing 25-entry exact-match cassette, blocks
network sockets, uses the fixture agent for service routing, and records zero
live chat calls and zero database writes. Planner inputs absent from the source
captures remain `missing_input`; points that the recorded path did not reach
remain `unfired`.
