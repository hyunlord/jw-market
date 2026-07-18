# R-1 UBIST sidecar input plan

## Context

The canonical full-input census currently materializes UBIST XLSX objects from
MinIO.  The live May 2026 restoration instead came from a SHA-pinned parquet
file on a read-only PVC, so a full rehearsal silently omits that month.

## Contract

1. Accept an explicit parquet sidecar source, destination partition, and SHA256
   when materializing full inputs.
2. Copy the sidecar into the isolated input root and record it in both the input
   manifest and census inventory.
3. Install verified sidecars into the isolated UBIST parquet work tree after the
   replace load and before any catalog or mart stage.
4. Fail closed on a missing source, SHA mismatch, escaping destination, duplicate
   destination, or overwrite attempt.
5. Keep shared MinIO, the PVC source, and operating schemas read-only.

## Verification

- Focused unit tests for materialization, manifest parsing, plan ordering, and
  negative controls.
- Related orchestrator test suite and the repository baseline suite.
- Exact-SHA image build, then a corrected isolated R-1 Job with the PVC mounted
  read-only.
- Twenty-table comparison against the operating reference before R-2 starts.
