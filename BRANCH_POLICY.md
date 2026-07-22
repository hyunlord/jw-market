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

---

## Canonical s2 catalog input files — DO NOT DELETE (PL decision, 2026-07-22)

Two files are **canonical, git-tracked inputs** required to rebuild the serving
catalog through `s2_catalog`. They are NOT generated data — the s2 chain reads
them via hardcoded paths with **no object-storage fallback** (unlike MI Master):

- `inputs/molecule_v4_worklist.csv`
  — read by `pipeline/etl/io/catalog/postfix/catalog_postfix.py:run_postfix`
    (`--inputs-dir`). Raises `FileNotFoundError` if missing.
- `data/cache/prototype_11_step_c4_target_priority_precompute_sample.csv`
  — read by `pipeline/etl/io/catalog/target/records.py:read_required_csv`
    via `run_target_priority` (`--cache-dir`). Raises if missing.

Both were deleted in `bb9f3d63` ("reset to s1 ingest base") and **intentionally
restored to git** on 2026-07-22 so a fresh clone / the R-1 `full_rehearsal` image
can rerun the catalog build without out-of-band provisioning. `.gitignore` carries
negation exceptions for exactly these two paths; the
`deploy/docker/pipeline-orchestrator.Dockerfile` COPYs them into the image at
`/app/data/cache` and `/app/inputs`; and `full_rehearsal.build_full_rehearsal_plan`
passes `--cache-dir`/`--inputs-dir` at those locations.

**Do not re-ignore, delete, or "clean up" these files.** Removing either one
re-opens the R-1 catalog-build blocker. Confirmed provenance: serving r2 catalog
(`catalog_manifest_hash` single-valued, MI Master 2026.05.18) was produced by this
same s2 path — see audit `R1_canonical_path_audit_20260722`.

---

## ⚠️ Deploy warning — response-keys-4 (1d38478e) NOT in production (rolled back 2026-07-22)

`develop` contains the response-keys-4 change (commits `9b5fa13d`/`e448d3b2`/`1d38478e`):
market-status `ubist_recent`/`iqvia_recent` and cause `data.kpi` `target_brand_sales` +
mutually-exclusive `market_cagr_5y_pct`/`market_cagr_3y_pct`.

**It was promoted to prod and rolled back the same day.** Production runs the previous
image `sha256:62d9152a…` (APP_VERSION `74f0d1d7`), which keeps the D-1 series=7 fix.

- **Root cause**: exclusive CAGR makes `market_cagr_5y_pct` **null** for IQVIA markets
  (market series starts 2021-Q2, so the 2021-Q1 5-year endpoint is absent). The portal
  calls `market_cagr_5y_pct.toFixed()` without a null guard → `null.toFixed()` →
  원인분석 ErrorBoundary. Previously the 5y slot always had a value (silent 5y→3y fallback).
- The CAGR logic is correct and market-level (not brand-launch-based); no code defect.
- ★ **Do NOT build the backend image from `develop` tip and promote it** until the portal
  null-guards `market_cagr_5y_pct` and reads `market_cagr_3y_pct` for the 3-year case.
- ★ **Do NOT revert the code** — it is the asset to re-promote after the portal fix.

See the rollback audit (stage_g rollback, 2026-07-22) for full evidence.
