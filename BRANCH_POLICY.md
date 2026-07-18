# Branch Policy

## Historical extraction sources

The following branches are retained for provenance only and must never be
merged into `develop`:

- `codex/crawl-2tier`
- `codex/short-long-lineage-bulk`

Both branches contain revisions that predate the current serving contracts.
Merging either branch would restore workflow revision 5365, remove Agent3
pre-I/O and idempotency gates, and replace current API contracts with stale
implementations.

Use the reviewed crawl and short/long extraction commits instead. Future
changes must preserve the current `develop` implementations under the API,
Agent3, forecast, and Agent3 manifest paths.

The crawl image must be rebuilt from the approved extraction lineage in a
separate deployment cycle. The historical branches are not valid image build
bases.

## Detached policy lineages

The lineage containing commit `3f0db0aead36604ef9ade7071ead647e4dd462a4`
must also not be merged into `develop`. It is not an ancestor of the current
policy lineage and carries the legacy event-exposure cutoff set
`43/49/51/54/55`. Treat matching numbers from that lineage as a different
definition unless ancestry and policy parity are independently demonstrated.

Keep the lineage for history only. Any useful change must be extracted onto
current `develop`, then reviewed against the active event policy and serving
contracts.

## Agent2 regenerator canon (PL decision, 2026-07-17)

The canonical agent2 regenerator is
`pipeline/scripts/ai_analysis/agent2_regen_orchestrator.py` (904-line
implementation): it alone carries the `--analysis-variant short|long`
contract matching the live wf217 short/long lineage. The recovered VM
snapshot under `pipeline/scripts/ai_analysis/ops/` is a deprecated prior
generation retained for provenance (see its README).
