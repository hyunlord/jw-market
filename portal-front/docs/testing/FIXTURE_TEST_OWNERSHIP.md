# Fixture Ownership Contract

The 16 files listed in `tests/fixtures/SHA256SUMS` are production-contract evidence. Every path and byte is owned by the public branch and must remain exact.

- Do not shorten, relocate, synthesize, or delete a fixture.
- A fixture change requires the fixture, its checksum, the owning test, and `tests/TEST_MANIFEST.json` to change in one reviewed commit.
- Missing, extra, or hash-mismatched fixture files fail `scripts/verify-test-contract.mjs`.
- The two README fixtures are intentional even though no test parses them directly.
- Git LFS is not part of the current repository or build contract. Do not convert these files to LFS pointers without a separate storage and checkout contract.

This contract records the 266 decision: preserve all 16 exact blobs. PL approval is required to weaken it.
