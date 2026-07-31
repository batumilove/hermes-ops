#!/usr/bin/env python3
"""Root-owned, fail-closed deployment and soak lease controller.

The GitHub deployment principal may invoke only the ``apply`` command through a
reviewed sudoers rule. Soak acquire/release use a separately authorized
principal. ``emergency-clear`` is intentionally root-console-only and is never
present in workflow or sudoers automation.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator


IMAGE_REPOSITORY = "ghcr.io/batumilove/hermes-agent-deploy"
INSTALLED_CONTROLLER = Path("/usr/local/libexec/hermes-deployment-controller")
STATE_ROOT = Path("/var/lib/hermes-deployment-control")
MANIFEST_PATH = STATE_ROOT / "artifact-manifest.json"
CONFIG_PATH = Path("/etc/hermes-deployment-control/config.json")
SHA_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
ENV_RE = re.compile(r"batumi-(?:staging|production)")
IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]{1,159}")
RUN_ID_RE = re.compile(r"[1-9][0-9]{0,19}")
ATTEMPT_RE = re.compile(r"[1-9][0-9]{0,5}")
LEASE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{15,127}")
EXPECTED_ARTIFACTS = {
    "controller", "deployer", "compose", "acceptance", "installer", "sudoers"
}
EXPECTED_MODES = {
    "controller": 0o755,
    "deployer": 0o755,
    "compose": 0o644,
    "acceptance": 0o755,
    "installer": 0o755,
    "sudoers": 0o600,
}


class ControlError(RuntimeError):
    pass


class ValidationError(ControlError):
    pass


class LeaseConflict(ControlError):
    pass


class ArtifactViolation(ControlError):
    pass


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValidationError("invalid lease timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("invalid lease timestamp") from exc
    if parsed.tzinfo is None:
        raise ValidationError("lease timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid JSON state: {path}") from exc


def _default_runner(argv: list[str], timeout: int) -> int:
    completed = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        env={
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "TZ": "UTC",
            "HOME": "/root",
        },
        timeout=timeout,
        check=False,
    )
    return completed.returncode


class ControlPlane:
    def __init__(
        self,
        *,
        state_root: Path,
        manifest_path: Path,
        config_path: Path,
        runner: Callable[[list[str], int], int] = _default_runner,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        lock_timeout: float = 5.0,
        require_production_ownership: bool = False,
    ) -> None:
        self.state_root = Path(state_root)
        self.manifest_path = Path(manifest_path)
        self.config_path = Path(config_path)
        self.runner = runner
        self.now = now
        self.lock_timeout = lock_timeout
        self.require_production_ownership = require_production_ownership
        self.leases_root = self.state_root / "leases"
        self.locks_root = self.state_root / "locks"
        self.audit_path = self.state_root / "audit.jsonl"
        self._ensure_state()
        if self.require_production_ownership:
            self._verify_production_metadata()

    def _ensure_state(self) -> None:
        old_umask = os.umask(0o077)
        try:
            for path in (self.state_root, self.leases_root, self.locks_root):
                path.mkdir(parents=path == self.state_root, exist_ok=True, mode=0o700)
                st = path.lstat()
                if not stat.S_ISDIR(st.st_mode) or path.is_symlink():
                    raise ArtifactViolation(f"unsafe state directory: {path}")
                os.chmod(path, 0o700)
        finally:
            os.umask(old_umask)

    def _verify_production_metadata(self) -> None:
        for label, path, mode in (
            ("manifest", self.manifest_path, 0o600),
            ("config", self.config_path, 0o600),
        ):
            try:
                st = path.lstat()
            except OSError as exc:
                raise ArtifactViolation(f"unsafe production {label} metadata") from exc
            if (
                not stat.S_ISREG(st.st_mode)
                or path.is_symlink()
                or st.st_nlink != 1
                or st.st_uid != 0
                or st.st_gid != 0
                or stat.S_IMODE(st.st_mode) != mode
            ):
                raise ArtifactViolation(f"unsafe production {label} metadata")
        for label, path in (
            ("state root", self.state_root),
            ("leases root", self.leases_root),
            ("locks root", self.locks_root),
        ):
            st = path.lstat()
            if (
                not stat.S_ISDIR(st.st_mode)
                or path.is_symlink()
                or st.st_uid != 0
                or st.st_gid != 0
                or stat.S_IMODE(st.st_mode) != 0o700
            ):
                raise ArtifactViolation(f"unsafe production {label} metadata")
        if self.audit_path.exists():
            st = self.audit_path.lstat()
            if (
                not stat.S_ISREG(st.st_mode)
                or self.audit_path.is_symlink()
                or st.st_nlink != 1
                or st.st_uid != 0
                or st.st_gid != 0
                or stat.S_IMODE(st.st_mode) != 0o600
            ):
                raise ArtifactViolation("unsafe production audit metadata")

    def lease_path(self, environment: str) -> Path:
        self._validate_environment(environment)
        return self.leases_root / f"{environment}.json"

    def _lock_path(self, environment: str) -> Path:
        self._validate_environment(environment)
        return self.locks_root / f"{environment}.lock"

    @contextmanager
    def _control_lock(self, environment: str) -> Iterator[None]:
        path = self._lock_path(environment)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        deadline = time.monotonic() + self.lock_timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LeaseConflict(f"deployment control lock busy for {environment}")
                    time.sleep(0.02)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _atomic_json(self, path: Path, value: object) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(fd)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _audit(self, event: str, **fields: object) -> None:
        record = {"version": 1, "timestamp": _utc_text(self.now()), "event": event, **fields}
        data = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        fd = os.open(
            self.audit_path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(self.audit_path, 0o600)

    @staticmethod
    def _validate_environment(value: str) -> None:
        if not ENV_RE.fullmatch(value):
            raise ValidationError("invalid deployment environment")

    @staticmethod
    def _validate_identity(label: str, value: str) -> None:
        if not IDENTITY_RE.fullmatch(value):
            raise ValidationError(f"invalid {label}")

    @staticmethod
    def _validate_source_digest(source_sha: str, digest: str) -> None:
        if not SHA_RE.fullmatch(source_sha):
            raise ValidationError("invalid source SHA")
        if not DIGEST_RE.fullmatch(digest):
            raise ValidationError("invalid image digest")

    def _config(self) -> dict:
        value = _load_json(self.config_path)
        if not isinstance(value, dict) or set(value) != {"version", "environments"} or value["version"] != 1:
            raise ValidationError("invalid deployment control config schema")
        environments = value["environments"]
        if not isinstance(environments, dict):
            raise ValidationError("invalid environment configuration")
        return value

    def _deploy_root(self, environment: str) -> Path:
        environments = self._config()["environments"]
        record = environments.get(environment)
        if not isinstance(record, dict) or set(record) != {"deploy_root"}:
            raise ValidationError(f"unconfigured deployment environment: {environment}")
        raw = record["deploy_root"]
        if not isinstance(raw, str) or not raw.startswith("/") or raw == "/" or ".." in Path(raw).parts:
            raise ValidationError("unsafe deployment root")
        return Path(raw)

    def _verify_artifacts(self) -> dict[str, Path]:
        value = _load_json(self.manifest_path)
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "reviewed_commit", "reviewed_tree", "artifacts"}
            or value["version"] != 1
            or not isinstance(value["reviewed_commit"], str)
            or not SHA_RE.fullmatch(value["reviewed_commit"])
            or not isinstance(value["reviewed_tree"], str)
            or not SHA_RE.fullmatch(value["reviewed_tree"])
        ):
            raise ArtifactViolation("invalid artifact manifest schema")
        records = value["artifacts"]
        if not isinstance(records, dict) or set(records) != EXPECTED_ARTIFACTS:
            raise ArtifactViolation("invalid artifact manifest set")
        paths: dict[str, Path] = {}
        for name in sorted(EXPECTED_ARTIFACTS):
            record = records[name]
            if not isinstance(record, dict) or set(record) != {"path", "sha256"}:
                raise ArtifactViolation(f"invalid {name} artifact record")
            raw_path, expected = record["path"], record["sha256"]
            if not isinstance(raw_path, str) or not raw_path.startswith("/"):
                raise ArtifactViolation(f"invalid {name} artifact path")
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ArtifactViolation(f"invalid {name} artifact digest")
            path = Path(raw_path)
            try:
                st = path.lstat()
                data = path.read_bytes()
            except OSError as exc:
                raise ArtifactViolation(f"missing {name} artifact") from exc
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or path.is_symlink():
                raise ArtifactViolation(f"unsafe {name} artifact")
            if hashlib.sha256(data).hexdigest() != expected:
                raise ArtifactViolation(f"{name} artifact digest mismatch")
            if self.require_production_ownership:
                if st.st_uid != 0 or stat.S_IMODE(st.st_mode) != EXPECTED_MODES[name]:
                    raise ArtifactViolation(f"{name} artifact ownership or mode mismatch")
            paths[name] = path
        return paths

    def _read_lease(self, environment: str) -> dict | None:
        path = self.lease_path(environment)
        if not path.exists():
            return None
        value = _load_json(path)
        required = {
            "version", "lease_id", "kind", "environment", "owner", "controller",
            "operation", "source_sha", "image_digest", "authorization", "run_id",
            "run_attempt", "acquired_at", "expires_at",
        }
        if not isinstance(value, dict) or set(value) != required or value.get("version") != 1:
            raise ValidationError("invalid lease schema")
        if value["environment"] != environment:
            raise ValidationError("lease environment mismatch")
        return value

    def _expire_or_read(self, environment: str) -> dict | None:
        lease = self._read_lease(environment)
        if lease is None:
            return None
        if _parse_utc(lease["expires_at"]) > self.now():
            return lease
        self._audit(
            "lease-expired",
            environment=environment,
            lease_id=lease["lease_id"],
            kind=lease["kind"],
            owner=lease["owner"],
        )
        self.lease_path(environment).unlink()
        return None

    def apply(
        self,
        *,
        environment: str,
        operation: str,
        image: str,
        digest: str,
        source_sha: str,
        controller: str,
        authorization: str,
        run_id: str,
        run_attempt: str,
    ) -> int:
        self._validate_environment(environment)
        if operation not in {"deploy", "rollback"}:
            raise ValidationError("invalid deployment operation")
        if image != IMAGE_REPOSITORY:
            raise ValidationError("unexpected image repository")
        self._validate_source_digest(source_sha, digest)
        self._validate_identity("controller", controller)
        self._validate_identity("authorization", authorization)
        if not RUN_ID_RE.fullmatch(run_id) or not ATTEMPT_RE.fullmatch(run_attempt):
            raise ValidationError("invalid workflow run identity")
        artifacts = self._verify_artifacts()
        deploy_root = self._deploy_root(environment)
        with self._control_lock(environment):
            blocking = self._expire_or_read(environment)
            if blocking is not None:
                self._audit(
                    "deployment-blocked",
                    environment=environment,
                    operation=operation,
                    source_sha=source_sha,
                    image_digest=digest,
                    controller=controller,
                    authorization=authorization,
                    run_id=run_id,
                    run_attempt=run_attempt,
                    blocking_lease_id=blocking["lease_id"],
                    blocking_kind=blocking["kind"],
                )
                raise LeaseConflict(f"active {blocking['kind']} lease blocks {operation}")
            acquired = self.now()
            lease_id = f"deployment-{uuid.uuid4().hex}"
            lease = {
                "version": 1,
                "lease_id": lease_id,
                "kind": "deployment",
                "environment": environment,
                "owner": controller,
                "controller": controller,
                "operation": operation,
                "source_sha": source_sha,
                "image_digest": digest,
                "authorization": authorization,
                "run_id": run_id,
                "run_attempt": run_attempt,
                "acquired_at": _utc_text(acquired),
                "expires_at": _utc_text(acquired + timedelta(minutes=30)),
            }
            self._atomic_json(self.lease_path(environment), lease)
            self._audit("deployment-acquired", **lease)
            argv = [
                str(artifacts["deployer"]), operation, environment, image, digest,
                source_sha, str(deploy_root), str(artifacts["compose"].parent),
            ]
            result: int | None = None
            try:
                result = self.runner(argv, 1200)
                return result
            finally:
                lease_path = self.lease_path(environment)
                try:
                    try:
                        current = self._read_lease(environment)
                    except ValidationError:
                        # The path remains the transaction lease created above while
                        # this process holds the environment's exclusive lock.
                        lease_path.unlink(missing_ok=True)
                    else:
                        if current is not None and current["lease_id"] == lease_id:
                            lease_path.unlink()
                finally:
                    self._audit(
                        "deployment-released",
                        environment=environment,
                        lease_id=lease_id,
                        operation=operation,
                        source_sha=source_sha,
                        image_digest=digest,
                        result=result,
                    )

    def acquire_soak(
        self,
        *,
        environment: str,
        source_sha: str,
        digest: str,
        owner: str,
        authorization: str,
        ttl_seconds: int,
        lease_id: str,
    ) -> dict:
        self._validate_environment(environment)
        self._validate_source_digest(source_sha, digest)
        self._validate_identity("owner", owner)
        self._validate_identity("authorization", authorization)
        if not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 604800:
            raise ValidationError("soak lease TTL must be 1..604800 seconds")
        if not LEASE_ID_RE.fullmatch(lease_id):
            raise ValidationError("invalid lease ID")
        with self._control_lock(environment):
            blocking = self._expire_or_read(environment)
            if blocking is not None:
                raise LeaseConflict(f"active {blocking['kind']} lease already exists")
            acquired = self.now()
            lease = {
                "version": 1,
                "lease_id": lease_id,
                "kind": "soak",
                "environment": environment,
                "owner": owner,
                "controller": owner,
                "operation": "soak",
                "source_sha": source_sha,
                "image_digest": digest,
                "authorization": authorization,
                "run_id": None,
                "run_attempt": None,
                "acquired_at": _utc_text(acquired),
                "expires_at": _utc_text(acquired + timedelta(seconds=ttl_seconds)),
            }
            self._atomic_json(self.lease_path(environment), lease)
            self._audit("soak-acquired", **lease)
            return lease

    def release_soak(
        self,
        *,
        environment: str,
        lease_id: str,
        owner: str,
        authorization: str,
    ) -> None:
        self._validate_environment(environment)
        self._validate_identity("owner", owner)
        self._validate_identity("authorization", authorization)
        if not LEASE_ID_RE.fullmatch(lease_id):
            raise ValidationError("invalid lease ID")
        with self._control_lock(environment):
            lease = self._read_lease(environment)
            if lease is None or lease["kind"] != "soak":
                raise LeaseConflict("matching soak lease does not exist")
            if any((lease["lease_id"] != lease_id, lease["owner"] != owner, lease["authorization"] != authorization)):
                raise LeaseConflict("soak lease identity mismatch")
            self.lease_path(environment).unlink()
            self._audit(
                "soak-released", environment=environment, lease_id=lease_id,
                owner=owner, authorization=authorization,
            )

    def emergency_clear(
        self,
        *,
        environment: str,
        reason: str,
        authorization: str,
        effective_uid: int,
    ) -> None:
        self._validate_environment(environment)
        self._validate_identity("authorization", authorization)
        if effective_uid != 0:
            raise PermissionError("emergency clear requires direct root authorization")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@ -]{5,159}", reason):
            raise ValidationError("invalid emergency reason")
        with self._control_lock(environment):
            lease = self._read_lease(environment)
            if lease is None:
                raise LeaseConflict("no lease exists to clear")
            self.lease_path(environment).unlink()
            self._audit(
                "emergency-cleared", environment=environment,
                lease_id=lease["lease_id"], kind=lease["kind"], owner=lease["owner"],
                reason=reason, authorization=authorization,
            )

    def status(self, environment: str) -> dict | None:
        with self._control_lock(environment):
            return self._expire_or_read(environment)


def _production_plane() -> ControlPlane:
    return ControlPlane(
        state_root=STATE_ROOT,
        manifest_path=MANIFEST_PATH,
        config_path=CONFIG_PATH,
        require_production_ownership=True,
    )


def _assert_installed_root_entrypoint() -> None:
    actual = Path(os.path.realpath(sys.argv[0]))
    if actual != INSTALLED_CONTROLLER:
        raise PermissionError("run only the installed root-owned deployment controller")
    st = actual.lstat()
    if os.geteuid() != 0 or st.st_uid != 0 or st.st_gid != 0 or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) != 0o755:  # windows-footgun: ok
        raise PermissionError("installed deployment controller ownership or mode mismatch")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        _assert_installed_root_entrypoint()
        plane = _production_plane()
        command = args.pop(0) if args else ""
        if command == "apply" and len(args) == 9:
            return plane.apply(
                environment=args[0], operation=args[1], image=args[2], digest=args[3],
                source_sha=args[4], controller=args[5], authorization=args[6],
                run_id=args[7], run_attempt=args[8],
            )
        if command == "soak-acquire" and len(args) == 7:
            lease = plane.acquire_soak(
                environment=args[0], source_sha=args[1], digest=args[2], owner=args[3],
                authorization=args[4], ttl_seconds=int(args[5]), lease_id=args[6],
            )
            print(json.dumps(lease, sort_keys=True, separators=(",", ":")))
            return 0
        if command == "soak-release" and len(args) == 4:
            plane.release_soak(
                environment=args[0], lease_id=args[1], owner=args[2], authorization=args[3]
            )
            return 0
        if command == "status" and len(args) == 1:
            print(json.dumps(plane.status(args[0]), sort_keys=True, separators=(",", ":")))
            return 0
        if command == "emergency-clear" and len(args) == 3:
            plane.emergency_clear(
                environment=args[0], reason=args[1], authorization=args[2],
                effective_uid=os.geteuid(),  # windows-footgun: ok
            )
            return 0
        raise ValidationError("invalid command or argument count")
    except (ControlError, PermissionError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
