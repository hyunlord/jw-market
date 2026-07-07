# Tier2 Hybrid Label Policy

Tier2 uses two distinct provenance labels:

- `tier2_exact_rule_v1`: deterministic search/exact-rule provenance. These rows
  remain useful for audit and replay planning, but they are not visible to
  Agent2 narrative evidence.
- `tier2_llm_v1`: LLM-confirmed brand/article links. The workflow may only
  accept candidates produced by the deterministic scanner, and Agent2 includes
  only these Tier2 rows in its evidence allowlist.

This separation keeps search provenance from becoming narrative evidence until
the article-level brand relevance has been confirmed.

## GenOS workflow

- Workflow: `jw-tier2-brand-tagging`
- Workflow id: `317`
- Revision: `5366`
- Deployment: `1325`
- Serving: `163`
- Backing Flowise chat_flow id: `b7dbe513-3879-4ec8-8baa-05a2d161500c`
- Endpoint inside the cluster: `http://workflow-317.llmops.svc.cluster.local:8080/run/v2`

The workflow was registered through the DB-backed GenOS path used for wf316,
not through the Flowise HTTP API. The repository prompt, workflow revision step
prompt, and Flowise backing row prompt all have SHA256
`aab7790a4d03d05cb6147c029a7783aea744b386e2694aa50b2e5fbeb3f0c43f`.
The Flowise backing row must remain an `AGENTFLOW` row, matching the wf316
runtime template; a null `chat_flow.type` makes Flowise reject `/run/v2` calls
with an ending-node validation error.

Smoke validation covered three multi-brand articles, including the
`프랄런트`/`레파타` PCSK9 article. `tier2_llm_tagging.py` parsed all three
responses without candidate omissions, out-of-candidate brands, or missing
`include`/`relevance_score`/`reason` fields.

## Tier2 scoring workflow

- Workflow: `jw-tier2-brand-scoring`
- Workflow id: `324`
- Revision: `5490`
- Deployment: `1389`
- Serving: `163`
- Backing Flowise chat_flow id: `034e7ed0-92d3-47a1-8ff4-87207028a45d`
- Endpoint inside the cluster: `http://workflow-324.llmops.svc.cluster.local:8080/run/v2`

This workflow is the Tier2 scoring counterpart to wf196 v3. It copies the
wf196 v3 relatedness-gate and importance rubric, but scores exactly one
`target_brand` supplied by the Tier2 replay runner instead of emitting the
JW25-oriented `matches[]` contract. It also emits the wf196 news classification
contract: `tag`, `category_label`, and `category_code`. `tag` and
`category_label` must be identical, and `기타` maps to `category_code=external`.
wf196 itself remains untouched.

The repository prompt, workflow revision step prompt, and Flowise backing row
prompt all have SHA256
`4bc6bb852ed0d6bc2ef280e1a89a7a3d6f3ee3639129c526ee52abfddc97a8e1`.
The Flowise backing row is an `AGENTFLOW` row.

Runtime smoke for revision `5490` passed on 15/15 Tier2 match samples. Each
response returned valid JSON with integer `score`, valid `tag`, matching
`category_label`, and the expected `category_code` mapping. Full replay remains
gated on the separate bulk runner and event_brand_scores replacement procedure.

## Body-exact match staging

`pipeline/scripts/crawler/tier2_body_match_runner.py` is the canonical Tier2
body-exact matching runner for the post-cutover news model. It is match-only:
it writes `tier2_match_staging` and never writes `event_brand_scores`.

The runner scans the cutover `news_raw` title/body against the d2
`mart_general_brand_metric` dictionary, excluding Tier1 main and competitor
brands already represented by `workflow_196_optionB` and
`cross_match_adapter_v1`. It applies the false-positive controls validated in
the design audit:

- longest match wins for nested names, so a longer product name suppresses its
  shorter substring.
- compact names of three characters or fewer and the stoplist
  (`제로`, `케어`, `프로`, `센스`, `데일리`, `로이드`, `탑`, `트라`, `이지`,
  `파인`, `피디`, `코미`, `웰`) are blocked on body-only hits.
- Tier2 `collection_provenance` for the same brand/key exempts that ambiguity
  gate and records the source as `body+search_provenance`.

Operational shape:

```bash
DB_NAME=jw_mart_d2_stage_20260630_r2 \
D2_WRITER_USER=... D2_WRITER_PASSWORD=... \
python3 pipeline/scripts/crawler/tier2_body_match_runner.py --apply --replace-table
```

The scoring stage must consume `tier2_match_staging` and then decide how to
replace legacy `tier2_exact_rule_v1`; this matcher deliberately leaves legacy
score rows untouched.
