#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage:
  hermes-compose-deploy.sh deploy ENV IMAGE DIGEST SOURCE_SHA DEPLOY_ROOT ASSET_ROOT
  hermes-compose-deploy.sh rollback ENV IMAGE DIGEST SOURCE_SHA DEPLOY_ROOT ASSET_ROOT

The reviewed root-owned asset directory must already contain:
  ASSET_ROOT/compose.yml
  ASSET_ROOT/verify-running-stack.py

The target must already contain:
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

[[ $# -eq 7 ]] || usage
operation=$1
environment=$2
image=$3
digest=$4
source_sha=$5
deploy_root=$6
asset_root=$7

[[ $operation == deploy || $operation == rollback ]] || usage
[[ $environment =~ ^[a-z][a-z0-9-]{1,31}$ ]] || die "invalid environment name"
[[ $image == ghcr.io/batumilove/hermes-agent-deploy ]] || die "unexpected image repository"
[[ $source_sha =~ ^[0-9a-f]{40}$ ]] || die "source SHA must be a full lowercase commit SHA"
[[ $deploy_root == /* && $deploy_root != / ]] || die "deployment root must be an absolute non-root path"
[[ $asset_root == /* && $asset_root != / ]] || die "asset root must be an absolute non-root path"
[[ $digest =~ ^sha256:[0-9a-f]{64}$ ]] || die "image digest must be sha256:<64 lowercase hex characters>"

mkdir -p "$deploy_root/releases"
compose_file="$asset_root/compose.yml"
runtime_env="$deploy_root/runtime.env"
current_env="$deploy_root/release.env"
previous_env="$deploy_root/release.previous.env"
history_file="$deploy_root/releases/history.tsv"
acceptance_helper="$asset_root/verify-running-stack.py"
lock_file="$deploy_root/deploy.lock"
shared_staging_lock=/run/lock/hermes-staging-diagnostic.lock

[[ -f $compose_file && ! -L $compose_file ]] || die "missing or unsafe $compose_file"
[[ -f $runtime_env ]] || die "missing $runtime_env"
[[ -f $acceptance_helper && ! -L $acceptance_helper ]] || die "missing or unsafe $acceptance_helper"
[[ -f $runtime_env && ! -L $runtime_env && $(stat -c '%h:%u:%a' -- "$runtime_env") == "1:$EUID:600" ]] || \
  die "$runtime_env must not be group/world accessible, must be single-link owned by the deployment controller, and must have expected mode 0600"
python3 - "$runtime_env" <<'PY' || die "invalid runtime environment"
import pathlib, re, stat, sys

path = pathlib.Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()
if len(lines) != 3 or any("=" not in line for line in lines):
    raise SystemExit(1)
values = dict(line.split("=", 1) for line in lines)
if set(values) != {"HERMES_DATA_DIR", "HERMES_UID", "HERMES_GID"}:
    raise SystemExit(1)
data_dir = values["HERMES_DATA_DIR"]
parts = pathlib.PurePosixPath(data_dir).parts
if not re.fullmatch(r"/[A-Za-z0-9._/-]+", data_dir) or data_dir == "/" or ".." in parts:
    raise SystemExit(1)
for name in ("HERMES_UID", "HERMES_GID"):
    if not re.fullmatch(r"[1-9][0-9]{0,9}", values[name]):
        raise SystemExit(1)
data_path = pathlib.Path(data_dir)
try:
    metadata = data_path.lstat()
except OSError as exc:
    print(f"unsafe HERMES_DATA_DIR metadata: {exc}", file=sys.stderr)
    raise SystemExit(1)
if (
    not stat.S_ISDIR(metadata.st_mode)
    or data_path.is_symlink()
    or metadata.st_uid != int(values["HERMES_UID"])
    or metadata.st_gid != int(values["HERMES_GID"])
    or stat.S_IMODE(metadata.st_mode) & 0o077
):
    print("unsafe HERMES_DATA_DIR metadata", file=sys.stderr)
    raise SystemExit(1)
PY

command -v docker >/dev/null || die "docker is not installed"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is not installed"
command -v flock >/dev/null || die "flock is not installed"

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
  rollback_digest=$(sed -n 's/^HERMES_IMAGE=.*@\(sha256:[0-9a-f]\{64\}\)$/\1/p' "$previous_env")
  rollback_source=$(sed -n 's/^HERMES_SOURCE_SHA=\([0-9a-f]\{40\}\)$/\1/p' "$previous_env")
  [[ $rollback_digest == "$digest" ]] || die "rollback target digest mismatch"
  [[ $rollback_source == "$source_sha" ]] || die "rollback target source SHA mismatch"
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
