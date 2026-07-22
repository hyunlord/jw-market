# Chat and 235 deployment environment runbook

The tracked Deployment manifests are complete, secret-safe baselines for the
live-observed snapshots below. They are not evidence that a later runtime still
matches those snapshots.

| Deployment | Observed source snapshot | Manifest |
| --- | --- | --- |
| `jw-chat-agent-poc` | `998f3519ea85c729d2e4fe201867af48315ede03` | `jw-chat-agent-poc/deploy/deployment.yaml` |
| `code-serving-235` | `abf6e59aafa06cf47ef2b31b771207a90627a57e` | `wf301-vdb-bridge/deploy/deployment.yaml` |

The former chat env and warmup patch files were absorbed into the complete
chat manifest. Keeping both forms would allow the same field to acquire two
owners, so the fragments were removed. Chat replicas are omitted because an
HPA owns that field. The 235 manifest retains the live-observed singleton.

## Release-time identity

Do not hardcode per-release identity into the baseline manifest. Deployment
automation must set the image and these environment variables in the same
release transaction:

- chat: `JW_CHAT_GIT_SHA` and `APP_VERSION`
- 235: `JW_235_GIT_SHA` and `OPENAPI_VERSION`

The chat values must equal the committed source SHA. The 235 OpenAPI marker
must name the contract shipped by the same source SHA. Remove the stale generic
chat aliases `GIT_SHA` and `COMMIT_SHA`; they must not be used as runtime
identity. If either alias reappears, locate its image or deployment injection
source instead of trusting its value.

The observed serving IDs in this baseline are common/final/planner `190` and
deep `202`. These are recorded observations, not a policy declaration. A policy
change requires a separate decision and a new verified manifest update.

Before applying the 235 manifest, provision Secret
`code-serving-235-runtime-secrets` with key `REPOSITORY_URL`. The live snapshot
held that credential-bearing URL as a literal. The tracked manifest deliberately
converts it to `secretKeyRef`; never copy the literal into Git or an audit file.

`FILE_SQL_ENABLED` is intentionally absent from the 235 manifest and required
set. Its live value and policy are being evaluated separately. Do not infer an
OFF or ON policy from that omission.

## Ordered post-deploy gates

Run these gates after every image or mode transition:

1. Verify pod `imageID` equals the intended digest exactly and Deployment
   `generation` equals `observedGeneration`.
2. Set pod-template annotations `jw-chat/release`, `jw-chat/deploy-at` (UTC),
   and `jw-chat/track=chat`.
3. Pipe the live Deployment JSON to the presence-only gate:

```bash
kubectl -n llmops get deployment jw-chat-agent-poc -o json \
  | python3 pipeline/scripts/gates/env_presence_gate.py \
      --required-file pipeline/scripts/gates/required_env/jw-chat-agent-poc.json

kubectl -n llmops get deployment code-serving-235 -o json \
  | python3 pipeline/scripts/gates/env_presence_gate.py \
      --required-file pipeline/scripts/gates/required_env/code-serving-235.json
```

The gate checks names only. It never prints or compares values. Missing keys,
malformed input, and an empty required population all exit non-zero.

## MCP standby recovery

Treat the four MCP URLs and `CHAT_EXTERNAL_TOOL_AGENT_ENABLED` as one atomic
configuration unit. Before `wire-chat`, verify all four standby Deployments are
Ready and each `/json` endpoint returns a successful `tools/list` response.
If any member fails, inject none of them. Once all four pass, run
`deploy/mcp_standby/render_standby.py wire-chat`, then repeat identity,
annotation, and env-presence gates. Partial URL injection is forbidden because
it creates a runtime that appears enabled while only some tool families work.

## 235 HOLD lineage

The earlier HOLD anchor `534d8952` is not the observed 235 runtime used here.
The manifest is based on `abf6e59a`; any deployment based on the HOLD anchor
must be rebased and revalidated rather than treated as equivalent.
