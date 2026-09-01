# Public Test Floor

The deployment floor established in round 266 is:

| Gate | Required result |
|---|---:|
| Golden evidence focus | 52/52 |
| Upload focus | 57/57 |
| Full public suite on Node 22.11 | 296/299 |
| Named test blocks | 303 |
| Assertions | 959 |

The three accepted Node 22.11 loader failures are `tests/kpiNoData.test.ts`, `tests/marketFilterMultiSearchSelection.test.ts`, and `tests/moleculeStrengthDrilldown.test.ts`. They are toolchain exceptions, not product passes. New tests may raise the runtime total; they do not lower the frozen floor or permit additional failures.

Fixture and test hashes in the machine manifests remain mandatory even when all numerical floors pass.

Round 267 strengthened `tests/tooltipCopy.test.ts`; therefore the current exact manifest is above the numerical floor. The machine manifest records the current hashes and totals while this document preserves the minimum contract.
