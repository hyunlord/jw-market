# Golden Census

Measured against base `64ce6f56242a8f924cd62028e687087a670b9c69` on
2026-07-15. The executable golden population is the explicit `registry.json`
list, not every literal in a unit test.

## Executable contracts

| Document | Contract | Independent truth basis |
| --- | --- | --- |
| `file_sql_goldens.json` | `chso_sell_out_total_2026_01` | original XLSX direct sum |
| `file_sql_goldens.json` | `chso_donga_sell_out_total_2026_01` | original XLSX exact-filter sum |
| `file_sql_goldens.json` | `chso_dongwha_sell_out_total_2026_01` | original XLSX exact-filter sum |
| `file_sql_goldens.json` | `chso_donga_dongwha_sell_out_difference_2026_01` | subtraction of two source-reproduced totals |
| `file_sql_goldens.json` | `chso_r05a0_manufacturer_sell_out_compare_2026_01` | original XLSX, 60 amount columns x 148 ATC4 census |
| `file_sql_goldens.json` | `bpi_q1_no_reference_pairs` | original XLSX exact q1 lookup |
| `market_goldens.json` | `ml_006_top5_hhi_cr5` | raw-precision independent formula reproduction |
| `relational_goldens.json` | `structured_market_precomputed_equals_realtime` | 2,445,691-cell independent-path equality census |
| `relational_goldens.json` | `p91_text_fast_path_psm3_equals_psm4` | controlled same-input PSM3/PSM4 equality |
| `performance_baselines.json` | `db501dcc_candidate_hot_tool_elapsed` | controlled candidate timing; not a live SLA |

Population is 10 and every row has the exact request, generation method,
independent truth basis, and measurement context. `registry.json` is the only
entry point; unlisted JSON documents fail the gate.

## Test and eval literals

An AST census found 65 Python test files, 3,677 `assert` nodes, 5,848 literal
constants inside assertions, and 28 files containing mock/monkeypatch symbols.
The `eval/` tree contains 10 tracked files. These are behavioral fixtures and
test inputs, not release truth contracts. Some intentionally repeat contract
values to exercise renderers and planners; repetition never promotes a fixture
to a truth basis.

The provenance gate therefore rejects `mock_fixture` truth types, test-file
evidence paths, temporary evidence paths, runtime observations, and snapshot
rehashes. This is stricter and less error-prone than treating every assertion
literal as a golden.

## Hashes and temporary paths

Tracked tests and eval files contain eight unique 64-hex values across 22
occurrences, including source identities and ownership-only JW Market API
references. Executable chat goldens are only the contracts above. The JW Market
API hashes in `README.md` are references and are not loaded by `registry.json`.

The executable golden documents contain zero `/tmp` or `/private/tmp`
references. Other test files may use temporary paths as sandbox fixtures; the
gate prohibits those paths specifically as expected-value or truth evidence.

The unreproducible p91 fixed hash is absent. Its contract is the reproducible
relation `PSM3 vs PSM4 byte-identical` under a controlled same-input run.
