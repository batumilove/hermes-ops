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

## Immutable-only publication

The reusable `build-image.yml` accepts typed boolean `immutable_only` (default
`false`, preserving existing callers). Set it to `true` in an independently
reviewed caller pinned to the exact operations commit, and pass the identical
`ops_sha`. Only the final `:candidate` promotion is skipped. The source-SHA
resolve/build, reuse-provenance verification, new GitHub-hosted attestation,
SBOM and fixable HIGH/CRITICAL vulnerability gate still run. This is not a
standalone dispatch endpoint and does not disable deployment jobs in a caller;
use a publication-only caller with no deploy job, or separately prove all caller
deployment conditions are disabled before dispatch. No `main`, `latest` or
other mutable tag is introduced here.

Admission requires the caller repository `batumilove/hermes-agent`, a protected
branch selected by the existing deployment-lane policy, and exact equality of
caller `github.sha`, requested source SHA and the current policy-branch head.
The existing aggregate CI context and GitHub Actions app attribution are unchanged.
Historical commits, tags, unprotected candidate refs and a dispatch from a newer
commit naming an older source are rejected; immutable-only is not a bypass lane.
The caller source identity is necessary because GitHub-generated provenance binds
the caller context, not an arbitrary checkout input. Publication jobs serialize
by repository and source SHA before resolving an existing tag, preventing two
cooperating calls from concurrently rebuilding the same source-SHA tag. This is
not registry-enforced immutability against unrelated writers or administrators.

After a real authorized run, independently resolve the `sha-<source SHA>` tag,
match its digest to workflow outputs, and verify the OCI attestation's image
subject, exact source SHA/ref and pinned signer on a GitHub-hosted runner. A
local Docker image ID or a successful local test is not hosted provenance, and
a hosted rebuild is not proof of byte equality with a previously tested local
image. Publication is not staging/production activation.

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
   start time, running/health state and restart count as current-container evidence.
   Compare the image digest and source revision with the receipt's deployed digest
   and source SHA. The receipt does not record container start time, health or
   restart count; retain its timestamp, result and environment as deployment evidence.
   If a newer release has replaced it, report the old receipt as historical
   deployment evidence and the newer container as current health evidence.
5. Record evidence paths and scope. If GitHub job logs return 404 or are missing,
   retain the available job/step conclusions and controller receipt; state which
   details remain unverified. OAuth configuration plus the pinned action's
   selection logic supports an authentication-path inference, not a recovered
   login log. Container health does not establish user-message end-to-end behavior.

Verification is read-only. It does not require redeploying an older image,
changing live dependency selection, or restarting a gateway to recreate evidence.
