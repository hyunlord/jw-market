# Row-Level Topic Assignment

This runner assigns fixed topic IDs to keyword-stage rows so topic share can be
recomputed by SQL filters without new LLM calls. The share definition is:

`affected_rows(topic) / brand_total_rows`

Each row-topic judgment is independent yes/no. A row can carry multiple topics,
so topic shares can sum above 100%.

## Inputs

- Stage rows: `jw_brand_activity_stage.km_keyword_event_stage`
- Topic rubric: `jw_brand_activity_stage.mart_brand_activity_topics.payload`
- Assignment table: `jw_brand_activity_stage.row_topic_assignment`
- Compatible view: `jw_brand_activity_stage.row_topic_assignment_share_view`
- Current topic set version: `brand_activity_replay_20260703_125045`

Only rows whose `therapeutic_class` appears in the stored topic payload scopes
are assigned. ATC4 values outside the current topic set, including the L04*
immunosuppressant rows, are intentionally out of scope until a topic set exists
for them.

## Batch And Checkpoint Contract

- Default batch size: 150 rows.
- Batches are grouped by `(scope_id, brand)`.
- Batch IDs are deterministic:
  `{scope_id}:{brand}:row_topic_v1:{index:06d}`
- A checkpoint JSONL row with `status: "ok"` marks that batch complete.
- Resume mode is just rerunning `execute` with the same checkpoint path; completed
  batch IDs are skipped and must not be called again.
- Assignments are inserted idempotently with primary key
  `(row_id, topic_id, topic_set_version)`.

## Missing-Row Fallback

The primary model response must echo every input `row_id` exactly once. If a
large batch omits some IDs:

1. Parsed assignments for returned rows are kept.
2. Omitted rows are reclassified in subbatches of at most 10 rows.
3. If a subbatch still omits rows, those rows are retried once more in subbatches
   of at most 3 rows.
4. Rows still omitted after fallback are recorded in checkpoint/log
   `missing_row_ids` and are not guessed or marked as none.

Duplicate IDs, unexpected IDs, and unknown topic IDs remain hard parse errors.

## Environment

Required environment variables:

- `GENOS_BEARER_TOKEN`: serving-direct token for model serving 163.
- `MARIADB_HOST`, `MARIADB_PORT`, `MARIADB_ROOT_PASSWORD`: MariaDB connection.
- Optional `GENOS_GATEWAY_CHAT_PATH_TEMPLATE` for in-cluster gateway paths, for
  example `/rep/serving/{serving_id}/chat/completions`.

Do not print secret values. Prefer writing the token into the process environment
from a secret file and delete that file after the run.

## Commands

Run these from the repository root.

### Apply DDL

Apply once only. If the assignment table already exists, the runner refuses to
overwrite it.

```bash
python -m pipeline.scripts.analysis.brand_activity.row_topic.execute apply-ddl \
  --schema jw_brand_activity_stage
```

### Dry Run

Dry run performs no LLM calls and is the cost gate.

```bash
python -m pipeline.scripts.analysis.brand_activity.row_topic.execute dry-run \
  --schema jw_brand_activity_stage \
  --topic-set-version brand_activity_replay_20260703_125045 \
  --checkpoint /tmp/row_topic_full/checkpoint.jsonl \
  --batch-size 150
```

### Execute Or Resume

Use the same checkpoint path for resume. The runner skips completed batch IDs.

```bash
python -m pipeline.scripts.analysis.brand_activity.row_topic.execute execute \
  --schema jw_brand_activity_stage \
  --topic-set-version brand_activity_replay_20260703_125045 \
  --checkpoint /tmp/row_topic_full/checkpoint.jsonl \
  --log /tmp/row_topic_full/execute_log.jsonl \
  --batch-size 150 \
  --max-calls 200 \
  --base-url http://llmops-gateway-api-service:8080 \
  --serving-id 163
```

For the 2026-07-04 recovery run, `--max-calls 200` is the cap for the remaining
work after the first 334 completed checkpoints. Do not use it to rerun completed
batches.

## Verification Queries

After execution, verify counts without exposing keyword text:

```sql
SELECT COUNT(*) AS assignments,
       COUNT(DISTINCT row_id) AS assigned_rows,
       COUNT(DISTINCT batch_id) AS batches
FROM jw_brand_activity_stage.row_topic_assignment
WHERE topic_set_version='brand_activity_replay_20260703_125045';

SELECT scope_id, COUNT(DISTINCT row_id) AS assigned_rows, COUNT(*) AS assignments
FROM jw_brand_activity_stage.row_topic_assignment
WHERE topic_set_version='brand_activity_replay_20260703_125045'
GROUP BY scope_id
ORDER BY scope_id;
```

Use `row_topic_assignment_share_view` to compare row-level assignment shares with
the existing mart payload and to demonstrate filtered distributions for period,
visit location, specialty, interest, and prescription evolution.

