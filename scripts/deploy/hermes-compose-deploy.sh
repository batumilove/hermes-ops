#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage:
  hermes-compose-deploy.sh deploy ENV IMAGE DIGEST SOURCE_SHA DEPLOY_ROOT
  hermes-compose-deploy.sh rollback ENV IMAGE - SOURCE_SHA DEPLOY_ROOT

The target must already contain:
  DEPLOY_ROOT/compose.yml
  DEPLOY_ROOT/runtime.env (mode 0600; HERMES_DATA_DIR, HERMES_UID, HERMES_GID)

Registry authentication and runtime secrets are host prerequisites. This script
never accepts secret values and never writes them to deployment evidence.
EOF
  exit 64
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ $# -eq 6 ]] || usage
operation=$1
environment=$2
image=$3
digest=$4
source_sha=$5
deploy_root=$6

[[ $operation == deploy || $operation == rollback ]] || usage
[[ $environment =~ ^[a-z][a-z0-9-]{1,31}$ ]] || die "invalid environment name"
[[ $image == ghcr.io/batumilove/hermes-agent-deploy ]] || die "unexpected image repository"
[[ $source_sha =~ ^[0-9a-f]{40}$ ]] || die "source SHA must be a full lowercase commit SHA"
[[ $deploy_root == /* && $deploy_root != / ]] || die "deployment root must be an absolute non-root path"
if [[ $operation == deploy ]]; then
  [[ $digest =~ ^sha256:[0-9a-f]{64}$ ]] || die "image digest must be sha256:<64 lowercase hex characters>"
fi

command -v docker >/dev/null || die "docker is not installed"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is not installed"
command -v flock >/dev/null || die "flock is not installed"

mkdir -p "$deploy_root/releases"
compose_file="$deploy_root/compose.yml"
runtime_env="$deploy_root/runtime.env"
current_env="$deploy_root/release.env"
previous_env="$deploy_root/release.previous.env"
history_file="$deploy_root/releases/history.tsv"
acceptance_helper="$deploy_root/verify-running-stack.py"
lock_file="$deploy_root/deploy.lock"
shared_staging_lock=/run/lock/hermes-staging-diagnostic.lock

[[ -f $compose_file ]] || die "missing $compose_file"
[[ -f $runtime_env ]] || die "missing $runtime_env"
[[ -f $acceptance_helper && ! -L $acceptance_helper ]] || die "missing or unsafe $acceptance_helper"
runtime_mode=$(stat -c '%a' "$runtime_env")
(( (8#$runtime_mode & 077) == 0 )) || die "$runtime_env must not be group/world accessible (expected mode 0600)"

if [[ $environment == batumi-staging && -e $shared_staging_lock ]]; then
  [[ -f $shared_staging_lock && ! -L $shared_staging_lock ]] || die "unsafe shared staging lock"
  [[ $(stat -c '%U:%G:%a:%h:%s' -- "$shared_staging_lock") == root:hermes-deploy:660:1:0 ]] || die "shared staging lock metadata mismatch"
  exec 9<>"$shared_staging_lock"
else
  # Compatibility until the dormant helper is explicitly staged. Once staged,
  # its root-owned sticky-directory lock is authoritative for deploy/run/recover.
  exec 9>"$lock_file"
fi
flock -w 300 9 || die "timed out waiting for deployment lock"

compose() {
  docker compose \
    --project-name "hermes-$environment" \
    --env-file "$runtime_env" \
    --env-file "$current_env" \
    -f "$compose_file" "$@"
}

verify_release() {
  compose config --quiet || return 1
  timeout 360 docker compose \
    --project-name "hermes-$environment" \
    --env-file "$runtime_env" \
    --env-file "$current_env" \
    -f "$compose_file" \
    up -d --wait --wait-timeout 300 --remove-orphans || return 1
  local container="hermes-${environment}-gateway"
  local health running
  health=$(docker inspect --format '{{.State.Health.Status}}' "$container" 2>/dev/null || true)
  running=$(docker inspect --format '{{.State.Running}}' "$container" 2>/dev/null || true)
  [[ $running == true && $health == healthy ]]
}

record_evidence() {
  local result=$1 deployed_digest=$2
  umask 077
  printf '%s\t%s\t%s\t%s\t%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$result" "$environment" \
    "$source_sha" "$deployed_digest" >> "$history_file"
}

if [[ $operation == rollback ]]; then
  [[ -s $previous_env ]] || die "no previous release is available for rollback"
  rollback_from="$deploy_root/release.rollback-from.env"
  cp -p "$current_env" "$rollback_from"
  cp -p "$previous_env" "$current_env"
  if verify_release; then
    cp -p "$rollback_from" "$previous_env"
    deployed_digest=$(sed -n 's/^HERMES_IMAGE=.*@\(sha256:[0-9a-f]\{64\}\)$/\1/p' "$current_env")
    record_evidence rollback "$deployed_digest"
    rm -f "$rollback_from"
    printf 'Rollback complete: environment=%s digest=%s\n' "$environment" "$deployed_digest"
    exit 0
  fi
  cp -p "$rollback_from" "$current_env"
  verify_release || true
  rm -f "$rollback_from"
  record_evidence rollback-failed unknown
  die "rollback candidate failed health verification; original release was restored"
fi

candidate="$deploy_root/release.candidate.env"
umask 077
cat >"$candidate" <<EOF
HERMES_IMAGE=${image}@${digest}
HERMES_DEPLOY_ENV=${environment}
HERMES_SOURCE_SHA=${source_sha}
EOF

had_current=false
if [[ -s $current_env ]]; then
  had_current=true
  cp -p "$current_env" "$previous_env"
fi
mv -f "$candidate" "$current_env"

# Pull before replacement so a registry/network failure cannot stop the current
# healthy container. The image reference is digest-pinned by validation above.
if ! compose pull gateway; then
  if [[ $had_current == true ]]; then
    cp -p "$previous_env" "$current_env"
  else
    rm -f "$current_env"
  fi
  record_evidence pull-failed "$digest"
  die "image pull failed; current release was left untouched"
fi

failure_result="health-failed"
if verify_release; then
  if python3 "$acceptance_helper" \
    --environment "$environment" \
    --image "$image" \
    --digest "$digest" \
    --source-sha "$source_sha" \
    --deploy-root "$deploy_root"; then
    record_evidence deployed "$digest"
    printf 'Deployment complete: environment=%s source=%s digest=%s\n' \
      "$environment" "$source_sha" "$digest"
    exit 0
  fi
  failure_result="acceptance-failed"
fi

record_evidence "$failure_result" "$digest"
if [[ $had_current == true ]]; then
  cp -p "$previous_env" "$current_env"
  if verify_release; then
    recovered_digest=$(sed -n 's/^HERMES_IMAGE=.*@\(sha256:[0-9a-f]\{64\}\)$/\1/p' "$current_env")
    record_evidence automatic-rollback "$recovered_digest"
    die "new release failed health verification; previous release restored"
  fi
  die "new release failed and automatic rollback also failed"
fi

compose stop gateway >/dev/null 2>&1 || true
rm -f "$current_env"
die "first deployment failed health verification; unhealthy container stopped"
