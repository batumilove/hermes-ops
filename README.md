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

## Deployment tailnet authentication

Configure `TAILSCALE_OAUTH_CLIENT_ID` and `TAILSCALE_OAUTH_SECRET` in the
caller's deployment environment (for staging, `batumi-staging`). The OAuth
client needs `auth_keys` scope for the environment's existing deployment tag.
Staging uses `tag:hermes-staging-deploy`, whose ACL permits only staging SSH;
set `TAILSCALE_TAGS` to that same tag. The pinned
Tailscale action prefers OAuth when its secret is present and creates
ephemeral, preauthorized runner identities. This avoids a reusable auth key
expiring between deployments. `TAILSCALE_AUTHKEY` remains a fallback for
environments that have not migrated; do not copy an admin API token into it.

After the action returns, deployment independently requires a `Running`
Tailscale backend with a local tailnet IP. Failed authentication, malformed
status, or CLI failure stops the job before SSH credentials are configured
or the host controller is invoked. Existing SSH host-key checks and tailnet
ACLs remain authoritative for destination access.
