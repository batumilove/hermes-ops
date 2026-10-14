# Hermes Ops

Fork-owned operational tooling for Hermes deployments:

- digest-pinned Compose deploy and automatic rollback
- running-stack acceptance and attestation verification
- staging Telegram socket diagnostics with crash recovery
- reusable shadow validation and deployment workflows
- container supervision assets and operational evidence recording

The application fork invokes workflows from this repository by full commit
SHA. Deployment callers must also pass the same reviewed `ops_sha`; the
workflow checks out no moving branch before it transfers or executes tooling.

Image publication prepares the application's install stamp before Docker removes
Git from the build context. The application's image-identity helper authenticates
the pinned release object and accepted fork import, then records a source package
version bound to the exact caller SHA. Both package metadata and the image marker
consume that version; development placeholders remain unchanged in source manifests.

The deployment script records the source SHA and immutable image digest, and
promotes an already-built digest. Rollback restores both the previous release
environment and its digest without rebuilding.

Deployment hosts run a reviewed, root-owned controller from
`/usr/local/libexec/hermes-deployment-controller`. GitHub Actions can request
only exact digest/SHA transitions through its restricted sudoers entry; it
cannot upload or replace deployment tooling. Install or update the controller
from a reviewed checkout with:

```bash
sudo scripts/deploy/install-hermes-deployment-controller.sh
```

The controller also owns environment-scoped soak leases, preventing a deploy
or rollback from replacing a release while its validation window is active.
BatumiLove edge distribution for Hermes Agent
