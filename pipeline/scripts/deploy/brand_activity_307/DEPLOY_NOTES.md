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
only runtime dependencies from `requirements.txt`. `uv` is required because the
topic replay path invokes `uv run --script` for `run_auto_topic.py`.

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
