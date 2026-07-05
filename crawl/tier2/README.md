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
