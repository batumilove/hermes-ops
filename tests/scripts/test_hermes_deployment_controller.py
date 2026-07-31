from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import stat
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parents[2]
CONTROLLER = REPO / "scripts" / "deploy" / "hermes_deployment_controller.py"
INSTALLER = REPO / "scripts" / "deploy" / "install-hermes-deployment-controller.sh"
SUDOERS = REPO / "deploy" / "deployment-control" / "hermes-deployment-controller.sudoers"
IMAGE = "ghcr.io/batumilove/hermes-agent-deploy"
SHA = "a" * 40
DIGEST = "sha256:" + "1" * 64
NOW = datetime(2026, 7, 29, 16, 0, tzinfo=UTC)


def _load():
    assert CONTROLLER.exists(), "deployment controller is not implemented"
    spec = importlib.util.spec_from_file_location("hermes_deployment_controller", CONTROLLER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def test_default_runner_preserves_root_docker_auth_home(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()
    observed: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        observed["argv"] = argv
        observed.update(kwargs)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    assert module._default_runner(["/reviewed/deployer", "deploy"], 1200) == 0
    assert observed["env"] == {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "HOME": "/root",
    }
    assert observed["argv"] == ["/reviewed/deployer", "deploy"]
    assert observed["stdin"] is subprocess.DEVNULL
    assert observed["timeout"] == 1200
    assert observed["check"] is False


def _fixture(tmp_path: Path, runner=None):
    module = _load()
    assets = tmp_path / "assets"
    assets.mkdir()
    artifact_paths = {}
    for name in ("controller", "deployer", "compose", "acceptance", "installer", "sudoers"):
        path = assets / name
        path.write_text(f"reviewed-{name}\n", encoding="utf-8")
        path.chmod(
            0o755 if name in {"controller", "deployer", "acceptance", "installer"}
            else 0o600 if name == "sudoers"
            else 0o644
        )
        artifact_paths[name] = path
    manifest = tmp_path / "manifest.json"
    _write_json(manifest, {
        "version": 1,
        "reviewed_commit": SHA,
        "reviewed_tree": "b" * 40,
        "artifacts": {
            name: {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for name, path in artifact_paths.items()
        },
    })
    deploy_root = tmp_path / "deploy-root"
    deploy_root.mkdir()
    config = tmp_path / "config.json"
    _write_json(config, {
        "version": 1,
        "environments": {"batumi-staging": {"deploy_root": str(deploy_root)}},
    })
    calls = []

    def default_runner(argv: list[str], timeout: int) -> int:
        calls.append((argv, timeout))
        return 0

    plane = module.ControlPlane(
        state_root=tmp_path / "state",
        manifest_path=manifest,
        config_path=config,
        runner=runner or default_runner,
        now=lambda: NOW,
        lock_timeout=0.2,
    )
    return module, plane, calls, artifact_paths, deploy_root


def _apply(plane, operation: str = "deploy") -> int:
    return plane.apply(
        environment="batumi-staging",
        operation=operation,
        image=IMAGE,
        digest=DIGEST,
        source_sha=SHA,
        controller="github-actions:deploy-compose.yml",
        authorization="batumilove",
        run_id="30470000000",
        run_attempt="1",
    )


def test_apply_records_exact_transaction_identity_while_running(tmp_path: Path) -> None:
    observed = {}

    def runner(argv: list[str], timeout: int) -> int:
        lease = json.loads((tmp_path / "state/leases/batumi-staging.json").read_text())
        observed.update(lease)
        observed["argv"] = argv
        observed["timeout"] = timeout
        return 0

    module, plane, _, artifact_paths, deploy_root = _fixture(tmp_path, runner)

    assert _apply(plane) == 0

    assert observed == {
        "version": 1,
        "lease_id": observed["lease_id"],
        "kind": "deployment",
        "environment": "batumi-staging",
        "owner": "github-actions:deploy-compose.yml",
        "controller": "github-actions:deploy-compose.yml",
        "operation": "deploy",
        "source_sha": SHA,
        "image_digest": DIGEST,
        "authorization": "batumilove",
        "run_id": "30470000000",
        "run_attempt": "1",
        "acquired_at": "2026-07-29T16:00:00Z",
        "expires_at": "2026-07-29T16:30:00Z",
        "argv": [
            str(artifact_paths["deployer"]),
            "deploy", "batumi-staging", IMAGE, DIGEST, SHA,
            str(deploy_root), str(artifact_paths["compose"].parent),
        ],
        "timeout": 1200,
    }
    assert not (tmp_path / "state/leases/batumi-staging.json").exists()
    audit = [json.loads(line) for line in (tmp_path / "state/audit.jsonl").read_text().splitlines()]
    assert [item["event"] for item in audit] == ["deployment-acquired", "deployment-released"]
    assert stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "state/audit.jsonl").stat().st_mode) == 0o600


def test_malformed_transaction_lease_is_removed_and_release_is_audited(
    tmp_path: Path,
) -> None:
    def runner(argv: list[str], timeout: int) -> int:
        (tmp_path / "state/leases/batumi-staging.json").write_text(
            "{malformed\n", encoding="utf-8"
        )
        return 0

    module, plane, _, _, _ = _fixture(tmp_path, runner)

    assert _apply(plane) == 0
    assert not plane.lease_path("batumi-staging").exists()
    audit = [json.loads(line) for line in plane.audit_path.read_text().splitlines()]
    assert [item["event"] for item in audit] == [
        "deployment-acquired",
        "deployment-released",
    ]
    assert audit[-1]["result"] == 0


def test_valid_replacement_lease_is_preserved_while_release_is_audited(
    tmp_path: Path,
) -> None:
    replacement_id = "deployment-replacement-1234567890"

    def runner(argv: list[str], timeout: int) -> int:
        path = tmp_path / "state/leases/batumi-staging.json"
        replacement = json.loads(path.read_text(encoding="utf-8"))
        replacement["lease_id"] = replacement_id
        _write_json(path, replacement)
        return 0

    module, plane, _, _, _ = _fixture(tmp_path, runner)

    assert _apply(plane) == 0
    assert json.loads(plane.lease_path("batumi-staging").read_text())["lease_id"] == replacement_id
    audit = [json.loads(line) for line in plane.audit_path.read_text().splitlines()]
    assert [item["event"] for item in audit] == [
        "deployment-acquired",
        "deployment-released",
    ]
    assert audit[-1]["result"] == 0


@pytest.mark.parametrize("operation", ["deploy", "rollback"])
def test_active_soak_lease_blocks_every_mutating_operation(tmp_path: Path, operation: str) -> None:
    module, plane, calls, _, _ = _fixture(tmp_path)
    lease = plane.acquire_soak(
        environment="batumi-staging",
        source_sha=SHA,
        digest=DIGEST,
        owner="fixed-grid-v3",
        authorization="batumilove",
        ttl_seconds=3600,
        lease_id="lease-1234567890abcdef",
    )

    with pytest.raises(module.LeaseConflict, match="active soak lease"):
        _apply(plane, operation)

    assert calls == []
    assert json.loads(plane.lease_path("batumi-staging").read_text()) == lease
    audit = [json.loads(line) for line in plane.audit_path.read_text().splitlines()]
    assert audit[-1]["event"] == "deployment-blocked"
    assert audit[-1]["blocking_lease_id"] == lease["lease_id"]


def test_expired_soak_is_audited_before_deployment_proceeds(tmp_path: Path) -> None:
    module, plane, calls, _, _ = _fixture(tmp_path)
    plane.acquire_soak(
        environment="batumi-staging",
        source_sha=SHA,
        digest=DIGEST,
        owner="fixed-grid-v3",
        authorization="batumilove",
        ttl_seconds=1,
        lease_id="lease-expired-123456",
    )
    plane.now = lambda: NOW + timedelta(seconds=2)

    assert _apply(plane) == 0

    assert len(calls) == 1
    events = [json.loads(line)["event"] for line in plane.audit_path.read_text().splitlines()]
    assert events == [
        "soak-acquired", "lease-expired", "deployment-acquired", "deployment-released"
    ]


def test_soak_acquire_persists_complete_identity_and_rejects_overlap(tmp_path: Path) -> None:
    module, plane, _, _, _ = _fixture(tmp_path)

    lease = plane.acquire_soak(
        environment="batumi-staging",
        source_sha=SHA,
        digest=DIGEST,
        owner="fixed-grid-v3",
        authorization="batumilove",
        ttl_seconds=7200,
        lease_id="lease-1234567890abcdef",
    )

    assert lease == {
        "version": 1,
        "lease_id": "lease-1234567890abcdef",
        "kind": "soak",
        "environment": "batumi-staging",
        "owner": "fixed-grid-v3",
        "controller": "fixed-grid-v3",
        "operation": "soak",
        "source_sha": SHA,
        "image_digest": DIGEST,
        "authorization": "batumilove",
        "run_id": None,
        "run_attempt": None,
        "acquired_at": "2026-07-29T16:00:00Z",
        "expires_at": "2026-07-29T18:00:00Z",
    }
    assert stat.S_IMODE(plane.lease_path("batumi-staging").stat().st_mode) == 0o600
    with pytest.raises(module.LeaseConflict):
        plane.acquire_soak(
            environment="batumi-staging", source_sha=SHA, digest=DIGEST,
            owner="other", authorization="other", ttl_seconds=60,
            lease_id="lease-other-12345678",
        )


def test_soak_release_requires_exact_lease_owner_and_authorization(tmp_path: Path) -> None:
    module, plane, _, _, _ = _fixture(tmp_path)
    plane.acquire_soak(
        environment="batumi-staging", source_sha=SHA, digest=DIGEST,
        owner="fixed-grid-v3", authorization="batumilove", ttl_seconds=3600,
        lease_id="lease-1234567890abcdef",
    )

    with pytest.raises(module.LeaseConflict):
        plane.release_soak(
            environment="batumi-staging", lease_id="lease-wrong-12345678",
            owner="fixed-grid-v3", authorization="batumilove",
        )
    with pytest.raises(module.LeaseConflict):
        plane.release_soak(
            environment="batumi-staging", lease_id="lease-1234567890abcdef",
            owner="fixed-grid-v3", authorization="other",
        )
    assert plane.lease_path("batumi-staging").exists()

    plane.release_soak(
        environment="batumi-staging", lease_id="lease-1234567890abcdef",
        owner="fixed-grid-v3", authorization="batumilove",
    )
    assert not plane.lease_path("batumi-staging").exists()


def test_emergency_clear_is_root_only_separate_and_audited(tmp_path: Path) -> None:
    module, plane, _, _, _ = _fixture(tmp_path)
    plane.acquire_soak(
        environment="batumi-staging", source_sha=SHA, digest=DIGEST,
        owner="fixed-grid-v3", authorization="batumilove", ttl_seconds=3600,
        lease_id="lease-1234567890abcdef",
    )

    with pytest.raises(PermissionError):
        plane.emergency_clear(
            environment="batumi-staging", reason="incident-42",
            authorization="root-console", effective_uid=1000,
        )
    plane.emergency_clear(
        environment="batumi-staging", reason="incident-42",
        authorization="root-console", effective_uid=0,
    )

    assert not plane.lease_path("batumi-staging").exists()
    event = json.loads(plane.audit_path.read_text().splitlines()[-1])
    assert event["event"] == "emergency-cleared"
    assert event["reason"] == "incident-42"
    assert event["authorization"] == "root-console"


@pytest.mark.parametrize("artifact", ["deployer", "compose", "acceptance"])
def test_tampered_root_owned_artifact_blocks_before_deployer(
    tmp_path: Path, artifact: str
) -> None:
    module, plane, calls, artifacts, _ = _fixture(tmp_path)
    artifacts[artifact].write_text("tampered\n", encoding="utf-8")

    with pytest.raises(module.ArtifactViolation, match=artifact):
        _apply(plane)

    assert calls == []
    assert not plane.lease_path("batumi-staging").exists()


def test_production_mode_rejects_unsafe_manifest_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, plane, _, _, _ = _fixture(tmp_path)
    real_lstat = Path.lstat

    def fake_lstat(path: Path):
        metadata = real_lstat(path)
        if path == plane.manifest_path:
            return SimpleNamespace(
                st_mode=stat.S_IFREG | 0o644,
                st_uid=0,
                st_gid=0,
                st_nlink=1,
            )
        return metadata

    monkeypatch.setattr(Path, "lstat", fake_lstat)

    with pytest.raises(module.ArtifactViolation, match="manifest metadata"):
        module.ControlPlane(
            state_root=plane.state_root,
            manifest_path=plane.manifest_path,
            config_path=plane.config_path,
            require_production_ownership=True,
        )


def test_concurrent_deployment_cannot_enter_while_transaction_is_running(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def runner(argv: list[str], timeout: int) -> int:
        entered.set()
        assert release.wait(timeout=5)
        return 0

    module, first, _, _, _ = _fixture(tmp_path, runner)
    second = module.ControlPlane(
        state_root=first.state_root,
        manifest_path=first.manifest_path,
        config_path=first.config_path,
        runner=lambda argv, timeout: 0,
        now=lambda: NOW,
        lock_timeout=0.1,
    )
    errors = []

    def run_first() -> None:
        try:
            _apply(first)
        except BaseException as exc:  # noqa: BLE001 - surfaced through errors below
            errors.append(exc)

    thread = threading.Thread(target=run_first, daemon=True)
    thread.start()
    assert entered.wait(timeout=5)

    with pytest.raises(module.LeaseConflict, match="control lock"):
        _apply(second)

    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []


def _run_installer_function(
    tmp_path: Path, function_call: str, available_tools: tuple[str, ...]
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in available_tools:
        tool = bin_dir / name
        tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        tool.chmod(0o755)
    command = (
        f"source {shlex.quote(str(INSTALLER))}; "
        f"PATH={shlex.quote(str(bin_dir))}; {function_call}"
    )
    return subprocess.run(
        ["bash", "-c", command], text=True, capture_output=True, timeout=10
    )


@pytest.mark.parametrize("missing", ["visudo", "ss", "getfacl"])
def test_installer_authorization_tools_fail_closed_when_missing(
    tmp_path: Path, missing: str
) -> None:
    tools = tuple(name for name in ("visudo", "ss", "getfacl") if name != missing)

    completed = _run_installer_function(tmp_path, "require_authorization_tools", tools)

    assert completed.returncode != 0
    assert f"{missing} unavailable" in completed.stderr


def test_installer_validates_exact_digest_bound_sudoers_candidate(tmp_path: Path) -> None:
    visudo = shutil.which("visudo")
    if visudo is None:
        pytest.skip("visudo unavailable")
    candidate = tmp_path / "sudoers"
    candidate.write_text(
        SUDOERS.read_text(encoding="utf-8").replace("__CONTROLLER_SHA256__", "0" * 64),
        encoding="utf-8",
    )
    candidate.chmod(0o440)
    command = (
        f"source {shlex.quote(str(INSTALLER))}; "
        f"validate_sudoers_candidate {shlex.quote(str(candidate))}"
    )

    completed = subprocess.run(
        ["bash", "-c", command], text=True, capture_output=True, timeout=10,
        env={**os.environ, "PATH": os.path.dirname(visudo) + ":/usr/bin:/bin"},
    )

    assert completed.returncode == 0, completed.stderr


def test_sudoers_separates_deploy_soak_and_root_emergency_authority() -> None:
    records: dict[str, set[str]] = {}
    for raw_line in SUDOERS.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        principal, command_list = line.split(" ALL=(root) NOPASSWD: ", 1)
        commands = {
            entry.split(" /usr/local/libexec/hermes-deployment-controller ", 1)[1]
            for entry in command_list.split(", ")
        }
        assert all(entry.startswith("sha256:__CONTROLLER_SHA256__ ") for entry in command_list.split(", "))
        records[principal] = commands

    assert records == {
        "%hermes-deploy": {"apply *"},
        "%hermes-soak": {"soak-acquire *", "soak-release *", "status *"},
    }
