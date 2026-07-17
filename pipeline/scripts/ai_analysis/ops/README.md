# Agent2 operational snapshots

`agent2_regen_orchestrator_vm_snapshot.py` and its test are the byte-exact VM
artifacts recovered during the pipeline canonicalization audit. The actively
maintained implementation is supplied by the crawl/short-long branch lineage;
this snapshot exists to make the former VM-only execution reproducible and is
not a scheduled production entry point.


## DEPRECATED (PL decision, 2026-07-17 — audit 6db86a11)

The canonical agent2 regenerator is
`pipeline/scripts/ai_analysis/agent2_regen_orchestrator.py` (the 904-line
implementation). Adoption rationale: only that implementation carries the
`--analysis-variant short|long` contract (32 references) that produced the
live short/long lineage (wf217 rev3727); this VM snapshot predates the
variant concept (0 references) and shows no execution traces after
2026-06-08.

This snapshot is a PRIOR GENERATION kept for provenance only. Do not extend
it, do not schedule it, and do not edit its bytes — the sha256 pins in
`tests/deploy/test_pipeline_canonical_artifacts.py` deliberately freeze the
captured bytes, which is why this notice lives in the README instead of the
file itself. Deletion requires a separate PL decision.
