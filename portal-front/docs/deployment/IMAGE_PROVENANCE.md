# Portal Image Provenance

Every newly built portal image must contain all five labels:

| Label | Contract value |
|---|---|
| `org.opencontainers.image.source` | `http://jwai-dev.jwhealthcare.com/gitea/jw-market/jw_portal_react` |
| `org.opencontainers.image.revision` | Full remote `public` commit SHA |
| `org.opencontainers.image.created` | UTC RFC3339 build time |
| `com.jw.source.tree-sha` | `HEAD^{tree}` |
| `com.jw.source.branch` | `public` |

Use `scripts/build-portal-image.sh` to build and `scripts/verify-image-provenance.sh` to verify. Both fail closed on missing or mismatched provenance. Existing images cannot be relabeled retroactively; absence remains a visible verification failure.

The repository has no confirmed CI contract. CI integration remains unknown and must not be invented.
