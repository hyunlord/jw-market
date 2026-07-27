# Catalog provisioning

S2 builds generated parquet under `parquet/` and publishes the complete set to
`output/catalog`. The published tree contains `CATALOG_MANIFEST.json`; every
artifact entry records its relative path, row count, byte size, and SHA256.
Consumers must validate the manifest before reading catalog parquet.

The catalog remains generated data and is intentionally excluded by
`.gitignore`. To reproduce a runtime tree from a pinned local or mounted storage
snapshot:

```bash
python3 -m pipeline.scripts.etl.materialize_catalog \
  --backend local \
  --source-root /storage/catalog/<snapshot> \
  --destination-root /app/output/catalog
```

For MinIO, credentials use the existing `MINIO_*` environment contract:

```bash
python3 -m pipeline.scripts.etl.materialize_catalog \
  --backend minio \
  --bucket jw-market-raw \
  --prefix catalog/<snapshot> \
  --destination-root /app/output/catalog
```

Materialization downloads or copies into a sibling scratch directory, validates
all manifest entries, and renames the verified tree into place. An existing
different destination is rejected. A repeated request for the same manifest is
an idempotent no-op.

Catalog parquet contains `ingested_at`. A byte-reproducible rebuild therefore
requires callers to pass the same explicit `--ingested-at`; leaving it unset
uses wall-clock time. Checksummed materialization preserves the exact published
snapshot even when a later rebuild would have a different timestamp.

Runtime S4 requires `strategic_brand` and `strategic_product`. Strategic reload
publication also requires `ml_market`. Missing roots, missing files, malformed
manifests, and checksum mismatches are distinct fail-closed errors.

Ingest Jobs materialize immediately before S4 when both runtime selectors are
set:

- `INGEST_CATALOG_BUCKET`: immutable snapshot bucket
- `INGEST_CATALOG_PREFIX`: immutable snapshot prefix

The trigger passes both selectors and the hook-owned read-only `MINIO_*`
credentials to each Job. If neither selector is set, S4 validates the
already-mounted catalog root. Configuring only one selector, losing storage
access, receiving an empty or partial snapshot, or finding a checksum mismatch
all stop the Job before S4. A release patch must provide the exact bucket and
immutable prefix; they intentionally have no defaults.
