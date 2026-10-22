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

## Verify a deployment

1. Bind the Actions run to the exact application merge SHA and reviewed operations
   pin. Inspect the required CI aggregate and applicable jobs; a green count that
   includes skipped jobs or omits a disabled workflow is not full CI evidence.
2. Check the tailnet join, authenticated-tailnet guard, and host-controller step
   conclusions. Confirm the caller environment supplies the intended OAuth
   credentials and tag without printing secret values. API token/key probes alone
   do not prove a runner joined the tailnet.
3. On the verified target host, read the matching entry in
   `<deploy_root>/releases/history.tsv`. Record timestamp, result, environment,
   source SHA and immutable image digest. Resolve `deploy_root` from the actual
   environment configuration; do not assume a production or staging default.
4. Inspect the intended container's image reference/digest, source revision label,
   start time, running/health state and restart count. Match them to the receipt.
   If a newer release has replaced it, report the old receipt as historical
   deployment evidence and the newer container as current health evidence.
5. Record evidence paths and scope. If GitHub job logs return 404 or are missing,
   retain the available job/step conclusions and controller receipt; state which
   details remain unverified. OAuth configuration plus the pinned action's
   selection logic supports an authentication-path inference, not a recovered
   login log. Container health does not establish user-message end-to-end behavior.

Verification is read-only. It does not require redeploying an older image,
changing live dependency selection, or restarting a gateway to recreate evidence.
