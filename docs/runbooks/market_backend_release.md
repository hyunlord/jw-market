# Market backend release set

`jw-market-backend-api` and `dynamic-market-cache-warm` are one release set.
The test2 Deployment and `dynamic-market-cache-warm-test2` follow the same
rule. A backend release is incomplete until both resources reference the same
immutable digest and every Ready backend pod reports that digest in
`status.containerStatuses[].imageID`.

## Current freeze

`deploy/k8s/jw-market/BACKEND_DEPLOY_FREEZE` is active. Do not deploy the
backend, test2 backend, cache-warm CronJobs, or any other workload that uses the
backend image until jw market issues an explicit resume notice. Remove the
marker only in a reviewed commit after that notice.

## Canonical entry point

After the freeze is removed, use only the tracked rollout command. Do not run
standalone `kubectl set image`, `kubectl rollout undo`, or mutable-tag deploys.

```bash
python3 -m pipeline.scripts.deploy.backend_image_rollout \
  --target prod \
  --image 'REGISTRY/jw-market-backend-api@sha256:<64hex>' \
  --source-commit '<full-40-character-sha>' \
  --expected-generation '<live-generation>' \
  --verify-blob 'pipeline/scripts/api/main.py=/app/pipeline/scripts/api/main.py'
```

Use `--target test2` for the test2 pair. Repeat `--verify-blob` for every
serving source file changed between the current live source commit and the
candidate. The repository path is hashed with `git show`; the absolute runtime
path is hashed in every Ready pod. Environment variables, image tags, and
string grep are not source identity evidence.

## Fail-closed sequence

1. Refuse to run while the freeze marker exists.
2. Require a full `repository@sha256:<64hex>` image and exact 40-character git
   commit.
3. Compare the live Deployment generation with `--expected-generation` before
   mutation.
4. Snapshot the previous immutable backend and cache-warm image references.
5. Set the backend Deployment image, then the paired cache-warm CronJob image,
   to the same digest.
6. Wait for rollout completion and require
   `generation == observedGeneration`, full updated/Ready/available population,
   `unavailable=0`, and `restartCount=0`.
7. Read every non-terminating Ready pod's actual `imageID`; all digests must
   equal the requested digest. The CronJob pod template must reference the
   exact same immutable image.
8. Compare every declared git blob byte-for-byte with every Ready pod.
9. Create and push the annotated tag
   `ops/market-<gen>-<shortsha>-<YYYYMMDD>` with the full image digest in the
   annotation.

Any failure after mutation restores both resources to their separately
captured previous immutable references and waits for the backend rollback to
converge. A failed tag push also rolls back; a release without the annotated
tag is not accepted.

This procedure updates only the cache-warm CronJob template. It does not start
a warm Job or mutate cache contents. Immediate cache alignment remains a
separate PL-gated operation.
