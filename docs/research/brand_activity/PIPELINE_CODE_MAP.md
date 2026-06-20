# Brand Activity Topic Pipeline Code Map

Authoritative map for the Brand Activity auto-topic pipeline after the
`serving_direct_singleconcept_top7_exec_20260620_143124` cleanup.

## Current Contract

- LLM route: serving-direct only.
- Retained serving path: Mac `127.0.0.1:19080` -> SSH 2-hop -> GCP node `kubectl port-forward svc/vertex-openai-proxy-service:8080` -> OpenAI-compatible `/v1/chat/completions`.
- Removed/unsupported paths in this package: `jwai-dev` gateway, Vertex direct, Vertex node-token direct.
- Source DB: local MariaDB `jw_brand_activity_stage` on `127.0.0.1:3308`; topic extraction uses Keyword rows only.
- API result tables: `jw_brand_activity_stage.mart_brand_activity_topics` and `jw_brand_activity_stage.mart_brand_activity_topic_runs`.
- Current measured run loaded to DB: `serving_direct_singleconcept_top7_exec_20260620_143124`.

## Auto Topic Files

| Path | Role |
|---|---|
| `pipeline/scripts/analysis/brand_activity/auto_topic/run_auto_topic.py` | CLI entrypoint; loads stage data, builds scopes/samples, executes calls, writes reports/audit, optionally stores DB results. |
| `pipeline/scripts/analysis/brand_activity/auto_topic/save_topic_results.py` | CLI for storing an existing measured audit run without LLM calls. |
| `pipeline/scripts/analysis/brand_activity/auto_topic/llm.py` | Direct-serving OpenAI-compatible HTTP client, retry, pacing, timeout, watchdog, call-log serialization. |
| `pipeline/scripts/analysis/brand_activity/auto_topic/execution.py` | Market-axis map-reduce, brand-share batching, tier recheck, stability repeat, batch aggregation. |
| `pipeline/scripts/analysis/brand_activity/auto_topic/response.py` | Model JSON normalization, topic cap, share normalization, label-to-id backfill, brand-specific dedup. |
| `pipeline/scripts/analysis/brand_activity/auto_topic/prompts.py` | Prompt templates and prompt-template manifest; enforces single-concept labels and distinct brand-specific topics. |
| `pipeline/scripts/analysis/brand_activity/auto_topic/quality.py` | Mechanical guard, drift check, dictionary cross-check, market grades. |
| `pipeline/scripts/analysis/brand_activity/auto_topic/label_rules.py` | Label normalization, compound-label detection, brand-specific near-duplicate checks. |
| `pipeline/scripts/analysis/brand_activity/auto_topic/chunking.py` | Token-budget row chunking and chunk summaries. |
| `pipeline/scripts/analysis/brand_activity/auto_topic/sampling.py` | Market/brand sample selection, full-row mode, top-N brands. |
| `pipeline/scripts/analysis/brand_activity/auto_topic/market_scope.py` | CSD-backed ATC4 universe for the 11 final markets. |
| `pipeline/scripts/analysis/brand_activity/auto_topic/market_groups.py` | MI Master membership, CSD English names, final market scopes, filter metadata. |
| `pipeline/scripts/analysis/brand_activity/auto_topic/data_source.py` | Local MariaDB reads, CSD market bridge, alias metadata, DB snapshots. |
| `pipeline/scripts/analysis/brand_activity/auto_topic/dictionary.py` | REDESIGN dictionary baseline calculations. |
| `pipeline/scripts/analysis/brand_activity/auto_topic/stability.py` | Axis similarity and keep/update decision helpers. |
| `pipeline/scripts/analysis/brand_activity/auto_topic/cache.py` | Input-hash cache keys for deterministic reruns. |
| `pipeline/scripts/analysis/brand_activity/auto_topic/privacy.py` | Token estimation and source-text redaction hashes. |
| `pipeline/scripts/analysis/brand_activity/auto_topic/quarantine.py` | Quarantine payload builders for failed calls. |
| `pipeline/scripts/analysis/brand_activity/auto_topic/qc_probe.py` | Artificial QC anomaly payloads for guard evidence. |
| `pipeline/scripts/analysis/brand_activity/auto_topic/reports.py` | Markdown report rendering. |
| `pipeline/scripts/analysis/brand_activity/auto_topic/viz.py` | Measured-only static HTML payload and renderer. |
| `pipeline/scripts/analysis/brand_activity/auto_topic/audit.py` | Manifests, raw-text leak scan, zip/backup packaging. |
| `pipeline/scripts/analysis/brand_activity/auto_topic/static_quality.py` | Docstring/dead-code/rationale static gate. |
| `pipeline/scripts/analysis/brand_activity/auto_topic/topic_store.py` | Builds API-ready market payload records and run metadata from measured audit artifacts. |
| `pipeline/scripts/analysis/brand_activity/auto_topic/topic_store_db.py` | DDL and idempotent MariaDB upsert adapter for API topic tables. |
| `pipeline/scripts/analysis/brand_activity/auto_topic/models.py` | Frozen dataclasses and JSON alias used across the pipeline. |

## Brand Activity ETL Files

| Path | Role |
|---|---|
| `pipeline/scripts/etl/brand_activity/load_raw_staging.py` | Local-only raw staging CLI; discovers CSD/Keyword source workbooks, profiles coverage, loads raw/stage tables idempotently. |
| `pipeline/scripts/etl/brand_activity/raw_db.py` | MariaDB raw/stage adapter for CSD and Keyword source rows. |
| `pipeline/scripts/etl/brand_activity/raw_schema.py` | Raw staging DDL. |
| `pipeline/scripts/etl/brand_activity/raw_extract.py` | CSD workbook extraction and source-root resolution. |
| `pipeline/scripts/etl/brand_activity/raw_source_sets.py` | Old/new source discovery and market coverage summaries. |
| `pipeline/scripts/etl/brand_activity/raw_stage_refresh.py` | Derived stage-table refresh from raw rows within the analysis window. |
| `pipeline/scripts/etl/brand_activity/raw_staging.py` | Dedup-key and recent-window helpers. |
| `pipeline/scripts/etl/brand_activity/csd_core.py` | CSD row normalization and natural-grain dedup helpers. |
| `pipeline/scripts/etl/brand_activity/csd_validation.py` | CSD source validation helpers. |
| `pipeline/scripts/etl/brand_activity/ingest_csd.py` | CSD workbook-to-stage reader. |
| `pipeline/scripts/etl/brand_activity/ingest_keyword.py` | Keyword workbook event reader. |
| `pipeline/scripts/etl/brand_activity/ingest_keyword_stage.py` | Keyword stage DDL builder. |
| `pipeline/scripts/etl/brand_activity/km_core.py` | Keyword models, parsing, hashes, JSON helpers. |
| `pipeline/scripts/etl/brand_activity/km_message_count.py` | Keyword Message Count sheet parser. |
| `pipeline/scripts/etl/brand_activity/km_validation.py` | Keyword source validation helpers. |

## Execution Flow

1. `run_auto_topic.py` refuses `main` and any schema outside `jw_brand_activity_stage`.
2. `data_source.py` reads Keyword rows, CSD market bridge, DB fingerprints, and alias metadata.
3. `market_scope.py` and `market_groups.py` build the final 11 market scopes from MI Master membership and CSD English names.
4. `sampling.py` selects full-row market axes and top-N brand rows; `chunking.py` bounds each prompt/batch.
5. `execution.py` calls `llm.py` for axis chunks, axis merge, brand batches, stability repeats, and large-market tier checks.
6. `response.py` normalizes model JSON, caps axes at 7 topics, computes `etc_pct = 100 - topic_sum`, backfills missing `topic_id` from labels, and deduplicates brand-specific near-duplicates.
7. `quality.py` and `label_rules.py` produce market grades and label-quality evidence.
8. `reports.py`, `viz.py`, and `audit.py` write measured docs, HTML, sanitized JSON, manifests, leak scans, and zip packages.
9. `topic_store.py` and `topic_store_db.py` store the measured primary payload in API tables.

## Serving-Direct Access

1. SSH hop 1: `kube@192.168.81.177` using the PL-provided bastion password via `SSHPASS`.
2. SSH hop 2: `GCP@34.47.113.232` using `~/.ssh/gcp_id_ed25519`.
3. On the GCP node, use read-only Kubernetes discovery plus `kubectl port-forward` to expose `svc/vertex-openai-proxy-service:8080`.
4. Forward to Mac `127.0.0.1:19080`.
5. Set `GENOS_DIRECT_BASE_URL=http://127.0.0.1:19080` and pass the transient bearer token via `GENOS_BEARER_TOKEN`.
6. Calls use OpenAI-compatible `POST /v1/chat/completions` with model aliases `genos-flash`, `genos-pro`, and `genos-lite`.
7. Token values, raw prompts, and raw model outputs are never stored in docs or audit artifacts.

## Topic Logic Rules

- Market axes use at most 7 concise single-concept topics.
- Labels must not combine multiple concepts with `및`, `/`, or `,`.
- Brand results use market-axis shares plus at most 2 distinct brand-specific topics.
- `etc_pct` is computed after parsing as `100 - (axis topic shares + brand-specific shares)`.
- Missing axis `topic_id` values are backfilled only when normalized labels exactly match the market axis label.
- Unknown unmatched topic ids still fail the mechanical guard.
- Shares plus brand-specific topics plus `etc_pct` must equal 100%.
- Primary API storage excludes `:pro` and `:lite` recheck variants.

## Final Market Scopes

| Scope | Display name | ATC4 values | Rule |
|---|---|---|---|
| `atc4:A02B2` | PPI Market | A02B2 | MI Master single market, CSD named. |
| `atc4:A03F0` | GANAKHAN Market | A03F0 | CSD-backed standalone fallback. |
| `atc4:B03A1` | FERINJECT Market | B03A1 | MI Master single market, CSD named. |
| `atc4:C11A1` | LIVALO V Market | C11A1 | MI Master single market, CSD named. |
| `atc4:G04C2` | TURUPAS Market | G04C2 | MI Master single market, CSD named. |
| `atc4:K01A3` | PLAJU OP Market | K01A3 | MI Master single market, CSD named. |
| `atc4:K01D2` | WINUF Market | K01D2 | MI Master single market, CSD named. |
| `atc4:V03G2` | FOSRENOL Market | V03G2 | CSD-backed standalone fallback. |
| `atc4:V06D0` | ENCOVER Market | V06D0 | MI Master single market, CSD named. |
| `group:gardlet_family` | GUARDLET Market | A10N1, A10N3 | MI Master grouped market. |
| `group:livalo_family` | LIVALO+LIVALOZET Market | C10A1, C10C0 | MI Master grouped market. |

## API Tables

```sql
CREATE TABLE IF NOT EXISTS jw_brand_activity_stage.mart_brand_activity_topics (
  scope_id VARCHAR(128) NOT NULL PRIMARY KEY,
  display_name VARCHAR(255) NOT NULL,
  atc4_values JSON NOT NULL,
  quality_grade VARCHAR(8) NOT NULL,
  source_row_count INT NOT NULL,
  payload JSON NOT NULL,
  run_id VARCHAR(160) NOT NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_mart_brand_activity_topics_run_id (run_id)
);

CREATE TABLE IF NOT EXISTS jw_brand_activity_stage.mart_brand_activity_topic_runs (
  run_id VARCHAR(160) NOT NULL PRIMARY KEY,
  created_at DATETIME NOT NULL,
  model_id VARCHAR(128) NOT NULL,
  serving_id VARCHAR(32) NOT NULL,
  route VARCHAR(64) NOT NULL,
  total_prompt_tokens BIGINT NOT NULL,
  total_completion_tokens BIGINT NOT NULL,
  est_cost_usd DECIMAL(12,4) NOT NULL,
  market_count INT NOT NULL,
  brand_count INT NOT NULL,
  axis_compound_count INT NOT NULL,
  brand_specific_dup_count INT NOT NULL,
  sha256 CHAR(64) NOT NULL,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
```

## Latest DB Load Evidence

- Stored run: `serving_direct_singleconcept_top7_exec_20260620_143124`.
- Stored SHA: `ede1a742e4f700db00e6e091528bcb7926435bf6d4207d0a1b1afec35d0567f7`.
- Stored market rows: 11.
- Stored brand payload count: 56.
- Stored run rows for this run id: 1.
- Quality in stored metadata: A 11 / B 0 / C 0 / D 0, compound labels 0, brand-specific duplicate pairs 0.

## Reproduction Runbook

This is the authoritative order for rebuilding the Brand Activity data layer from local source files. Steps 1, 2, and 4 are deterministic local DB operations. Step 3 is the only LLM step and requires serving-direct infrastructure.

### 1. Source Files

- New CSD/Keyword root: `data/IQVIA/CSD`
- Legacy Keyword root: `data/IQVIA/CSD2`
- Current deterministic source set:
  - CSD: `ChannelDynamics_JW Pharma Regional Report_Dec.23.xlsx`, `Dec.24.xlsx`, `Dec.25.xlsx`
  - Keyword new: `Keywords for JW Dec. 23.xlsx`, `Dec. 24.xlsx`, `Dec. 25.xlsx`, `Apr. 26.xlsx`
  - Keyword legacy: `Keywords for JW Jan. 25.xlsx`, `Feb. 25.xlsx`, `Mar. 25.xlsx`, `Apr. 25.xlsx`, `May. 25.xlsx`, `June. 25.xlsx`, `July. 25.xlsx`, `Aug. 25.xlsx`, `Sep. 25.xlsx`, `Oct. 25.xlsx`
- Meeting workbooks under the same source roots are preserved on disk but not loaded by this keyword-only topic pipeline; they can be reintroduced by a future ETL change if PL requests meeting analysis again.

### 2. ETL Raw + Stage Load

Production isolated staging load:

```bash
uv run --script pipeline/scripts/etl/brand_activity/load_raw_staging.py \
  --execute \
  --repeat 2 \
  --audit-dir audit/brand_activity_raw_staging
```

Repro-only scratch proof, without touching real raw/stage schemas:

```bash
SCRATCH_SCHEMA="jw_brand_activity_repro_$(date +%Y%m%d_%H%M%S)"
uv run --script pipeline/scripts/etl/brand_activity/load_raw_staging.py \
  --execute \
  --repeat 2 \
  --raw-schema "$SCRATCH_SCHEMA" \
  --stage-schema "$SCRATCH_SCHEMA" \
  --audit-dir "audit/brand_activity_repro/$SCRATCH_SCHEMA"
```

The second pass must insert 0 raw rows. Current measured counts for the 2023-05..2026-04 window:

| Table | Rows |
|---|---:|
| `raw_csd_channel_dynamics` | 187847 |
| `raw_keyword_events` | 29346 |
| `csd_channel_dynamics_stage` | 44025 |
| `km_keyword_event_stage` | 29346 |

### 3. Topic Extraction

Do not run this step during ETL/store reproducibility checks. It is the only non-deterministic/cost-bearing step and requires serving-direct access:

```bash
# Terminal/session 1: SSH 2-hop to the GCP node, then:
kubectl -n llmops port-forward svc/vertex-openai-proxy-service 19080:8080

# Terminal/session 2: run extraction only when serving-direct base URL and bearer token are present.
export GENOS_DIRECT_BASE_URL="http://127.0.0.1:19080"
export GENOS_BEARER_TOKEN="<transient bearer token>"
uv run --script pipeline/scripts/analysis/brand_activity/auto_topic/run_auto_topic.py \
  --execute \
  --tag "<new_run_tag>" \
  --max-real-calls 600
```

Tokens and raw prompts are never committed or written to audit artifacts.

### 4. DB Store From Existing Topic Run

For normal reproducibility checks, reuse the measured run output and do not call the LLM:

```bash
uv run --script pipeline/scripts/analysis/brand_activity/auto_topic/save_topic_results.py \
  --audit-dir docs/research/brand_activity/auto_topic/audit/serving_direct_singleconcept_top7_exec_20260620_143124 \
  --artifact-sha256 ede1a742e4f700db00e6e091528bcb7926435bf6d4207d0a1b1afec35d0567f7
```

This command upserts exactly 11 market payload rows into `mart_brand_activity_topics` and 1 run row into `mart_brand_activity_topic_runs`.
