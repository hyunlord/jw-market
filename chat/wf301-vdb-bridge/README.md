# wf301 VDB Bridge

This service is the canonical source for the wf301 VDB registration bridge.
GitHub is the rebuild source of truth; the GenOS code-serving repository is a deploy-side copy/provenance surface.

Current deployed reference:

- Code-serving: 232
- Docker image: `mnc/wf301-vdb-bridge:api-20260701`
- DockerImage id: 264
- Image digest observed during deployment: `sha256:6286715ec3e8942145148bc674a77b83c4a837707eda03dfc333eceb1dcc8a6e`

The bridge imports already-preprocessed Temp VDB chunks into registered Shared VDB 139 with GenOS ledger rows, then exposes session-scoped APIs for commit, search, document listing, quota checks, and health.

## Endpoints

- `GET /health`
- `POST /dry-run`
- `POST /commit`
- `POST /search`
- `GET /documents`
- `GET /quota/check`

## Runtime Configuration

Important environment variables:

- `TARGET_VDB_ID`
- `TARGET_VDB_COLLECTION`
- `COMMIT_ENABLED`
- `ALLOWED_WORKFLOW_IDS`
- `WEAVIATE_BASE`
- `EMBEDDING_BASE`
- `TTL_DAYS`
- `QUOTA_MAX_FILES`
- `QUOTA_MAX_PER_REQUEST`
- `QUOTA_MAX_FILE_MB`
- `QUOTA_MAX_SESSION_MB`
- `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`

Database credentials must be injected by Kubernetes SecretKeyRef or an equivalent secret mechanism. Do not hard-code credentials in this repository.

## Container Contract

The image follows the GenOS code-serving contract:

- Working directory: `/app`
- Port: `8080`
- Command: `supervisord -n -c /etc/supervisor/supervisord.conf`
- Non-root numeric user: `3000:3000`
