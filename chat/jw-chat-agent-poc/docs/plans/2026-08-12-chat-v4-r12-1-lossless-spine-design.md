# Chat V4 R12.1 Lossless Spine Design

## Goal

External record answers must preserve every received record independently of LLM
synthesis. The LLM may add commentary, but it may not select, remove, or rewrite the
deterministic fact surface.

## Boundaries

- `market_analysis` keeps the current R11b synthesis and gate path unchanged.
- The new spine applies only to record-shaped external answers:
  `clinical_portfolio`, `patent_portfolio`, `policy_document`, and
  `single_record_detail`.
- Existing seven-source planning and execution remain intact. Patent provenance is
  represented as three named sub-lanes inside the existing `patent` source.
- No database tables, source catalog descriptions, protected evidence-binding files,
  or flag-off behavior change.

## Data Contracts

`EvidenceSet` is the adapter-to-renderer contract. It contains source identity,
query manifests, retrieval timestamp, reported/received/unique/rendered counts,
pagination state, normalized records with stable public evidence IDs, failures, and
source references.

`CoverageLedger` is derived from `EvidenceSet` values and rejects impossible states
such as rendered records exceeding received records. Count differences are rendered
with an explicit reason rather than silently normalized.

`RenderedFacts` contains deterministic markdown plus render-node metadata. Metadata
records `block_id`, `record_ids`, and `surface_fields`; requested-field checks use
these nodes only and never inspect natural-language prose.

`RequestedAnswerShape` records entities, measure or attribute, horizon, and
granularity. A missing requested axis produces an additive notice before related
material; related material is explicitly marked as non-substitutive.

## ClinicalTrials Path

The existing MCP 169 schema permits only `pageSize <= 20`, while the approved
contract requires API v2 pages of 100. A V4-only official API v2 client therefore
compiles concept specs to `query.intr` or `query.cond`, requests 100 rows per page,
follows `nextPageToken`, and stops at 1,000 unique records with an explicit partial
result notice. Legacy and non-V4 MCP callers remain unchanged.

All accepted static planner query entries are retained, deduplicated by compiled
parameters, run by the existing executor, and unioned by NCT ID. The typed input
boundary rejects more than 32 ClinicalTrials queries instead of silently truncating
them. Each record preserves every matching query ID.

## Patent Path

The patent adapter returns three isolated lanes:

1. `kr_primary`: NeDrug `search_korea_drug_patent` structured records.
2. `us_secondary`: Orange Book structured records.
3. `news`: Tavily web documents with event and publication dates kept separate.

The renderer never combines Korean and US dates and uses only source-qualified
language about listed status and listed expiration dates.

## Render And Compose

For external record profiles the order is:

1. normalize source envelopes into evidence sets;
2. render the complete deterministic fact surface;
3. ask the synthesizer for commentary only;
4. compose requested-axis notices, facts, commentary, limitations, and all sources.

If synthesis times out or errors, composition retains the complete fact surface and
adds one notice that automatic commentary was not completed. Existing SSE progress
events may announce fact rendering, but persistent progressive delivery is deferred
to R12.2 because the current stream stores only the final answer.

## Requested-Field Rollout

`CHAT_V4_REQUESTED_FIELDS_MODE` accepts `shadow` and `inject`. Shadow records three
rates without changing answer bytes. Inject mode may append only missing
deterministic blocks or explicit absence rows. It cannot delete, replace, or rewrite
existing prose. Structurally impossible coverage states remain hard failures.
