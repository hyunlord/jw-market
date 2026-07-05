# Brand Activity Raw Staging ETL

This loader stages CSD ChannelDynamics and Keyword workbooks into the
brand-activity raw and legacy stage schemas.

## Stage Scope

`load_raw_staging.py` accepts:

```bash
python -m pipeline.scripts.etl.brand_activity.load_raw_staging \
  --stage-scope all
```

Valid values:

- `all`: legacy behavior. Discover, parse, raw-load, truncate, and rebuild both
  CSD and Keyword stage tables.
- `csd`: discover and parse only CSD workbooks, insert only
  `raw_csd_channel_dynamics`, and truncate/rebuild only
  `csd_channel_dynamics_stage`.
- `keyword`: discover and parse only Keyword workbooks, insert only
  `raw_keyword_events`, and truncate/rebuild only `km_keyword_event_stage`.

The default is `all` to preserve existing callers.

## CSD Rebuild Procedure

1. Back up the current CSD raw and stage tables.
2. Run a no-write dry-run:

   ```bash
   python -m pipeline.scripts.etl.brand_activity.load_raw_staging \
     --stage-scope csd \
     --audit-dir /tmp/csd_rebuild_dry_run
   ```

   Confirm `run_summary.json.execution_plan.truncate_targets` contains only
   `csd_channel_dynamics_stage`.

3. Execute the CSD-only load during a quiet window:

   ```bash
   python -m pipeline.scripts.etl.brand_activity.load_raw_staging \
     --stage-scope csd \
     --execute \
     --audit-dir /tmp/csd_rebuild_execute
   ```

4. Verify period coverage, `jw_channel` distribution, and the S1/S4 API
   quarter counts after the stage refresh.

## Keyword Safety Warning

Do not run `--stage-scope keyword` or the default `all` mode against live data
unless the row-topic assignment rebuild plan is approved. `km_keyword_event_stage`
row IDs are referenced by `row_topic_assignment` and
`row_topic_assignment_status`. Truncating and reloading Keyword rows changes
those IDs even when the workbook content is unchanged, which makes existing
row-topic assignments orphaned and causes DB-pending mode to treat rows as
new work.

Keyword rebuilds require a separate plan for topic extraction and row-topic
assignment regeneration.
