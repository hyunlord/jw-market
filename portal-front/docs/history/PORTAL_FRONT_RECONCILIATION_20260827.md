# Portal Front Reconciliation, Rounds 260-267

The public runtime and `develop` diverged at `839d72a4`. Investigation found that merge commits `24fe6f9b` and `f5fd7b77` recorded both parents while selecting one side's tree, which made ancestry an unreliable content proof.

Rounds 261-266 froze public evidence, checked upload/session/cache preservation, resolved seven conflicts in favor of public behavior, reconciled every hunk, and inventoried fixtures and tests. Of 26 commits unique to `develop`, zero contained behavior that should be transferred to public. Public retained the stronger runtime and regression contracts.

Round 267 corrected and deepened tooltip copy on the deployed public lineage, ending at `c9e44861`. The canonical `public` branch was created from that exact commit and tree. Historical `develop` remains reachable through the unchanged branch and the annotated tag `archive/develop-pre-canonical-20260827`; the deployed starting point is tagged `public-runtime-20260827-c9e44861`.

Future work must not treat `develop` as the public source or selectively merge it. Compare trees and behavior contracts before transferring any commit.
