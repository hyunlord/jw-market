# Agent3 Brand Strength Operations

Agent3 builds one `agent3_brand_strength` row per brand from mart realtime
profile and slice evidence. It does not read portal cache tables.

## Workflow

- Workflow: `jw-agent3-brand-strength`
- Workflow id: `316`
- Active prompt revision: `5365`
- Active deploy id: `1324`
- Serving: `163`

Revision `5365` is the live prompt revision that asks wf316 to return `candidate_index` only; exact raw `numbers` are injected server-side from the matched candidate while narrative text still must copy only `display_numbers`. Keep
`AGENT3_WORKFLOW_REV` aligned with the live revision so idempotency hashes are
rev-aware.

wf316 narrative validation stays strict: every numeric token in a narrative must
come from the candidate `display_numbers`, and raw high-precision decimals are
rejected. If a brand fails validation during a full run, the runner retries that
brand once with the same input. If the retry also fails, the brand is stored as
profile-only with `unavailable_reason="validation_failed"` in
`strength_summary_json`, and the failure is logged in the run output. A chunk
should pause for PL review when validation-isolated brands exceed 2% of
wf-call targets or 10 brands, whichever is stricter for the chunk.

## Build the Job Image

The Agent3 runner is packaged in the backend image and used only by Agent3
Job/CronJob manifests. Do not roll this image into the live backend deployment
as part of Agent3 batch work.

```bash
docker build --platform linux/amd64 -f api/Dockerfile \
  --build-arg APP_VERSION=v0.9.XX-agent3-<commit8>-<YYYYMMDD> \
  -t asia-northeast3-docker.pkg.dev/prj-jw-agn-stg-ai/ar-jw-agn-stg-genos-dev-01/jw-market-backend-api:v0.9.XX-agent3-<commit8>-<YYYYMMDD> \
  .
docker push asia-northeast3-docker.pkg.dev/prj-jw-agn-stg-ai/ar-jw-agn-stg-genos-dev-01/jw-market-backend-api:v0.9.XX-agent3-<commit8>-<YYYYMMDD>
```

Current full-run image:

- Tag: `asia-northeast3-docker.pkg.dev/prj-jw-agn-stg-ai/ar-jw-agn-stg-genos-dev-01/jw-market-backend-api:v0.9.59-agent3-90015ca5-20260706`
- Digest: `sha256:67344fa150eae6c7046b2dd115a2525693f43670e8dced7d2d5b1142f54a00a0`
- Runtime code baseline: `90015ca5` (slice-label validator fix plus validation_failed recovery)

## Runner

```bash
python -m pipeline.scripts.agent3.run_full \
  --brand-source general_all \
  --mode dry-run \
  --chunk-index 0 \
  --chunk-size 500 \
  --output /tmp/agent3_full_result.json
```

Use `--brands brand_key_a,brand_key_b` for bounded sample checks; the same
loader path is used, but the universe is limited to the explicit list.

`--brand-source`:

- `jw25`: display catalog JW25.
- `strategic_ml`: distinct brands in `mart_strategic_ml_brand_metric`.
- `general_all`: distinct `brand_key` values in `mart_general_brand_metric` (the general-view operating universe).

`--mode`:

- `dry-run`: profile and candidate extraction only. LLM 0, DB write 0.
- `full`: upsert into `agent3_brand_strength` only.

## Idempotency

Full mode computes `input_hash = sha256(profile + candidates + workflow_rev)`.
`agent3_brand_strength` is keyed by `brand_key`, not display name, because
`mart_general_brand_metric.brand_key` is the stable general-view universe while
display names are not 1:1. If the existing row for the same `brand_key` has the
same `input_hash` and `workflow_rev`, wf316 is not called and the row is
skipped. Regular reruns therefore pay only for changed inputs.

When a `brand_key` maps to multiple display names, the runner chooses the
canonical `brand_name` from `mart_general_brand_metric` by the highest latest
sales value and stores it as display-only metadata. The key remains the only
idempotency and upsert identity.

Serving lookup uses `serving_brand_name`, not `brand_name`. This column is
nullable and unique: if several `brand_key` rows share one canonical display
name, only the latest-sales representative stores that name; the other rows stay
stored with `serving_brand_name = NULL`. API routes that resolve
`GET /api/deep-analysis/{brand_name}` must filter on `serving_brand_name` so a
decoded name resolves to at most one row.

## Schema Reset

The brand-key schema is stored in
`pipeline/scripts/agent3/sql/002_recreate_agent3_brand_strength_brand_key.sql`.
For the JW25 pilot reset, dump the old 25 rows as evidence, run:

```sql
DROP TABLE IF EXISTS agent3_brand_strength;
```

then apply the `002` DDL and rerun the JW25 pilot. Do not put `DROP TABLE` in the
runner path; `ensure_table()` is intentionally create-only so routine Agent3
runs cannot erase prior rows.

Apply `pipeline/scripts/agent3/sql/003_add_serving_brand_name.sql` after `002`
to add the serving-name unique constraint.

## Kubernetes

- `deploy/k8s/agent3/agent3-full-job.yaml`: manual chunk template.
- `deploy/k8s/agent3/agent3-refresh-cronjob.yaml`: suspended daily refresh
  template at 06:00 KST.

Operational sequence:

1. Run one or more dry-run chunks.
2. Review candidate count and estimated cost.
3. Get PL approval for real LLM calls.
4. Run full chunks.
5. Keep `jw-agent3-refresh-daily` suspended until PL explicitly approves
   recurring execution.
