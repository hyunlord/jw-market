# Post-open Gate Follow-ups

The integration branch intentionally excludes `82d7bf11` and `45c0870c`.
Both commits are based on an older lineage and restoring their deleted gate
files would require an explicit design decision.

Rebuild the useful pieces directly on the `6006e19c` lineage after opening:

1. Recreate the acceptance-format utility previously implemented in
   `release_acceptance.py`.
2. Recreate the failure-injection framework previously implemented in
   `test_gate_failure_injection.py`.
3. Classify the 331 unresolved `any()` sites.
4. Classify the 92 `if not data: return True` candidates, especially the
   evaluation gate at `scoring.py:101`.
5. Review the three `except: pass` candidates.
6. Port only the approved useful portions of `82d7bf11` and `45c0870c`.

The rejected merge attempt is preserved for reference at
`codex/chat-integration-merge-attempt-82d`
(`b99d697dd5c343ea088f88da9ca5e9c295e2c56d`). It is not an integration
candidate.
