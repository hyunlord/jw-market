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

The CronJob is created with `suspend: true`. It computes the current
`km_keyword_event_stage` fingerprint before calling code-serving 238. If the
fingerprint matches the latest successful `mart_brand_activity_topic_runs`
record, it exits with `no-op: input unchanged` and spends zero GenOS calls. If
there is no stored seed, it fails closed instead of starting a first run.
