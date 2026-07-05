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
