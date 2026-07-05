# Agent3 Brand Strength Operations

Agent3 builds one `agent3_brand_strength` row per brand from mart realtime
profile and slice evidence. It does not read portal cache tables.

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
If the existing row has the same `input_hash` and `workflow_rev`, wf316 is not
called and the row is skipped. Regular reruns therefore pay only for changed
inputs.

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
