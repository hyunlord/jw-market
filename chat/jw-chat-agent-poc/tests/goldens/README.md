# Golden Contract Ownership

Chat owns only chat-specific contracts, including file SQL goldens such as the
CHSO R05A0 comparison in `file_sql_goldens.json`.

JW Market API goldens are owned by
`tests/api/API_GOLDEN_CONTRACTS.md` in the JW Market repository. Chat must not
keep duplicate API response hashes.

The PL-approved JW Market goldens are:

- `/api/brands`: `77917362f9ca356bc6a596abcb59d5b7b8e418c45ca7599d5c618852866fa6ab`
- `/api/cause/리바로`: `e7dcc96e4e5390ca55ae78aff28c04a28956e68cb098d25ac171e7878c81ec3b`

These hashes are references only. They are not chat-owned executable goldens.
