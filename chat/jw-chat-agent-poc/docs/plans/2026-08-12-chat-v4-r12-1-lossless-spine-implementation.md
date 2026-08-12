# Chat V4 R12.1 Implementation Plan

1. Add failing contract tests for ClinicalTrials concept compilation, pagination,
   full static query retention, NCT union, and complete normalization.
2. Implement the V4-only official API v2 client and planner-side concept/request
   metadata while retaining legacy MCP behavior.
3. Add failing tests for patent lane isolation and implement NeDrug, Orange Book,
   and Tavily lane assembly inside the existing patent source.
4. Add failing contract tests for evidence counts, deterministic profile rendering,
   synthesis-timeout retention, full source references, and requested-field nodes.
5. Implement the lossless spine and route only non-market external record profiles
   through render-before-synthesis composition.
6. Add request-satisfaction regressions for API price, ten-year history, and active
   Korean trials, plus AS_OF_DATE and market-path invariance coverage.
7. Run focused tests, V4/mart regression sets, protected-file hashes, flag-off byte
   checks, and the full suite against the recorded baseline node-ID set.
8. Secret-scan and push the branch. Deploy to DEV in shadow mode with a CAS guard,
   collect metrics, then activate inject-only only when additive invariants hold.
9. Run the approved live gate, promote `feature/chat` by fast-forward on HTTP 500=0
   and flag-off invariance, then build and self-test the evidence archive.
