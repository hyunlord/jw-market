# Immutable image reference runbook

Kubernetes workload manifests must reference container images by immutable
digest. Mutable image tags are build-discovery metadata only and must not be
used in `Deployment`, `CronJob`, `StatefulSet`, or one-shot `Job` deployment
specifications.

## Release sequence

1. Build and push the image.
2. Resolve and record the registry digest.
3. Update the tracked manifest to `repository@sha256:<digest>`.
4. Commit the manifest change before applying it.
5. Apply the committed manifest.
6. Verify the live declaration uses a digest:

   ```bash
   kubectl get <resource> -n llmops \
     -o jsonpath='{..image}' | grep -Eq '@sha256:[0-9a-f]{64}$'
   ```

Do not use `kubectl set image` with a mutable tag. An orchestrator rebuild is
complete only when the new digest is reported and the tracked manifest is
updated in the same release workflow.

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
