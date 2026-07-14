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
