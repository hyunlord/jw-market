# auto_topic runtime environment

`auto_topic` runs in two supported environments.

## Local external Gateway

```bash
GENOS_DIRECT_BASE_URL=https://jwai-dev.jwhealthcare.com
GENOS_BEARER_TOKEN=<admin-login-access-token>
MARIADB_HOST=127.0.0.1
MARIADB_PORT=3308
```

`GENOS_DIRECT_BASE_URL` can be omitted locally because the code defaults to the external Gateway.
Local DB credentials may come from `pipeline/docker/.env`.

## Workflow in-cluster Gateway

```bash
GENOS_DIRECT_BASE_URL=http://llmops-gateway-api-service:8080
GENOS_GATEWAY_CHAT_PATH_TEMPLATE=/rep/serving/{serving_id}/chat/completions
MARIADB_HOST=llmops-mariadb-service
MARIADB_PORT=3306
MARKET_GROUP_SCHEMA=jw_brand_activity_stage
GENOS_BEARER_TOKEN=<optional-if-internal-gateway-requires-auth>
```

The path template must keep `{serving_id}` when the same workflow code may switch serving revisions.
If the internal Gateway accepts unauthenticated in-cluster traffic, leave `GENOS_BEARER_TOKEN` unset.
