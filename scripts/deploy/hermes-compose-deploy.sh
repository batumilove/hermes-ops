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
  trap - EXIT INT TERM HUP
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

umask 077
[[ ! -L $deploy_root && -d $deploy_root ]] || die "deployment root must be a real directory"
releases_dir="$deploy_root/releases"
if [[ -e $releases_dir && ! -d $releases_dir ]]; then
  die "$releases_dir must be a directory if present"
fi
[[ -e $releases_dir && -L $releases_dir ]] && die "$releases_dir must not be a symlink"
mkdir -p "$deploy_root/releases"
pull_attempt_dir="$deploy_root/releases/pull-attempts"
mkdir -p "$pull_attempt_dir"
[[ -d $pull_attempt_dir && ! -L $pull_attempt_dir && $(stat -c '%u:%a' -- "$pull_attempt_dir") == "$EUID:700" ]] || \
  die "$pull_attempt_dir must be a private directory owned by the deployment controller"
[[ ! -L $deploy_root && -d $deploy_root ]] || die "deployment root must be a real directory"
compose_file="$asset_root/compose.yml"
runtime_env="$deploy_root/runtime.env"
current_env="$deploy_root/release.env"
previous_env="$deploy_root/release.previous.env"
history_file="$deploy_root/releases/history.tsv"
[[ ! -e $history_file || (! -L $history_file && -f $history_file) ]] || die "$history_file must be a real regular file if present"
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
command -v timeout >/dev/null || die "timeout is not installed"

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

# Cleanup is serialized by the deployment lock so it cannot race an active
# pull whose log exists before its final JSON sidecar.
python3 - "$pull_attempt_dir" <<'PY'
import pathlib
import shutil
import stat
import sys

root = pathlib.Path(sys.argv[1])
for entry in root.iterdir():
    if not (entry.name.startswith("pull-") and (entry.name.endswith(".json") or entry.name.endswith(".log") or entry.name.endswith(".json.tmp"))):
        continue
    # remove temporary and non-regular entries outright; complete pairs are
    # decided below against their regular-file counterpart
    is_regular = entry.is_file() and not entry.is_symlink()
    if entry.name.endswith(".json.tmp") or not is_regular:
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink(missing_ok=True)

# keep only entries that are members of a complete regular-file pair
logs = {entry for entry in root.iterdir() if entry.name.startswith("pull-") and entry.name.endswith(".log")}
jsons = {entry for entry in root.iterdir() if entry.name.startswith("pull-") and entry.name.endswith(".json")}
for log in logs:
    meta = log.with_suffix(".json")
    if meta not in jsons:
        log.unlink(missing_ok=True)
        jsons.discard(log.with_suffix(".json"))
for meta in list(jsons):
    log = meta.with_suffix(".log")
    if log not in logs:
        meta.unlink(missing_ok=True)
        jsons.discard(meta)
PY

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

restore_candidate_release() {
  local rc=$?
  if [[ ${candidate_published:-false} == true ]]; then
    if [[ $had_current == true ]]; then
      cp -p "$previous_env" "$current_env.restore"
      mv -f "$current_env.restore" "$current_env"
      verify_release >/dev/null 2>&1 || true
    else
      rm -f "$current_env"
    fi
  fi
  return "$rc"
}

restore_release_atomically() {
  if [[ $had_current == true ]]; then
    cp -p "$previous_env" "$current_env.restore"
    mv -f "$current_env.restore" "$current_env"
  else
    rm -f "$current_env"
  fi
}
terminate_after_restore() {
  local rc=$1
  restore_candidate_release
  trap - EXIT INT TERM HUP
  exit "$rc"
}
trap restore_candidate_release EXIT
trap 'terminate_after_restore 130' INT
trap 'terminate_after_restore 143' TERM
trap 'terminate_after_restore 129' HUP

if [[ $operation == rollback ]]; then
  [[ -s $previous_env ]] || die "no previous release is available for rollback"
  had_current=true
  rollback_digest=$(sed -n 's/^HERMES_IMAGE=.*@\(sha256:[0-9a-f]\{64\}\)$/\1/p' "$previous_env")
  rollback_source=$(sed -n 's/^HERMES_SOURCE_SHA=\([0-9a-f]\{40\}\)$/\1/p' "$previous_env")
  [[ $rollback_digest == "$digest" ]] || die "rollback target digest mismatch"
  [[ $rollback_source == "$source_sha" ]] || die "rollback target source SHA mismatch"
  rollback_from="$deploy_root/release.rollback-from.env"
  cp -p "$current_env" "$rollback_from"
  cp -p "$previous_env" "$current_env.rollback"
  mv -f "$current_env.rollback" "$current_env"
  had_current=true
  candidate_published=true
  previous_env_saved="$rollback_from"
  if verify_release; then
    cp -p "$rollback_from" "$previous_env.swap"
    mv -f "$previous_env.swap" "$previous_env"
    deployed_digest=$(sed -n 's/^HERMES_IMAGE=.*@\(sha256:[0-9a-f]\{64\}\)$/\1/p' "$current_env")
    record_evidence rollback "$deployed_digest"
    candidate_published=false
    trap - EXIT INT TERM HUP
    cp -p "$rollback_from" "$deploy_root/release.previous.env.swap"
    mv -f "$deploy_root/release.previous.env.swap" "$deploy_root/release.previous.env"
    rm -f "$rollback_from"
    printf 'Rollback complete: environment=%s digest=%s\n' "$environment" "$deployed_digest"
    exit 0
  fi
  previous_env="$rollback_from"
  record_evidence rollback-failed unknown
  candidate_published=false
  restore_release_atomically
  verify_release || true
  rm -f "$rollback_from"
  trap - EXIT INT TERM HUP
  die "rollback candidate failed health verification; original release was restored"
fi

candidate="$deploy_root/release.candidate.env"
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
candidate_published=true
mv -f "$candidate" "$current_env"

if [[ ${FAKE_PULL_INTERRUPT:-0} == 1 ]]; then
  exit 143
fi

# Pull before replacement so a registry/network failure cannot stop the current
# healthy container. The image reference is digest-pinned by validation above.
# This leaves twenty minutes inside the 50-minute controller budget for replacement,
# health verification, acceptance, evidence, and cleanup.
pull_rc=0
pull_started_epoch=$(date +%s)
pull_started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
pull_log=$(mktemp "$pull_attempt_dir/pull-${source_sha:0:12}-XXXXXXXX.log")
pull_attempt_id=$(basename "$pull_log" .log)
pull_result=success
timeout --signal=TERM --kill-after=10s 1800s docker compose \
  --project-name "hermes-$environment" \
  --env-file "$runtime_env" \
  --env-file "$current_env" \
  -f "$compose_file" \
  pull gateway >"$pull_log" 2>&1 || pull_rc=$?
cat "$pull_log" >&2
# bound individual evidence size: keep the most recent 4 MiB of pull output
log_size=$(stat -c '%s' -- "$pull_log")
if (( log_size > 4194304 )); then
  tail -c 4194304 -- "$pull_log" > "$pull_log.tail"
  mv -f "$pull_log.tail" "$pull_log"
fi
pull_finished_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
pull_duration_seconds=$(( $(date +%s) - pull_started_epoch ))
if (( pull_rc == 124 || pull_rc == 137 )); then
  pull_result=pull-timeout
elif (( pull_rc != 0 )); then
  pull_result=pull-failed
fi
diagnostic_rc=0
python3 - "$pull_attempt_dir/${pull_attempt_id}.json" "$environment" "$source_sha" \
  "$digest" "$pull_result" "$pull_rc" "$pull_started_at" "$pull_finished_at" \
  "$pull_duration_seconds" "$(basename "$pull_log")" <<'PY' || diagnostic_rc=$?
import json
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
if os.environ.get("FAKE_PULL_DIAGNOSTIC_FAIL") == "1":
    raise OSError("simulated pull diagnostic failure")
payload = {
    "schema_version": 1,
    "environment": sys.argv[2],
    "source_sha": sys.argv[3],
    "image_digest": sys.argv[4],
    "result": sys.argv[5],
    "exit_code": int(sys.argv[6]),
    "started_at": sys.argv[7],
    "finished_at": sys.argv[8],
    "duration_seconds": int(sys.argv[9]),
    "log_file": sys.argv[10],
}
temporary = path.with_suffix(".json.tmp")
temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(temporary, 0o600)
os.replace(temporary, path)
PY
if (( diagnostic_rc != 0 )); then
  restore_release_atomically
  rm -f "$pull_log" "$pull_attempt_dir/${pull_attempt_id}.json.tmp"
  die "pull diagnostics failed; current release was left untouched"
fi
# Retain only the twenty newest complete attempts. Metadata is published after
# the log, so a missing JSON sidecar is never accepted as a complete record.
retention_rc=0
HERMES_PULL_ATTEMPT_ID="$pull_attempt_id" python3 - "$pull_attempt_dir" <<'PY' || retention_rc=$?
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
if __import__("os").environ.get("FAKE_PULL_RETENTION_FAIL") == "1":
    raise OSError("simulated retention failure")
import os
import shutil
import stat as stat_module

complete = []
for entry in root.iterdir():
    if entry.name.startswith("pull-") and entry.name.endswith(".json"):
        meta = entry.lstat()
        if not stat_module.S_ISREG(meta.st_mode):
            stray_log = entry.with_suffix(".log")
            if stray_log.is_dir() and not stray_log.is_symlink():
                shutil.rmtree(stray_log)
            else:
                stray_log.unlink(missing_ok=True)
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink(missing_ok=True)
            continue
        log = entry.with_suffix(".log")
        try:
            log_meta = log.lstat()
        except (FileNotFoundError, OSError):
            entry.unlink(missing_ok=True)
            continue
        if not stat_module.S_ISREG(log_meta.st_mode):
            if log.is_dir() and not log.is_symlink():
                shutil.rmtree(log)
            else:
                log.unlink(missing_ok=True)
            entry.unlink(missing_ok=True)
            continue
        complete.append((meta.st_mtime_ns, entry))
complete.sort(reverse=True)
# evict oldest first, but never the attempt this invocation just published;
# when the current attempt is not among the newest twenty, keep it by evicting
# one additional oldest record so the bound stays at twenty-one pairs
current_stem = os.environ.get("HERMES_PULL_ATTEMPT_ID", "")
current_in_newest = any(record.stem == current_stem for _, record in complete[:20])
if current_in_newest:
    doomed = complete[20:]
else:
    doomed = [item for item in complete[20:] if item[1].stem != current_stem]
for _, record in doomed:
    if record.is_dir() and not record.is_symlink():
        shutil.rmtree(record)
    else:
        record.unlink(missing_ok=True)
    log = record.with_suffix(".log")
    if log.is_dir() and not log.is_symlink():
        shutil.rmtree(log)
    else:
        log.unlink(missing_ok=True)
PY
if (( retention_rc != 0 )); then
  restore_release_atomically
  rm -f "$pull_log" "$pull_attempt_dir/${pull_attempt_id}.json" "$pull_attempt_dir/${pull_attempt_id}.json.tmp"
  die "pull evidence retention failed; current release was left untouched"
fi
if (( pull_rc != 0 )); then
  restore_release_atomically
  if (( pull_rc == 124 || pull_rc == 137 )); then
    record_evidence pull-timeout "$digest"
    die "image pull timed out; current release was left untouched"
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
    candidate_published=false
    trap - EXIT INT TERM HUP
    printf 'Deployment complete: environment=%s source=%s digest=%s\n' \
      "$environment" "$source_sha" "$digest"
    exit 0
  fi
  failure_result="acceptance-failed"
fi

record_evidence "$failure_result" "$digest"
if [[ $had_current == true ]]; then
  restore_release_atomically
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
