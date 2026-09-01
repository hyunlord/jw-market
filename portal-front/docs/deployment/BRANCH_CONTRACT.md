# Portal Front Branch and Route Contract

## Stable branches

| Surface | Stable source | Deployment rule |
|---|---|---|
| Public `jwai.jwhealthcare.com` | `public` | PL-approved immutable digest, full-container resourceVersion CAS patch |
| Dev `jwai-dev.jwhealthcare.com` | `develop` | Dev-only validation; never promote it to public by name |
| Test2 `/test2` | Explicit experiment branch | Record the branch and provenance labels before deployment |
| Stream Lab `/stream-lab/` | Explicit experiment branch | Record the branch and provenance labels before deployment |

`develop` is preserved history, not the public release branch. Public releases must be built from a clean, attached `public` HEAD that exactly matches the remote `public` SHA.

## Route target gate

Before every deployment, trace the requested host and path through DNS/load balancer, Gateway or Ingress, VirtualService, Service, Endpoints, Pod, and the named container imageID. Record each object and namespace. Do not select a Deployment because its name looks related.

Public currently resolves to `portal/portal-front`; this is evidence to re-check, not a timeless alias. Cluster commands must name the approved DEV context and namespace. Deployment uses a server dry-run followed by one full-containers CAS patch. `kubectl rollout undo`, partial container patches, force-push, and concurrent deployment to the same target are forbidden.

## No selective merge

A merge must be justified by both-parent tree comparison. Recording both parents while silently choosing one parent's tree is forbidden. Missing paths must be listed and individually justified; otherwise the merge is invalid.
