#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage:
  install-hermes-deployment-controller --stage REPO_ROOT REVIEWED_COMMIT REVIEWED_TREE INSTALLER_SHA256 STAGING_DEPLOY_ROOT PRODUCTION_DEPLOY_ROOT_OR_DASH
  install-hermes-deployment-controller --authorize

--stage installs exact git blobs as root-owned artifacts but does not grant sudo.
--authorize verifies every installed byte and refuses while any hermes-deploy
principal retains docker group access, then installs the reviewed sudoers rules.
EOF
  exit 64
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_authorization_tools() {
  local tool
  for tool in visudo ss getfacl; do
    command -v "$tool" >/dev/null || die "$tool unavailable"
  done
}

validate_sudoers_candidate() {
  [[ $# -eq 1 ]] || die "sudoers candidate path required"
  # Parsing the exact substituted candidate is the authoritative live capability
  # gate for Digest_Spec support; it is stronger than brittle version parsing.
  visudo -c -f "$1"
}

# Keep the host-only installer helpers executable in hermetic tests without
# weakening the root and installed-copy gates on normal execution.
if [[ ${BASH_SOURCE[0]} != "$0" ]]; then
  return 0
fi

umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

[[ $EUID -eq 0 ]] || die "root required"
mode=${1:-}
installed_installer=/usr/local/sbin/install-hermes-deployment-controller
installed_controller=/usr/local/libexec/hermes-deployment-controller
asset_root=/usr/local/libexec/hermes-deployment
installed_deployer=$asset_root/hermes-compose-deploy.sh
installed_acceptance=$asset_root/verify-running-stack.py
installed_compose=$asset_root/compose.yml
state_root=/var/lib/hermes-deployment-control
manifest=$state_root/artifact-manifest.json
config_root=/etc/hermes-deployment-control
config=$config_root/config.json
staged_root=$state_root/staged
staged_sudoers=$staged_root/hermes-deployment-controller.sudoers
sudoers_target=/etc/sudoers.d/hermes-deployment-controller

[[ $(readlink -f -- "$0") == "$installed_installer" ]] || \
  die "run only the externally verified root-owned installer copy"
[[ $(stat -c '%U:%G:%a:%h' -- "$installed_installer") == root:root:755:1 ]] || \
  die "installer ownership or mode mismatch"

atomic_install() {
  local source=$1 target=$2 mode_bits=$3 directory temporary
  directory=$(dirname -- "$target")
  temporary="$directory/.hermes-deployment.$$.tmp"
  install -o root -g root -m "$mode_bits" -- "$source" "$temporary"
  sync -f "$temporary"
  mv -fT -- "$temporary" "$target"
  sync -f "$directory"
}

materialize_reviewed_blob() {
  local repo_path=$1 target=$2 mode_bits=$3 temporary expected actual
  temporary="$staged_root/.blob.$$.${RANDOM}"
  /usr/bin/git --no-replace-objects -c safe.directory="$repo_root" -C "$repo_root" \
    cat-file blob "$reviewed_commit:$repo_path" >"$temporary"
  expected=$(/usr/bin/git --no-replace-objects -c safe.directory="$repo_root" -C "$repo_root" \
    cat-file blob "$reviewed_commit:$repo_path" | sha256sum | cut -d' ' -f1)
  actual=$(sha256sum -- "$temporary" | cut -d' ' -f1)
  [[ $actual == "$expected" ]] || die "reviewed blob mismatch: $repo_path"
  atomic_install "$temporary" "$target" "$mode_bits"
  rm -f -- "$temporary"
}

validate_deploy_root() {
  local value=$1
  [[ $value == /* && $value != / && $value != *'/../'* && $value != *'/..' ]] || \
    die "unsafe deployment root: $value"
}

validate_host_deploy_root() {
  local root=$1 runtime=$1/runtime.env
  [[ -d $root && ! -L $root && $(stat -c '%U:%G:%a' -- "$root") == root:root:700 ]] || \
    die "deployment root must be a root-owned mode-0700 directory: $root"
  [[ -f $runtime && ! -L $runtime && $(stat -c '%U:%G:%a:%h' -- "$runtime") == root:root:600:1 ]] || \
    die "runtime.env must be a root-owned mode-0600 regular file: $runtime"
}

if [[ $mode == --stage ]]; then
  [[ $# -eq 7 ]] || usage
  repo_root=$(realpath -e -- "$2")
  reviewed_commit=$3
  reviewed_tree=$4
  installer_digest=$5
  staging_root=$6
  production_root=$7
  [[ $reviewed_commit =~ ^[0-9a-f]{40}$ ]] || die "invalid reviewed commit"
  [[ $reviewed_tree =~ ^[0-9a-f]{40}$ ]] || die "invalid reviewed tree"
  [[ $installer_digest =~ ^[0-9a-f]{64}$ ]] || die "invalid installer digest"
  validate_deploy_root "$staging_root"
  validate_host_deploy_root "$staging_root"
  if [[ $production_root != - ]]; then
    validate_deploy_root "$production_root"
    validate_host_deploy_root "$production_root"
  fi
  [[ $(sha256sum -- "$installed_installer" | cut -d' ' -f1) == "$installer_digest" ]] || \
    die "installed installer digest mismatch"
  reviewed_installer_digest=$(/usr/bin/git --no-replace-objects -c safe.directory="$repo_root" -C "$repo_root" \
    cat-file blob "$reviewed_commit:scripts/deploy/install-hermes-deployment-controller.sh" | sha256sum | cut -d' ' -f1)
  [[ $reviewed_installer_digest == "$installer_digest" ]] || \
    die "installer is not bound to reviewed commit"
  [[ $(/usr/bin/git --no-replace-objects -c safe.directory="$repo_root" -C "$repo_root" \
    rev-parse "$reviewed_commit^{tree}") == "$reviewed_tree" ]] || die "reviewed tree mismatch"

  install -o root -g root -m 0755 -d /usr/local/libexec "$asset_root"
  install -o root -g root -m 0700 -d "$state_root" "$staged_root" "$state_root/leases" "$state_root/locks"
  install -o root -g root -m 0755 -d "$config_root"

  materialize_reviewed_blob scripts/deploy/hermes_deployment_controller.py "$installed_controller" 0755
  materialize_reviewed_blob scripts/deploy/hermes-compose-deploy.sh "$installed_deployer" 0755
  materialize_reviewed_blob scripts/deploy/verify_running_stack.py "$installed_acceptance" 0755
  materialize_reviewed_blob deploy/compose.yml "$installed_compose" 0644
  materialize_reviewed_blob deploy/deployment-control/hermes-deployment-controller.sudoers "$staged_sudoers" 0600

  python3 - "$config.tmp" "$staging_root" "$production_root" <<'PY'
import json, pathlib, sys
output, staging, production = sys.argv[1:]
environments = {"batumi-staging": {"deploy_root": staging}}
if production != "-":
    environments["batumi-production"] = {"deploy_root": production}
pathlib.Path(output).write_text(
    json.dumps({"version": 1, "environments": environments}, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
PY
  chown root:root "$config.tmp"
  chmod 0600 "$config.tmp"
  sync -f "$config.tmp"
  mv -fT -- "$config.tmp" "$config"
  sync -f "$config_root"

  python3 - "$manifest.tmp" "$reviewed_commit" "$reviewed_tree" \
    "$installed_controller" "$installed_deployer" "$installed_compose" "$installed_acceptance" \
    "$installed_installer" "$staged_sudoers" <<'PY'
import hashlib, json, pathlib, stat, sys
output, reviewed_commit, reviewed_tree, controller, deployer, compose, acceptance, installer, sudoers = sys.argv[1:]
paths = {
    "controller": controller,
    "deployer": deployer,
    "compose": compose,
    "acceptance": acceptance,
    "installer": installer,
    "sudoers": sudoers,
}
artifacts = {}
for name, raw in paths.items():
    path = pathlib.Path(raw)
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != 0 or st.st_gid != 0:
        raise SystemExit(f"unsafe installed {name}")
    artifacts[name] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
payload = {
    "version": 1,
    "reviewed_commit": reviewed_commit,
    "reviewed_tree": reviewed_tree,
    "artifacts": artifacts,
}
pathlib.Path(output).write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
PY
  chown root:root "$manifest.tmp"
  chmod 0600 "$manifest.tmp"
  sync -f "$manifest.tmp"
  mv -fT -- "$manifest.tmp" "$manifest"
  sync -f "$state_root"
  rm -f -- "$sudoers_target"
  printf '%s\n' "Staged exact reviewed deployment-control artifacts for commit $reviewed_commit tree $reviewed_tree."
  printf '%s\n' 'Authorization remains disabled. Remove docker group access from every hermes-deploy principal, verify host state, then run --authorize separately.'
  exit 0
fi

[[ $mode == --authorize && $# -eq 1 ]] || usage
require_authorization_tools
getent group hermes-deploy >/dev/null || die "hermes-deploy group missing"
getent group hermes-soak >/dev/null || die "hermes-soak group missing"
[[ -f $manifest && ! -L $manifest && $(stat -c '%U:%G:%a:%h' -- "$manifest") == root:root:600:1 ]] || \
  die "artifact manifest missing or unsafe"
[[ -f $config && ! -L $config && $(stat -c '%U:%G:%a:%h' -- "$config") == root:root:600:1 ]] || \
  die "deployment config missing or unsafe"
[[ -f $staged_sudoers && ! -L $staged_sudoers && $(stat -c '%U:%G:%a:%h' -- "$staged_sudoers") == root:root:600:1 ]] || \
  die "staged sudoers missing or unsafe"

controller_digest=$(python3 - "$manifest" <<'PY'
import hashlib, json, pathlib, stat, sys
manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if set(manifest) != {"version", "reviewed_commit", "reviewed_tree", "artifacts"} or manifest["version"] != 1:
    raise SystemExit("invalid manifest schema")
import re
if not re.fullmatch(r"[0-9a-f]{40}", manifest["reviewed_commit"]):
    raise SystemExit("invalid reviewed commit")
if not re.fullmatch(r"[0-9a-f]{40}", manifest["reviewed_tree"]):
    raise SystemExit("invalid reviewed tree")
expected = {
    "controller": 0o755,
    "deployer": 0o755,
    "compose": 0o644,
    "acceptance": 0o755,
    "installer": 0o755,
    "sudoers": 0o600,
}
if set(manifest["artifacts"]) != set(expected):
    raise SystemExit("invalid manifest artifact set")
for name, mode in expected.items():
    record = manifest["artifacts"][name]
    if set(record) != {"path", "sha256"}:
        raise SystemExit(f"invalid {name} record")
    path = pathlib.Path(record["path"])
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != 0 or st.st_gid != 0:
        raise SystemExit(f"unsafe {name}")
    if stat.S_IMODE(st.st_mode) != mode:
        raise SystemExit(f"wrong {name} mode")
    if hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
        raise SystemExit(f"{name} digest mismatch")
print(manifest["artifacts"]["controller"]["sha256"])
PY
)
[[ $controller_digest =~ ^[0-9a-f]{64}$ ]] || die "invalid controller digest"

# The workflow account must not retain a second, unmediated path to the Docker
# daemon. Refuse authorization until all users in hermes-deploy (supplementary
# or primary group membership) have lost docker group access.
deploy_gid=$(getent group hermes-deploy | cut -d: -f3)
mapfile -t deploy_users < <(
  {
    getent group hermes-deploy | cut -d: -f4 | tr ',' '\n'
    getent passwd | awk -F: -v gid="$deploy_gid" '$4 == gid {print $1}'
  } | sed '/^$/d' | sort -u
)
for user in "${deploy_users[@]}"; do
  if id -nG "$user" | tr ' ' '\n' | grep -Fxq docker; then
    die "docker group access remains for hermes-deploy principal: $user"
  fi
  user_uid=$(id -u "$user")
  user_home=$(getent passwd "$user" | cut -d: -f6)
  for rootless_socket in "/run/user/$user_uid/docker.sock" "$user_home/.docker/run/docker.sock"; do
    [[ ! -S $rootless_socket ]] || \
      die "rootless Docker socket remains for hermes-deploy principal: $user"
  done
done
if command -v ss >/dev/null && ss -H -ltn | awk '{print $4}' | grep -Eq ':(2375|2376)$'; then
  die "Docker TCP listener is exposed on port 2375 or 2376"
fi
if [[ -S /var/run/docker.sock ]]; then
  [[ $(stat -c '%U:%G:%a' -- /var/run/docker.sock) == root:docker:660 ]] || \
    die "Docker socket ownership/mode is not root:docker:660"
  if command -v getfacl >/dev/null; then
    if getfacl -cp /var/run/docker.sock | \
      grep -Ev '^(#|user::|group::|mask::|other::|$)' >/dev/null; then
      die "Docker socket has named ACL entries"
    fi
  fi
fi

[[ ! -e $state_root/leases/batumi-staging.json && ! -e $state_root/leases/batumi-production.json ]] || \
  die "cannot authorize while a deployment-control lease exists"
temporary=/etc/sudoers.d/.hermes-deployment-controller.$$.tmp
trap 'rm -f -- "$temporary"' EXIT
python3 - "$staged_sudoers" "$temporary" "$controller_digest" <<'PY'
from pathlib import Path
import sys
source = Path(sys.argv[1])
target = Path(sys.argv[2])
digest = sys.argv[3]
text = source.read_text(encoding="utf-8")
if text.count("__CONTROLLER_SHA256__") != 4:
    raise SystemExit("sudoers controller digest placeholder count mismatch")
target.write_text(text.replace("__CONTROLLER_SHA256__", digest), encoding="utf-8")
PY
chown root:root "$temporary"
chmod 0440 "$temporary"
validate_sudoers_candidate "$temporary"
mv -fT -- "$temporary" "$sudoers_target"
sync -f /etc/sudoers.d
trap - EXIT
printf '%s\n' 'Authorized digest-bound deployment apply and separately scoped soak lease commands.'
printf '%s\n' 'Emergency clear remains root-console-only and is intentionally absent from sudoers.'
