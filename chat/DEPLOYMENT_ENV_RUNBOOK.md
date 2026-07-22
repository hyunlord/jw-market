# Chat and 235 deployment environment runbook

The tracked Deployment files are secret-safe, server-side-apply ownership
manifests for stable environment settings. They are deliberately incomplete and
must only be applied to existing Deployments. They do not own image, replicas,
release identity, probes, resources, selectors, or other workload fields.

| Deployment | Stable env owner | Manifest-owned env |
| --- | --- | ---: |
| `jw-chat-agent-poc` | `jw-chat-env-canonicalizer` | 64 |
| `code-serving-235` | `jw-chat-env-canonicalizer` | 36 |

The former chat env and warmup fragments were absorbed into the chat env
ownership manifest. Keeping both forms would allow the same field to acquire
two owners, so the fragments remain removed. The chat HPA owns replicas; the
release or workload controller owns every other omitted field.

Do not use ordinary client-side `kubectl apply` with these files. Use a dedicated
server-side field manager so omitted fields retain their existing owners:

```bash
kubectl -n llmops apply --server-side --dry-run=server \
  --field-manager=jw-chat-env-canonicalizer \
  -f chat/jw-chat-agent-poc/deploy/deployment.yaml -o json
```

The dry-run result must retain the exact live image and replica count. A first
ownership transfer can report conflicts on env entries owned by another
manager. Review the conflict paths and the complete masked env census before a
separately approved apply uses `--force-conflicts`; never force unrelated
workload fields.

## Release-time identity

Do not hardcode per-release identity into the env ownership manifest.
Deployment automation must set the image and these environment variables in the
same release transaction:

- chat: `APP_VERSION`, `JW_CHAT_GIT_SHA`, `JW_CHAT_IMAGE_DIGEST`
- 235: `COMMIT_HASH`, `JW_235_GIT_SHA`, `JW_235_IMAGE_DIGEST`,
  `JW_235_RELEASE_ID`, `OPENAPI_VERSION`

The chat values must equal the committed source SHA. The 235 OpenAPI marker
must name the contract shipped by the same source SHA. Remove the stale generic
chat aliases `GIT_SHA` and `COMMIT_SHA`; they must not be used as runtime
identity. If either alias reappears, locate its image or deployment injection
source instead of trusting its value.

The observed serving IDs in the stable env set are common/final/planner `190`
and deep `202`. These are recorded observations, not a policy declaration. A
policy change requires a separate decision and a new verified manifest update.

Before applying the 235 manifest, provision Secret
`code-serving-235-runtime-secrets` with key `REPOSITORY_URL`. The live snapshot
held that credential-bearing URL as a literal. The tracked manifest deliberately
converts it to `secretKeyRef`; never copy the literal into Git or an audit file.
The explicit `value: null` beside `valueFrom` is intentional: server-side apply
uses it to remove the live literal while transferring this one env entry to the
Secret reference. A no-force server dry-run must first show only the expected
field-manager conflict on that literal. Provision and hash-verify the Secret
before any separately approved `--force-conflicts` apply; never force this
ownership transfer while the Secret is absent.

`FILE_SQL_ENABLED` is intentionally absent from the 235 manifest and required
set. Its live value and policy are being evaluated separately. Do not infer an
OFF or ON policy from that omission.

## Ordered post-deploy gates

Before applying either env ownership manifest, run the field-ownership gate:

```bash
python3 pipeline/scripts/gates/manifest_field_ownership_gate.py \
  < chat/jw-chat-agent-poc/deploy/deployment.yaml
python3 pipeline/scripts/gates/manifest_field_ownership_gate.py \
  < chat/wf301-vdb-bridge/deploy/deployment.yaml
```

It must report one checked container and zero failures for each file. Then use
server-side dry-run to prove that the live image and replicas survive the merge.
An image field, a replicas field, or an empty container population is a hard
stop.

Run these gates after every image, mode, or env transition:

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

The presence gate checks names only. It never prints or compares values. The
required sets retain release-time identities even though the env ownership
manifests omit them. Missing keys, malformed input, and an empty required
population all exit non-zero.

Omitting the stale generic aliases `GIT_SHA` and `COMMIT_SHA` from the env
ownership manifest does not by itself remove live entries owned by another
field manager. The release track must remove them explicitly after proving their
source; their continued presence must not be interpreted as current identity.

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
