# Analysis Cache Blue-Green Publish

This path publishes `mart_analysis_level_block` and `cache_brands` as one
generation. It never writes either live table before the final atomic rename.

## Prepare and build staging

```bash
python -m pipeline.scripts.deploy.analysis_cache_blue_green \
  --target-db "$DB_NAME" prepare

MALB_TARGET_DB="$DB_NAME" \
MALB_TARGET_TABLE=mart_analysis_level_block_staging \
MALB_MODE=build MALB_SHARD_COUNT=4 MALB_SHARD_INDEX=0 \
python -m pipeline.scripts.etl.build_analysis_level_blocks

python -m pipeline.scripts.etl.build_cache_brands \
  --target-table "$DB_NAME.cache_brands_staging"
```

Run the four MALB shards with indices `0` through `3`. Both `MALB_TARGET_DB`
and `MALB_TARGET_TABLE` are mandatory so a missing variable cannot silently
select the live table. Run parity against the same staging identity by retaining
both variables and setting `MALB_MODE=parity`. The caller must also record the
source epoch and compare replay payloads with mart-direct output before
switching. A live-table build requires the same explicit opt-in and is outside
this blue-green workflow.

## Validate and switch

```bash
python -m pipeline.scripts.deploy.analysis_cache_blue_green \
  --target-db "$DB_NAME" validate \
  --expected-brands-sha256 "$BRANDS_SHA" \
  --expected-source-epoch "$SOURCE_EPOCH"

python -m pipeline.scripts.deploy.analysis_cache_blue_green \
  --target-db "$DB_NAME" switch \
  --run-id "$RUN_ID" \
  --expected-brands-sha256 "$BRANDS_SHA" \
  --expected-source-epoch "$SOURCE_EPOCH"
```

`switch` revalidates staging immediately before issuing one four-move
`RENAME TABLE` statement. Its JSON result records the statement's elapsed time,
which is the Galera TOI observation for the release audit.

## Roll back

```bash
python -m pipeline.scripts.deploy.analysis_cache_blue_green \
  --target-db "$DB_NAME" rollback --run-id "$RUN_ID"
```

Rollback also uses one four-move `RENAME TABLE` statement. The old and failed
tables are retained for audit and must not be dropped by this workflow.
