from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "deploy" / "hermes-compose-deploy.sh"
ACCEPTANCE = REPO / "scripts" / "deploy" / "verify_running_stack.py"
COMPOSE = REPO / "deploy" / "compose.yml"
IMAGE = "ghcr.io/batumilove/hermes-agent-deploy"
SHA = "a" * 40
DIGEST_ONE = "sha256:" + "1" * 64
DIGEST_TWO = "sha256:" + "2" * 64


def _fake_docker(bin_dir: Path) -> None:
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "${FAKE_DOCKER_LOG:?}"
if [[ ${1:-} == compose && ${2:-} == version ]]; then
  echo 'Docker Compose version v2.40.0'
  exit 0
fi
if [[ ${1:-} == inspect ]]; then
  if [[ $* == *Health.Status* ]]; then
    if [[ -e ${FAKE_DOCKER_FAIL_ONCE:-/nonexistent} ]]; then
      rm -f "$FAKE_DOCKER_FAIL_ONCE"
      echo unhealthy
    else
      echo healthy
    fi
  elif [[ $* == *State.Running* ]]; then
    echo true
  else
    restart_count=0
    if [[ -e ${FAKE_ACCEPTANCE_FAIL_ONCE:-/nonexistent} ]]; then
      rm -f "$FAKE_ACCEPTANCE_FAIL_ONCE"
      restart_count=1
    fi
    python3 - "$FAKE_DEPLOY_ROOT/release.env" "$restart_count" <<'PY'
import json
import sys
from pathlib import Path

release = dict(
    line.split("=", 1)
    for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
)
print(json.dumps([{
    "Config": {
        "Image": release["HERMES_IMAGE"],
        "Labels": {"org.opencontainers.image.revision": release["HERMES_SOURCE_SHA"]},
        "Env": [
            f"HERMES_SOURCE_SHA={release['HERMES_SOURCE_SHA']}",
            f"HERMES_DEPLOY_ENV={release['HERMES_DEPLOY_ENV']}",
        ],
    },
    "State": {
        "Running": True,
        "Health": {"Status": "healthy"},
        "Pid": 1234,
        "StartedAt": "2026-07-24T16:00:00Z",
    },
    "RestartCount": int(sys.argv[2]),
}]))
PY
  fi
  exit 0
fi
if [[ ${1:-} == exec ]]; then
  echo 'up (pid 1234) 1 seconds'
  exit 0
fi
exit 0
"""
    )
    docker.chmod(0o755)
    flock = bin_dir / "flock"
    flock.write_text("#!/usr/bin/env bash\nexit 0\n")
    flock.chmod(0o755)
    stat = bin_dir / "stat"
    stat.write_text(
        """#!/usr/bin/env python3
import os
import sys

path = sys.argv[-1]
mode = os.stat(path).st_mode & 0o777
print(f"{mode:o}")
"""
    )
    stat.chmod(0o755)
    timeout = bin_dir / "timeout"
    timeout.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nshift\nexec \"$@\"\n"
    )
    timeout.chmod(0o755)


def _run(root: Path, bin_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["FAKE_DOCKER_LOG"] = str(root / "docker.log")
    env["FAKE_DOCKER_FAIL_ONCE"] = str(root / "fail-once")
    env["FAKE_ACCEPTANCE_FAIL_ONCE"] = str(root / "acceptance-fail-once")
    env["FAKE_DEPLOY_ROOT"] = str(root)
    return subprocess.run(
        ["bash", str(SCRIPT), *args, str(root)],
        text=True,
        capture_output=True,
        env=env,
        timeout=20,
    )


def _prepare(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "deploy-root"
    root.mkdir()
    (root / "compose.yml").write_text(COMPOSE.read_text())
    (root / "verify-running-stack.py").write_text(ACCEPTANCE.read_text(encoding="utf-8"), encoding="utf-8")
    runtime = root / "runtime.env"
    runtime.write_text(
        f"HERMES_DATA_DIR={tmp_path / 'data'}\nHERMES_UID=1000\nHERMES_GID=1000\n"
    )
    runtime.chmod(0o600)
    (tmp_path / "data").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_docker(bin_dir)
    return root, bin_dir


def test_deploy_uses_immutable_digest_and_records_evidence(tmp_path: Path) -> None:
    root, bin_dir = _prepare(tmp_path)

    result = _run(root, bin_dir, "deploy", "staging", IMAGE, DIGEST_ONE, SHA)

    assert result.returncode == 0, result.stderr
    release = (root / "release.env").read_text()
    assert f"HERMES_IMAGE={IMAGE}@{DIGEST_ONE}" in release
    assert f"HERMES_SOURCE_SHA={SHA}" in release
    history = (root / "releases" / "history.tsv").read_text()
    assert "\tdeployed\tstaging\t" in history
    assert DIGEST_ONE in history
    log = (root / "docker.log").read_text()
    assert "pull gateway" in log
    assert "up -d --wait --wait-timeout 300 --remove-orphans" in log


def test_failed_health_check_restores_previous_release(tmp_path: Path) -> None:
    root, bin_dir = _prepare(tmp_path)
    first = _run(root, bin_dir, "deploy", "staging", IMAGE, DIGEST_ONE, SHA)
    assert first.returncode == 0, first.stderr
    (root / "fail-once").touch()

    failed = _run(root, bin_dir, "deploy", "staging", IMAGE, DIGEST_TWO, SHA)

    assert failed.returncode != 0
    assert "previous release restored" in failed.stderr
    assert DIGEST_ONE in (root / "release.env").read_text()
    history = (root / "releases" / "history.tsv").read_text()
    assert "\thealth-failed\tstaging\t" in history
    assert "\tautomatic-rollback\tstaging\t" in history


def test_failed_acceptance_restores_previous_release(tmp_path: Path) -> None:
    root, bin_dir = _prepare(tmp_path)
    first = _run(root, bin_dir, "deploy", "staging", IMAGE, DIGEST_ONE, SHA)
    assert first.returncode == 0, first.stderr
    (root / "acceptance-fail-once").touch()

    failed = _run(root, bin_dir, "deploy", "staging", IMAGE, DIGEST_TWO, SHA)

    assert failed.returncode != 0
    assert "previous release restored" in failed.stderr
    assert DIGEST_ONE in (root / "release.env").read_text()
    history = (root / "releases" / "history.tsv").read_text()
    assert "\tacceptance-failed\tstaging\t" in history
    assert "\tautomatic-rollback\tstaging\t" in history


def test_manual_rollback_swaps_current_and_previous(tmp_path: Path) -> None:
    root, bin_dir = _prepare(tmp_path)
    assert _run(root, bin_dir, "deploy", "staging", IMAGE, DIGEST_ONE, SHA).returncode == 0
    assert _run(root, bin_dir, "deploy", "staging", IMAGE, DIGEST_TWO, SHA).returncode == 0

    rolled_back = _run(root, bin_dir, "rollback", "staging", IMAGE, "-", SHA)

    assert rolled_back.returncode == 0, rolled_back.stderr
    assert DIGEST_ONE in (root / "release.env").read_text()
    assert DIGEST_TWO in (root / "release.previous.env").read_text()


def test_rejects_mutable_or_untrusted_image_reference(tmp_path: Path) -> None:
    root, bin_dir = _prepare(tmp_path)

    mutable = _run(root, bin_dir, "deploy", "staging", IMAGE, "latest", SHA)
    foreign = _run(
        root,
        bin_dir,
        "deploy",
        "staging",
        "ghcr.io/example/other",
        DIGEST_ONE,
        SHA,
    )

    assert mutable.returncode != 0
    assert "image digest must be" in mutable.stderr
    assert foreign.returncode != 0
    assert "unexpected image repository" in foreign.stderr


def test_rejects_runtime_env_with_unsafe_permissions(tmp_path: Path) -> None:
    root, bin_dir = _prepare(tmp_path)
    (root / "runtime.env").chmod(0o644)

    result = _run(root, bin_dir, "deploy", "staging", IMAGE, DIGEST_ONE, SHA)

    assert result.returncode != 0
    assert "must not be group/world accessible" in result.stderr
