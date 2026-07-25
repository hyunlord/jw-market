# HIRA Benefit Temporal Implementation Plan

1. Lock parser, change-detection, gate, alert, schema, and cleanup behavior with
   unit tests.
2. Implement the HIRA-only domain package with no news crawler edits.
3. Add a four-stage Temporal workflow and worker definition without schedule
   registration or deployment manifests.
4. Validate the DDL in dry-run mode only.
5. Prepare the workflow in a Temporal SDK 1.30.0 sandbox.
6. Run focused and repository regression tests.
7. Commit and push only the feature branch.
8. Package source, evidence, and reports in one verified `/tmp` archive.

Deployment remains a separate round after the 2026-07-26 news runtime gate.
