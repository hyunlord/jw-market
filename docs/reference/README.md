# Reference Materials

This directory keeps API/design inputs that should be treated as reference-only.
Do not edit upstream reference files in place; add a dated replacement and update
this README when a newer design arrives.

## v0.9.0 (latest)

| File | Description |
|---|---|
| `JW_Market_Analysis_API_Spec_20260520.html` | v0.9.0 API specification. |
| `jw_market_hardcoded_mockup_20260520.html` | 3-page SPA mockup reference. |
| `JW_Market_v0_9_0_DataPipeline_Handoff.md` | Data pipeline handoff for replacing mock JSON with real pipeline output. |
| `deep_analysis_가드메트_reference.json` | Mock response reference for `/api/deep-analysis/{brand_name}`. |

Core finding:

- GKE already has the v0.9.0 mock deployed.
- External URL: `https://jwai-dev.jwhealthcare.com/jw-market-analysis/`
- Image: `asia-northeast3-docker.pkg.dev/prj-jw-agn-stg-ai/ar-jw-agn-stg-genos-dev-01/jw-market-analysis:v0.9.0`
- Deployment: `jw-market-api` in namespace `llmops`
- Current behavior: mock JSON responses with 100% spec-response consistency.
- This repository's mission: replace the mock responses with real data output.

Next decision gate:

- Option A: use the deployed `jw-market` mock code as the base and replace data.
- Option B: extend the existing 16-F-2 FastAPI implementation to the v0.9.0 schema.
- Option C: inspect the mock code first, then choose A or B.

Potential downstream phases:

- Phase 16-G-4: v0.9.0 schema-aligned API replacement.
- Phase 16-I: forecast model.
- Phase 16-J: simulation.
- Phase 16-K: events crawling.
- Phase 16-L: `ai_analysis` LLM generation.

## v.20260519 (previous)

| File | Description |
|---|---|
| `JW_Market_Analysis_API_Spec_20260519.html` | Previous API specification retained for diff/comparison. |
| `jw_market_demo_integrated_20260519.html` | Previous integrated demo/mockup retained for comparison. |

Major v0.9.0 differences from the 20260519 design:

- `market_id` format shifts from `ml_006` style IDs to `strategy_006` style IDs.
- `view` is now a market-dimension toggle (`market_landscape` vs `competitive_dynamics`) rather than only an analysis emphasis.
- `/api/cause/{brand}` payload is expanded from simple metrics to chart components such as `ei_ms_matrix`, `growth_contribution_ms_matrix`, `analysis_levels`, and `kpi`.
- `/api/deep-analysis/{brand}` becomes the main v0.9.0 pipeline output surface: forecast, simulation, events, and AI analysis.

## Handling Rules

- Treat these files as immutable source references.
- Prefer the latest dated spec unless a task explicitly asks for the older version.
- Do not start Phase 16-G-4 implementation from these references until PL chooses Option A/B/C.
