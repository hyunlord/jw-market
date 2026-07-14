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

- Workflow: `jw-tier2-brand-tagging-ga`
- Workflow id: `334`
- Revision: `5668`
- Deployment: `1450`
- Serving: `163`
- Backing Flowise chat_flow id: `57de4034-b136-4afd-a460-b67bd79ed5df`
- Endpoint inside the cluster: `http://workflow-334.llmops.svc.cluster.local:8080/run/v2`

The workflow was rebuilt through the GenOS workflow-copy and revision APIs so
its resource metadata is managed by the platform. The repository prompt,
workflow revision step prompt, and Flowise backing row prompt all have SHA256
`aab7790a4d03d05cb6147c029a7783aea744b386e2694aa50b2e5fbeb3f0c43f`.
The Flowise backing row must remain an `AGENTFLOW` row, matching the wf316
runtime template; a null `chat_flow.type` makes Flowise reject `/run/v2` calls
with an ending-node validation error.

Smoke validation covered three multi-brand articles, including the
`프랄런트`/`레파타` PCSK9 article. `tier2_llm_tagging.py` parsed all three
responses without candidate omissions, out-of-candidate brands, or missing
`include`/`relevance_score`/`reason` fields.

## Tier2 scoring workflow

- Workflow: `jw-tier2-brand-scoring-ga`
- Workflow id: `337`
- Revision: `5671`
- Deployment: `1453`
- Serving: `163`
- Backing Flowise chat_flow id: `6f040141-56aa-4f29-948f-d64e223fb9c0`
- Endpoint inside the cluster: `http://workflow-337.llmops.svc.cluster.local:8080/run/v2`

This workflow is the Tier2 scoring counterpart to wf196 v3. It copies the
wf196 v3 relatedness-gate and importance rubric, but scores the
`target_brands[]` supplied by the Tier2 replay runner in one news-level call
instead of emitting the JW25-oriented `matches[]` contract. It emits one
wf196-compatible news classification (`tag`, `category_label`,
`category_code`) and one `brand_scores[]` item per input brand. `tag` and
`category_label` must be identical, and `기타` maps to
`category_code=external`. wf196 itself remains untouched.

The repository prompt, workflow revision step prompt, and Flowise backing row
prompt all have SHA256
`15074bf21e1e919d296fcc1da65f7f1f6a98b18a2062bb55981ea2fcf50050a3`.
The Flowise backing row is an `AGENTFLOW` row.

The pre-migration runtime smoke for revision `5496` passed on 15/15 Tier2 match samples, including
12 multi-brand news samples. Each response returned valid JSON with a valid
news-level `tag`, matching `category_label`, expected `category_code`, and a
`brand_scores[]` brand-key set exactly equal to the input `target_brands[]`
brand-key set. Full replay remains gated on the separate bulk runner and
event_brand_scores replacement procedure.

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
