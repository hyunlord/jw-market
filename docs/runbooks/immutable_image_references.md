# Immutable image reference runbook

Kubernetes workload manifests must reference container images by immutable
digest. Mutable image tags are build-discovery metadata only and must not be
used in `Deployment`, `CronJob`, `StatefulSet`, or one-shot `Job` deployment
specifications.

## Release sequence

1. Build and push the image.
2. Resolve and record the registry digest.
3. For market backend releases, use
   `pipeline.scripts.deploy.backend_image_rollout`; it updates the backend
   Deployment and its paired cache-warm CronJob as one release set.
4. Wait until `generation == observedGeneration` and the full pod population is
   Ready.
5. Verify actual pod `status.containerStatuses[].imageID` values, not only the
   declared `spec.containers[].image`.
6. Compare changed runtime file bytes with `git <commit>` blobs.
7. Create and push the required annotated release tag.

For resources outside the market backend release set, update the tracked
manifest to `repository@sha256:<digest>`, commit it, apply it, and verify the
live declaration uses a digest:

   ```bash
   kubectl get <resource> -n llmops \
     -o jsonpath='{..image}' | grep -Eq '@sha256:[0-9a-f]{64}$'
   ```

Do not use `kubectl set image` with a mutable tag. A declaration check alone is
not runtime identity evidence; inspect `imageID` after rollout convergence.

## Source-to-image provenance

For each production release, create and push an annotated git tag:

```text
ops/market-<generation>-<short-source-sha>-<YYYYMMDD>
```

The annotation must include the full image digest and the workload or release
scope. The git tag records which source commit produced an image; it is distinct
from a container registry tag and does not permit mutable image references in
Kubernetes manifests.

Example:

```bash
git tag -a ops/market-387-0a8e9080-20260719 0a8e9080 \
  -m 'image: repository@sha256:<digest>'
git push jw-private ops/market-387-0a8e9080-20260719
```

Historical releases are not retroactively tagged unless their source commit and
image digest can both be independently verified.
