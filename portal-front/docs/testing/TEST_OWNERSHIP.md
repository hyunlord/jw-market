# Public Test Ownership

`public` owns the complete test inventory in `tests/TEST_MANIFEST.json`. The 266 floor is 47 files, 303 named blocks, and 959 assertions; later public-only strengthening may raise those totals but must never lower them.

The following four tests are public-only assets and are not optional:

- `tests/inspectionPanelPreferences.test.ts`
- `tests/marketDocumentBlockedUpload.test.ts`
- `tests/traceToolResults.test.ts`
- `tests/uploadUnknownStateRender.test.ts`

Every test path, SHA256, block count, and assertion count is an exact contract. Adding a test requires updating the manifest. Removing a test or weakening an assertion requires an explicit behavior decision and PL approval. Counts are a lower-level signal; exact hashes prevent a weakened test from passing on count alone.
