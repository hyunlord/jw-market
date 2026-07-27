# Brand Activity code-serving 307 mirror

Source of truth stays in `jw-market`.

The gitea `llmops/307` deployment repo is generated from this repository with:

```bash
python -m pipeline.scripts.deploy.brand_activity_307.build_mirror --output /tmp/llmops_307_mirror
```

Image preparation:

```bash
docker load -i /tmp/market_apis.tar
docker image inspect mnc/template-code-serving:market_apis
```

`Dockerfile` intentionally keeps the template code-serving entrypoint and installs
only runtime dependencies from `requirements.txt`. `uv` remains available for
operator convenience, but the deployed topic replay path uses the current Python
interpreter so the airgapped container never resolves PEP 723 dependencies at
runtime.

Code-serving env:

```bash
MCP_RUN_CMD=python -m pipeline.scripts.serving.brand_activity.topic_server
SERVER_PORT=8710
MAX_CONCURRENCY=1
IDLE_TIMEOUT_SECONDS=1800
GENOS_DIRECT_BASE_URL=http://llmops-gateway-api-service:8080
GENOS_GATEWAY_CHAT_PATH_TEMPLATE=/rep/serving/{serving_id}/chat/completions
MARIADB_HOST=llmops-mariadb-service
MARIADB_PORT=3306
MARKET_GROUP_SCHEMA=jw_brand_activity_stage
```

The child server exposes MCP-compatible JSON-RPC tools on `/mcp`:

- `run_topic_extraction`
- `get_status`
- `get_result`

Monthly scheduler:

```bash
kubectl -n llmops apply -f pipeline/scripts/deploy/brand_activity_307/cronjob_topic_monthly.yaml
```

The CronJob was initially created with `suspend: true` so its first
non-production run could be verified before activation. It now runs as an
active monthly production schedule (`suspend: false`). The exact activation
mutation timestamp is not retained in manifest history; live status proves it
was active no later than its first schedule at `2026-07-04T19:00:00Z`.
Activation followed the verified first-run gate so topic axes would refresh
monthly.

The job computes the current `km_keyword_event_stage` fingerprint before
calling code-serving 238. If the fingerprint matches the latest successful
`mart_brand_activity_topic_runs` record, it exits with
`no-op: input unchanged` and spends zero GenOS calls. If there is no stored
seed, it fails closed instead of starting a first run.

Row-topic monthly scheduler:

```bash
kubectl -n llmops apply -f pipeline/scripts/deploy/brand_activity_307/cronjob_row_topic_monthly.yaml
```

As of `2026-07-27`, this CronJob has no controller-scheduled run:
`lastScheduleTime` is absent and all three retained child Jobs were manually
instantiated. Its `2026-08-04T22:00:00Z` run is therefore expected to be the
first scheduled execution. The retained Kubernetes metadata does not preserve
the historical `suspend` transition, so the exact reason the
`2026-07-05T22:00:00Z` schedule did not fire is unknown.
