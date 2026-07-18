# Golden Contract Ownership

Chat owns only chat-specific contracts, including file SQL goldens such as the
CHSO R05A0 comparison in `file_sql_goldens.json`.

Every executable golden is listed in `registry.json` and records four things:

1. the exact request or calculation,
2. the generation and canonicalization method,
3. the independent truth basis used to judge correctness, and
4. the measurement context: time, database, build, runtime, and source file.

An observed live response, a mock fixture, or a rehash of the expected snapshot
is not an independent truth basis. Temporary files are not allowed to supply an
expected value. Equality contracts are preferred to fixed hashes when the
contract is equivalence between two controlled paths, as with the p91 PSM3 and
PSM4 text fast path.

JW Market API goldens are owned by
`tests/api/API_GOLDEN_CONTRACTS.md` in the JW Market repository. Chat must not
keep duplicate API response hashes.

The PL-approved JW Market goldens are:

- `/api/brands`: `77917362f9ca356bc6a596abcb59d5b7b8e418c45ca7599d5c618852866fa6ab`
- `/api/cause/리바로`: `e7dcc96e4e5390ca55ae78aff28c04a28956e68cb098d25ac171e7878c81ec3b`

These hashes are references only. They are not chat-owned executable goldens.
`악템라` and `가드렛` do not have approved API goldens and must not be added here.
