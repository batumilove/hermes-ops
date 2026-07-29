#!/usr/bin/env python3
"""Fixed-target, restore-only crash-recoverable staging socket diagnostic."""

from __future__ import annotations

import fcntl
import hashlib
import io
import ipaddress
import json
import ctypes
import errno
import os
import re

import selectors
import signal
import socket
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, NamedTuple

DEPLOY_HOST = "hermes-staging-01"
DEPLOY_ROOT = Path("/opt/hermes-compose/staging")
DATA_ROOT = Path("/home/hermes-staging/.hermes-staging")
STATE_ROOT = Path("/var/lib/hermes-staging-diagnostics")
TRANSACTION_ROOT = STATE_ROOT / "transactions"
LOCK_PATH = Path("/run/lock/hermes-staging-diagnostic.lock")
CRASH_TOKEN_PATH = Path("/run/hermes-staging-diagnostic-crash-token.json")
INSTALLED_HELPER_PATH = Path("/usr/local/libexec/hermes-staging-diagnostic")
RUNTIME_UID = 1001
RUNTIME_GID = 1001
SUDO_UID = 1002
CONTAINER = "hermes-batumi-staging-gateway"
ENVIRONMENT = "batumi-staging"
DOCKER = "/usr/bin/docker"
CONTAINER_PYTHON = "/opt/hermes/.venv/bin/python"
MAX_REQUEST_BYTES = 4096
MAX_CONFIG_BYTES = 1024 * 1024
MAX_METADATA_BYTES = 4096
MAX_COMMAND_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024
FORWARD_DEADLINE_SECONDS = 600
RESTORE_DEADLINE_SECONDS = 240
TOTAL_DEADLINE_SECONDS = FORWARD_DEADLINE_SECONDS + RESTORE_DEADLINE_SECONDS
LOCK_WAIT_SECONDS = 60
COMMAND_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC"}
STATE_ORDER = ("PREPARED", "ARMED", "MUTATED", "ENABLED", "OBSERVING", "RESTORING", "RESTORED")
TERMINAL_STATES = {"RESTORED", "ABORTED"}
_TRANSITIONS = {a: b for a, b in zip(STATE_ORDER, STATE_ORDER[1:])}
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]{0,19})\Z")
_NONCE = re.compile(r"[A-Za-z0-9_-]{16,64}\Z")
_IMAGE = re.compile(r"ghcr\.io/batumilove/hermes-agent-deploy@sha256:[0-9a-f]{64}\Z")
_EVENT = re.compile(
    r"(?:WARNING plugins\.platforms\.telegram\.telegram_network: )?"
    r"\[Telegram socket\] event=(request-started|request-cancelled|request-failed|socket-opened|"
    r"socket-close-started|socket-closed|socket-close-error|response-created|"
    r"response-closed|response-close-error) "
    r"owner=(general|polling) route=(primary|(?:[0-9]{1,3}\.){3}[0-9]{1,3}) "
    r"(?:request_id=(none|[1-9][0-9]{0,19}) )?local_port=([0-9]{1,5}|unknown|none)\s*\Z"
)


class DiagnosticError(Exception):
    exit_code = 1


class RequestError(DiagnosticError):
    exit_code = 64


class AuthorizationError(DiagnosticError):
    exit_code = 77


class ConfigError(DiagnosticError):
    pass


class ConfigDriftError(ConfigError):
    pass


class StateError(DiagnosticError):
    pass


class TransactionConflictError(StateError):
    pass


class CommandError(DiagnosticError):
    pass


class Request(NamedTuple):
    expected_source_sha: str
    observation_seconds: int
    run_id: str
    run_attempt: str
    nonce: str

    def dictionary(self) -> dict[str, object]:
        return dict(self._asdict())


class ConfigSnapshot(NamedTuple):
    existed: bool
    content: bytes
    mode: int
    uid: int
    gid: int
    sha256: str


class Transaction:
    def __init__(self, path: Path, record: dict[str, object]):
        self.path = path
        self.record = record

    @property
    def state(self) -> str:
        return str(self.record["state"])


class _DuplicateKey(ValueError):
    pass


def _object_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(f"duplicate key: {key}")
        result[key] = value
    return result


def parse_request(stream: BinaryIO) -> Request:
    raw = stream.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise RequestError("request exceeds 4096 bytes")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise RequestError("request is not UTF-8") from exc
    decoder = json.JSONDecoder(object_pairs_hook=_object_no_duplicates)
    try:
        value, end = decoder.raw_decode(text)
    except _DuplicateKey as exc:
        raise RequestError("request contains duplicate keys") from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise RequestError("request is not valid JSON") from exc
    if text[end:].strip():
        raise RequestError("request contains trailing data")
    keys = {"expected_source_sha", "observation_seconds", "run_id", "run_attempt", "nonce"}
    if not isinstance(value, dict) or set(value) != keys:
        raise RequestError("request keys do not match the exact schema")
    sha = value["expected_source_sha"]
    duration = value["observation_seconds"]
    run_id = value["run_id"]
    attempt = value["run_attempt"]
    nonce = value["nonce"]
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise RequestError("invalid expected source SHA")
    if type(duration) is not int or duration not in {60, 90, 120}:
        raise RequestError("invalid observation duration")
    if not isinstance(run_id, str) or not _DECIMAL.fullmatch(run_id) or run_id == "0":
        raise RequestError("invalid run ID")
    if not isinstance(attempt, str) or not _DECIMAL.fullmatch(attempt) or attempt == "0":
        raise RequestError("invalid run attempt")
    if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
        raise RequestError("invalid nonce")
    return Request(sha, duration, run_id, attempt, nonce)


def parse_cli(argv: list[str], environ: dict[str, str], euid: int) -> str:
    if argv == ["--recover"]:
        if euid != 0 or "SUDO_UID" in environ:
            raise AuthorizationError("recovery mode requires direct root invocation")
        return "recover"
    if argv:
        raise RequestError("arguments are not permitted")
    return "run"


def authorize_caller(environ: dict[str, str], euid: int) -> None:
    if euid != 0 or environ.get("SUDO_UID") != str(SUDO_UID):
        raise AuthorizationError("caller authorization failed")


def _sanitize(message: object, limit: int = 512) -> str:
    text = str(message)
    text = re.sub(r"[^A-Za-z0-9 _.,:=/@+-]", "?", text)
    return text[:limit]


def render_error(error: BaseException) -> str:
    payload = {"ok": False, "error": error.__class__.__name__, "message": _sanitize(error)}
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return rendered.encode()[:MAX_OUTPUT_BYTES].decode("utf-8", "ignore")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class CrashBarrier:
    """Root-armed one-shot crash barrier for exact staging recovery canaries."""

    _TARGETS = {"ARMED", "MUTATED", "ENABLED", "OBSERVING", "RESTORING"}

    def __init__(
        self,
        *,
        token_path: Path = CRASH_TOKEN_PATH,
        helper_path: Path = INSTALLED_HELPER_PATH,
        expected_uid: int = 0,
        expected_gid: int = 0,
        pid=os.getpid,
        signal_process=os.kill,
    ):
        self.token_path = Path(token_path)
        self.helper_path = Path(helper_path)
        self.expected_uid = expected_uid
        self.expected_gid = expected_gid
        self.pid = pid
        self.signal_process = signal_process

    def _helper_sha256(self) -> str:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.helper_path, flags)
        except OSError as exc:
            raise StateError("crash token helper identity is unsafe") from exc
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != self.expected_uid
                or info.st_gid != self.expected_gid
                or stat.S_IMODE(info.st_mode) != 0o755
                or info.st_size > 256 * 1024
            ):
                raise StateError("crash token helper identity is unsafe")
            data = b""
            while len(data) <= 256 * 1024:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                data += chunk
            if len(data) != info.st_size:
                raise StateError("crash token helper identity is unsafe")
            return _sha(data)
        finally:
            os.close(fd)

    def after_transition(self, tx: Transaction, state_name: str) -> None:
        parent = self.token_path.parent
        name = self.token_path.name
        dir_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            dir_fd = os.open(parent, dir_flags)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise StateError("crash token directory is unsafe") from exc
        fd = None
        try:
            try:
                fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
            except FileNotFoundError:
                return
            except OSError as exc:
                raise StateError("crash token is unsafe") from exc
            opened = os.fstat(fd)
            try:
                named = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except OSError as exc:
                raise StateError("crash token is unsafe") from exc
            if (
                not stat.S_ISREG(opened.st_mode)
                or stat.S_ISLNK(named.st_mode)
                or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
                or opened.st_nlink != 1
                or (opened.st_uid, opened.st_gid) != (self.expected_uid, self.expected_gid)
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_size > 512
            ):
                raise StateError("crash token is unsafe")
            raw = os.read(fd, 513)
            if len(raw) != opened.st_size:
                raise StateError("crash token is unsafe")
            try:
                text = raw.decode("utf-8", "strict")
                token, end = json.JSONDecoder(object_pairs_hook=_object_no_duplicates).raw_decode(text)
            except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKey, ValueError) as exc:
                raise StateError("crash token schema is invalid") from exc
            if text[end:].strip() or not isinstance(token, dict) or set(token) != {
                "version", "transaction", "target", "helper_sha256", "expected_source_sha"
            }:
                raise StateError("crash token schema is invalid")
            target = token.get("target")
            request_value = tx.record.get("request")
            expected_source = request_value.get("expected_source_sha") if isinstance(request_value, dict) else None
            if (
                type(token.get("version")) is not int
                or token.get("version") != 1
                or not isinstance(target, str)
                or target not in self._TARGETS
                or token.get("transaction") != tx.path.name
                or token.get("expected_source_sha") != expected_source
                or not isinstance(token.get("helper_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", str(token.get("helper_sha256")))
                or token.get("helper_sha256") != self._helper_sha256()
            ):
                raise StateError("crash token binding is invalid")
            if target != state_name:
                return
            current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise StateError("crash token identity changed")
            os.unlink(name, dir_fd=dir_fd)
            os.fsync(dir_fd)
            tx.record["crash_barrier"] = {
                "target": state_name,
                "helper_sha256": token["helper_sha256"],
                "expected_source_sha": expected_source,
                "consumed": True,
            }
            _atomic_path_write(tx.path / "state.json", (json.dumps(tx.record, sort_keys=True) + "\n").encode())
            self.signal_process(self.pid(), signal.SIGKILL)  # windows-footgun: ok
            raise StateError("crash token signal unexpectedly returned")
        finally:
            if fd is not None:
                os.close(fd)
            os.close(dir_fd)


def _same_snapshot(left: ConfigSnapshot, right: ConfigSnapshot) -> bool:
    return (
        left.existed,
        left.sha256,
        left.mode,
        left.uid,
        left.gid,
    ) == (
        right.existed,
        right.sha256,
        right.mode,
        right.uid,
        right.gid,
    )


def _fsync_directory(fd: int) -> None:
    os.fsync(fd)


_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2 = getattr(_LIBC, "renameat2", None)
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_TEST_GUARD_NAME = ".gateway.json.tx-00000000000000000000000000000000.swap"


def _rename_noreplace(old_dir_fd: int, old_name: str, new_dir_fd: int, new_name: str) -> None:
    if _RENAMEAT2 is None:
        raise OSError(errno.ENOSYS, "renameat2 unavailable")
    result = _RENAMEAT2(
        ctypes.c_int(old_dir_fd), ctypes.c_char_p(os.fsencode(old_name)),
        ctypes.c_int(new_dir_fd), ctypes.c_char_p(os.fsencode(new_name)),
        ctypes.c_uint(_RENAME_NOREPLACE),
    )
    if result != 0:
        value = ctypes.get_errno()
        raise OSError(value, os.strerror(value), old_name, new_name)


def _rename_exchange(first_dir_fd: int, first_name: str, second_dir_fd: int, second_name: str) -> None:
    if _RENAMEAT2 is None:
        raise OSError(errno.ENOSYS, "renameat2 unavailable")
    result = _RENAMEAT2(
        ctypes.c_int(first_dir_fd), ctypes.c_char_p(os.fsencode(first_name)),
        ctypes.c_int(second_dir_fd), ctypes.c_char_p(os.fsencode(second_name)),
        ctypes.c_uint(_RENAME_EXCHANGE),
    )
    if result != 0:
        value = ctypes.get_errno()
        raise OSError(value, os.strerror(value), first_name, second_name)


class ConfigStore:
    """Operate on gateway.json relative to a verified directory descriptor."""

    name = "gateway.json"

    def __init__(self, root: Path, *, expected_uid: int, expected_gid: int):
        self.root = Path(root)
        self.expected_uid = expected_uid
        self.expected_gid = expected_gid

    def _open_dir(self) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.root, flags)
        except OSError as exc:
            raise ConfigError("data directory cannot be opened safely") from exc
        st = os.fstat(fd)
        if (
            not stat.S_ISDIR(st.st_mode)
            or (st.st_uid, st.st_gid) != (self.expected_uid, self.expected_gid)
            or stat.S_IMODE(st.st_mode) != 0o700
        ):
            os.close(fd)
            raise ConfigError("data root ownership or mode mismatch")
        return fd

    def _read_named(self, dir_fd: int, name: str, *, allow_absent: bool) -> ConfigSnapshot:
        # A hostile runtime path may be a FIFO. Open non-blocking so we can
        # inspect and reject its type instead of hanging the privileged helper.
        flags = os.O_RDONLY | os.O_NONBLOCK
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(name, flags, dir_fd=dir_fd)
        except FileNotFoundError:
            if allow_absent:
                return ConfigSnapshot(False, b"", 0o600, self.expected_uid, self.expected_gid, _sha(b""))
            raise ConfigError("gateway config is absent")
        except OSError as exc:
            raise ConfigError("gateway config cannot be opened safely") from exc
        try:
            st = os.fstat(fd)
            named = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(named.st_mode):
                raise ConfigError("gateway config is not regular")
            if (st.st_dev, st.st_ino) != (named.st_dev, named.st_ino) or st.st_nlink != 1:
                raise ConfigError("gateway config identity is unsafe")
            if st.st_size > MAX_CONFIG_BYTES:
                raise ConfigError("gateway config is oversized")
            if (st.st_uid, st.st_gid) != (self.expected_uid, self.expected_gid):
                raise ConfigError("gateway config ownership mismatch")
            content = bytearray()
            while len(content) <= MAX_CONFIG_BYTES:
                chunk = os.read(fd, min(65536, MAX_CONFIG_BYTES + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
            if len(content) > MAX_CONFIG_BYTES:
                raise ConfigError("gateway config is oversized")
            final = os.fstat(fd)
            if (final.st_dev, final.st_ino, final.st_size) != (st.st_dev, st.st_ino, st.st_size):
                raise ConfigError("gateway config changed while reading")
            raw = bytes(content)
            try:
                parsed = json.loads(raw.decode("utf-8", "strict"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ConfigError("gateway config is not valid UTF-8 JSON") from exc
            if not isinstance(parsed, dict):
                raise ConfigError("gateway config root is not an object")
            return ConfigSnapshot(True, raw, stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid, _sha(raw))
        finally:
            os.close(fd)

    def _read_current(self, dir_fd: int, *, allow_absent: bool) -> ConfigSnapshot:
        return self._read_named(dir_fd, self.name, allow_absent=allow_absent)

    def snapshot(self) -> ConfigSnapshot:
        fd = self._open_dir()
        try:
            return self._read_current(fd, allow_absent=True)
        finally:
            os.close(fd)

    def device(self) -> int:
        fd = self._open_dir()
        try:
            return os.fstat(fd).st_dev
        finally:
            os.close(fd)

    @staticmethod
    def enabled_payload(snapshot: ConfigSnapshot) -> bytes:
        data = json.loads(snapshot.content.decode()) if snapshot.existed else {}
        platforms = data.setdefault("platforms", {})
        if not isinstance(platforms, dict):
            raise ConfigError("platforms is not an object")
        telegram = platforms.setdefault("telegram", {})
        if not isinstance(telegram, dict):
            raise ConfigError("telegram is not an object")
        extra = telegram.setdefault("extra", {})
        if not isinstance(extra, dict):
            raise ConfigError("telegram extra is not an object")
        extra["socket_diagnostics"] = True
        return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()

    @staticmethod
    def _validate_guard_name(guard_name: str) -> None:
        if not re.fullmatch(r"\.gateway\.json\.tx-[0-9a-f]{32}\.swap", guard_name):
            raise ConfigError("transaction guard name is invalid")

    def _write_named(self, dir_fd: int, name: str, value: ConfigSnapshot) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
        try:
            os.fchown(fd, value.uid, value.gid)
            os.fchmod(fd, value.mode)
            view = memoryview(value.content)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        except BaseException:
            try:
                os.unlink(name, dir_fd=dir_fd)
            except OSError:
                pass
            raise
        finally:
            os.close(fd)
        _fsync_directory(dir_fd)

    def _cleanup_guard(
        self,
        dir_fd: int,
        quarantine_fd: int,
        guard_name: str,
        expected: ConfigSnapshot,
    ) -> None:
        in_data = self._read_named(dir_fd, guard_name, allow_absent=True)
        quarantined = self._read_named(quarantine_fd, guard_name, allow_absent=True)
        if in_data.existed and quarantined.existed:
            raise ConfigDriftError("multiple transaction guards block cleanup")
        if in_data.existed:
            try:
                _rename_noreplace(dir_fd, guard_name, quarantine_fd, guard_name)
                _fsync_directory(dir_fd)
                _fsync_directory(quarantine_fd)
            except OSError as exc:
                raise ConfigDriftError("transaction guard changed during quarantine") from exc
            quarantined = self._read_named(quarantine_fd, guard_name, allow_absent=False)
        if not quarantined.existed:
            return
        if not _same_snapshot(quarantined, expected):
            try:
                _rename_noreplace(quarantine_fd, guard_name, dir_fd, guard_name)
                _fsync_directory(quarantine_fd)
                _fsync_directory(dir_fd)
            except OSError:
                pass
            raise ConfigDriftError("transaction guard drift blocks cleanup")
        os.unlink(guard_name, dir_fd=quarantine_fd)
        _fsync_directory(quarantine_fd)
        if self._read_named(dir_fd, guard_name, allow_absent=True).existed:
            raise ConfigDriftError("concurrent transaction guard drift was preserved")

    def _exchange_checked(
        self,
        dir_fd: int,
        guard_name: str,
        expected_current: ConfigSnapshot,
        expected_guard: ConfigSnapshot,
    ) -> None:
        _rename_exchange(dir_fd, guard_name, dir_fd, self.name)
        _fsync_directory(dir_fd)
        current = self._read_current(dir_fd, allow_absent=False)
        guard = self._read_named(dir_fd, guard_name, allow_absent=False)
        if _same_snapshot(current, expected_guard) and _same_snapshot(guard, expected_current):
            return
        try:
            _rename_exchange(dir_fd, guard_name, dir_fd, self.name)
            _fsync_directory(dir_fd)
        except OSError:
            pass
        raise ConfigDriftError("gateway config drift captured by atomic exchange")

    def enable(self, snapshot: ConfigSnapshot, guard_name: str = _TEST_GUARD_NAME) -> str:
        self._validate_guard_name(guard_name)
        content = self.enabled_payload(snapshot)
        fd = self._open_dir()
        try:
            current = self._read_current(fd, allow_absent=True)
            if not _same_snapshot(current, snapshot):
                raise ConfigDriftError("gateway config drift before mutation")
            replacement = ConfigSnapshot(
                True, content, snapshot.mode, snapshot.uid, snapshot.gid, _sha(content)
            )
            guard = self._read_named(fd, guard_name, allow_absent=True)
            if guard.existed:
                raise ConfigDriftError("transaction guard already exists")
            self._write_named(fd, guard_name, replacement)
            if snapshot.existed:
                self._exchange_checked(fd, guard_name, snapshot, replacement)
            else:
                try:
                    _rename_noreplace(fd, guard_name, fd, self.name)
                    _fsync_directory(fd)
                except OSError as exc:
                    raise ConfigDriftError("gateway config pathname changed during install") from exc
                installed = self._read_current(fd, allow_absent=False)
                if not _same_snapshot(installed, replacement):
                    raise ConfigDriftError("gateway config install verification failed")
            return _sha(content)
        finally:
            os.close(fd)

    def restore(
        self,
        snapshot: ConfigSnapshot,
        expected_mutated_hash: str,
        quarantine_fd: int,
        guard_name: str = _TEST_GUARD_NAME,
    ) -> None:
        self._validate_guard_name(guard_name)
        fd = self._open_dir()
        try:
            current = self._read_current(fd, allow_absent=True)
            expected_mutated = ConfigSnapshot(
                True, b"", snapshot.mode, snapshot.uid, snapshot.gid, expected_mutated_hash
            )
            guard = self._read_named(fd, guard_name, allow_absent=True)
            if snapshot.existed:
                if _same_snapshot(current, snapshot):
                    if guard.existed and not _same_snapshot(guard, expected_mutated):
                        raise ConfigDriftError("transaction guard drift blocks cleanup")
                elif _same_snapshot(current, expected_mutated) and _same_snapshot(guard, snapshot):
                    self._exchange_checked(fd, guard_name, expected_mutated, snapshot)
                    current = self._read_current(fd, allow_absent=False)
                    guard = self._read_named(fd, guard_name, allow_absent=False)
                else:
                    raise ConfigDriftError("gateway config drift blocks restore")
            else:
                if not current.existed:
                    if guard.existed and not _same_snapshot(guard, expected_mutated):
                        raise ConfigDriftError("transaction guard drift blocks cleanup")
                elif _same_snapshot(current, expected_mutated) and not guard.existed:
                    try:
                        _rename_noreplace(fd, self.name, fd, guard_name)
                        _fsync_directory(fd)
                    except OSError as exc:
                        raise ConfigDriftError("gateway config pathname changed during removal") from exc
                    current = self._read_current(fd, allow_absent=True)
                    guard = self._read_named(fd, guard_name, allow_absent=False)
                    if current.existed or not _same_snapshot(guard, expected_mutated):
                        raise ConfigDriftError("gateway config removal verification failed")
                else:
                    raise ConfigDriftError("gateway config drift blocks restore")
            restored = self._read_current(fd, allow_absent=True)
            if not _same_snapshot(restored, snapshot):
                raise ConfigDriftError("gateway config exact restoration failed")
            self._cleanup_guard(fd, quarantine_fd, guard_name, expected_mutated)
        finally:
            os.close(fd)

    def matches(self, snapshot: ConfigSnapshot) -> bool:
        fd = self._open_dir()
        try:
            return _same_snapshot(self._read_current(fd, allow_absent=True), snapshot)
        finally:
            os.close(fd)


def _atomic_path_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        view = memoryview(content)
        while view:
            count = os.write(fd, view)
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class TransactionStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def _read(self, path: Path) -> dict[str, object]:
        try:
            raw = path.read_bytes()
            value = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError("transaction state is unreadable") from exc
        if not isinstance(value, dict) or value.get("state") not in set(STATE_ORDER) | {"RESTORE_FAILED", "ABORTED"}:
            raise StateError("transaction state is invalid")
        return value

    def _write(self, tx: Transaction) -> None:
        _atomic_path_write(tx.path / "state.json", (json.dumps(tx.record, sort_keys=True) + "\n").encode())

    def transactions(self) -> list[Transaction]:
        result = []
        for entry in sorted(self.root.iterdir()):
            if entry.is_symlink() or not entry.is_dir():
                raise StateError("unsafe transaction entry")
            state_file = entry / "state.json"
            if state_file.exists():
                result.append(Transaction(entry, self._read(state_file)))
        return result

    def open_transaction(self, tx: Transaction) -> int:
        if tx.path.parent != self.root or not tx.path.name:
            raise StateError("transaction path is outside the state root")
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        root_fd = os.open(self.root, flags)
        fd: int | None = None
        try:
            fd = os.open(tx.path.name, flags, dir_fd=root_fd)
            opened = os.fstat(fd)
            named = os.stat(tx.path.name, dir_fd=root_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or stat.S_ISLNK(named.st_mode)
                or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
                or (opened.st_uid, opened.st_gid) != (os.geteuid(), os.getegid())  # windows-footgun: ok
                or stat.S_IMODE(opened.st_mode) != 0o700
            ):
                raise StateError("transaction directory identity is unsafe")
            result = fd
            fd = None
            return result
        except OSError as exc:
            raise StateError("transaction directory cannot be opened safely") from exc
        finally:
            if fd is not None:
                os.close(fd)
            os.close(root_fd)

    def prepare(self, request: Request) -> Transaction:
        identity = f"{request.run_id}-{request.run_attempt}-{request.nonce}"
        digest = _sha(json.dumps(request.dictionary(), sort_keys=True, separators=(",", ":")).encode())
        path = self.root / identity
        if path.exists():
            tx = Transaction(path, self._read(path / "state.json"))
            if tx.record.get("request_digest") != digest:
                raise TransactionConflictError("conflicting transaction replay")
            return tx
        for existing in self.transactions():
            if existing.state not in TERMINAL_STATES:
                raise TransactionConflictError("another transaction is active")
        path.mkdir(mode=0o700)
        root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
        record = {"version": 1, "state": "PREPARED", "request": request.dictionary(), "request_digest": digest}
        tx = Transaction(path, record)
        self._write(tx)
        return tx

    def transition(self, tx: Transaction, state_name: str) -> None:
        expected = _TRANSITIONS.get(tx.state)
        if state_name != expected:
            raise StateError("invalid durable state transition")
        tx.record["state"] = state_name
        self._write(tx)

    def fail_restore(self, tx: Transaction) -> None:
        tx.record["state"] = "RESTORE_FAILED"
        self._write(tx)

    def begin_restore(self, tx: Transaction) -> None:
        if tx.state not in {"ARMED", "MUTATED", "ENABLED", "OBSERVING", "RESTORING", "RESTORE_FAILED"}:
            raise StateError("transaction cannot begin restoration")
        if tx.state != "RESTORING":
            tx.record["state"] = "RESTORING"
            self._write(tx)

    def abort_prepared(self, tx: Transaction) -> None:
        if tx.state != "PREPARED":
            raise StateError("only a prepared transaction can be aborted")
        tx.record["state"] = "ABORTED"
        self._write(tx)

    def save_snapshot(self, tx: Transaction, snapshot: ConfigSnapshot) -> None:
        if tx.state != "PREPARED":
            raise StateError("snapshot must be saved while prepared")
        if snapshot.existed:
            _atomic_path_write(tx.path / "gateway.json.original", snapshot.content)
        tx.record["snapshot"] = {
            "existed": snapshot.existed, "mode": snapshot.mode, "uid": snapshot.uid,
            "gid": snapshot.gid, "sha256": snapshot.sha256,
        }
        request_digest = tx.record.get("request_digest")
        if not isinstance(request_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", request_digest):
            raise StateError("transaction request digest is invalid")
        tx.record["config_guard"] = f".gateway.json.tx-{request_digest[:32]}.swap"
        self._write(tx)

    @staticmethod
    def load_guard_name(tx: Transaction) -> str:
        guard_name = tx.record.get("config_guard")
        if not isinstance(guard_name, str) or not re.fullmatch(
            r"\.gateway\.json\.tx-[0-9a-f]{32}\.swap", guard_name
        ):
            raise StateError("transaction config guard is invalid")
        return guard_name

    def load_snapshot(self, tx: Transaction) -> ConfigSnapshot:
        value = tx.record.get("snapshot")
        if not isinstance(value, dict):
            raise StateError("transaction snapshot metadata is absent")
        existed = value.get("existed") is True
        content = (tx.path / "gateway.json.original").read_bytes() if existed else b""
        if _sha(content) != value.get("sha256"):
            raise StateError("transaction snapshot hash mismatch")
        return ConfigSnapshot(existed, content, int(value["mode"]), int(value["uid"]), int(value["gid"]), str(value["sha256"]))

    def record_mutated_hash(self, tx: Transaction, digest: str) -> None:
        tx.record["mutated_sha256"] = digest
        self._write(tx)


def command_runner(
    argv,
    *,
    timeout: int,
    env: dict[str, str],
    input_data=None,
    max_output=None,
    lock_fd: int | None = None,
) -> str:
    if not argv or not os.path.isabs(argv[0]):
        raise CommandError("command vector is not absolute")
    if input_data is not None:
        raise CommandError("subprocess stdin is not permitted")
    limit = min(max_output or MAX_COMMAND_OUTPUT_BYTES, MAX_COMMAND_OUTPUT_BYTES)
    try:
        process = subprocess.Popen(
            list(argv), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, close_fds=True,
            start_new_session=True,
            pass_fds=() if lock_fd is None else (lock_fd,),
        )
    except OSError as exc:
        raise CommandError("bounded command execution failed") from exc

    def terminate() -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)  # windows-footgun: ok
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    raw = bytearray()
    deadline = time.monotonic() + timeout
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    terminate()
                    raise CommandError("bounded command execution timed out")
                events = selector.select(min(0.2, remaining))
                if not events:
                    if process.poll() is None:
                        continue
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                else:
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                if len(raw) + len(chunk) > limit:
                    terminate()
                    raise CommandError("command output exceeded bound")
                raw.extend(chunk)
        try:
            returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            terminate()
            raise CommandError("bounded command execution timed out") from exc
    except BaseException:
        if process.poll() is None:
            terminate()
        raise
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
    if returncode != 0:
        raise CommandError(f"command failed with status {returncode}")
    try:
        return bytes(raw).decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise CommandError("command output was not UTF-8") from exc


_EFFECTIVE_TRUE = """from gateway.config import Platform, load_gateway_config
platform = load_gateway_config().platforms.get(Platform.TELEGRAM)
value = None if platform is None else platform.extra.get("socket_diagnostics")
if value is not True: raise SystemExit("effective value is not literal true")
print("true")
"""
_EFFECTIVE_FALSE = """from gateway.config import Platform, load_gateway_config
platform = load_gateway_config().platforms.get(Platform.TELEGRAM)
value = None if platform is None else platform.extra.get("socket_diagnostics")
if value is True: raise SystemExit("effective value is literal true")
print("false")
"""


class DiagnosticExecutor:
    def __init__(self, *, deploy_root=DEPLOY_ROOT, data_root=DATA_ROOT, state_root=TRANSACTION_ROOT,
                 lock_path=LOCK_PATH,
                 lock_uid=0,
                 expected_uid=RUNTIME_UID, expected_gid=RUNTIME_GID, runner=command_runner,
                 sleep=time.sleep, hostname=lambda: socket.gethostname().split(".")[0],
                 crash_barrier=None):
        self.deploy_root = Path(deploy_root)
        self.data_root = Path(data_root)
        self.states = TransactionStore(Path(state_root))
        self.config = ConfigStore(Path(data_root), expected_uid=expected_uid, expected_gid=expected_gid)
        self.expected_uid = expected_uid
        self.expected_gid = expected_gid
        self.lock_path = Path(lock_path)
        self.lock_uid = lock_uid
        self.runner = runner
        self.sleep = sleep
        self.hostname = hostname
        self.crash_barrier = crash_barrier
        self.deadline = 0.0
        self.active_lock_fd: int | None = None

    def _start_deadline(self, seconds: int) -> None:
        self.deadline = time.monotonic() + seconds

    def _remaining(self, cap: int) -> int:
        if not self.deadline:
            raise CommandError("diagnostic deadline was not initialized")
        remaining = int(self.deadline - time.monotonic())
        if remaining <= 0:
            raise CommandError("global diagnostic deadline expired")
        return max(1, min(cap, remaining))

    def _command(self, argv, timeout, max_output=65536):
        return self.runner(
            tuple(argv), timeout=self._remaining(timeout), env=COMMAND_ENV,
            input_data=None, max_output=max_output, lock_fd=self.active_lock_fd,
        )

    def _open_lock(self) -> int:
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.lock_path, flags)
        except OSError as exc:
            raise DiagnosticError("deployment lock unavailable") from exc
        try:
            st = os.fstat(fd)
            named = os.stat(self.lock_path, follow_symlinks=False)
            if (
                not stat.S_ISREG(st.st_mode)
                or st.st_nlink != 1
                or st.st_uid != self.lock_uid
                or stat.S_IMODE(st.st_mode) != 0o660
                or (st.st_dev, st.st_ino) != (named.st_dev, named.st_ino)
            ):
                raise DiagnosticError("deployment lock identity mismatch")
            lock_deadline = min(self.deadline, time.monotonic() + LOCK_WAIT_SECONDS)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= lock_deadline:
                        raise DiagnosticError("deployment lock deadline expired")
                    self.sleep(0.05)
            final = os.fstat(fd)
            named = os.stat(self.lock_path, follow_symlinks=False)
            if (final.st_dev, final.st_ino) != (named.st_dev, named.st_ino):
                raise DiagnosticError("deployment lock changed while acquiring")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _exact_env(self, name: str) -> dict[str, str]:
        dir_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            dir_fd = os.open(self.deploy_root, dir_flags)
        except OSError as exc:
            raise DiagnosticError("fixed metadata directory is unavailable") from exc
        try:
            flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(name, flags, dir_fd=dir_fd)
            try:
                st = os.fstat(fd)
                named = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                root_st = os.fstat(dir_fd)
                if (
                    not stat.S_ISREG(st.st_mode)
                    or stat.S_ISLNK(named.st_mode)
                    or st.st_nlink != 1
                    or (st.st_dev, st.st_ino) != (named.st_dev, named.st_ino)
                    or (st.st_uid, st.st_gid) != (root_st.st_uid, root_st.st_gid)
                    or st.st_mode & 0o022
                    or st.st_size > MAX_METADATA_BYTES
                ):
                    raise DiagnosticError("fixed metadata file is unsafe")
                raw = os.read(fd, MAX_METADATA_BYTES + 1)
                final = os.fstat(fd)
                if len(raw) > MAX_METADATA_BYTES or (final.st_dev, final.st_ino, final.st_size) != (st.st_dev, st.st_ino, st.st_size):
                    raise DiagnosticError("fixed metadata file changed while reading")
            finally:
                os.close(fd)
        except OSError as exc:
            raise DiagnosticError("fixed metadata file is unavailable") from exc
        finally:
            os.close(dir_fd)
        try:
            lines = raw.decode("utf-8", "strict").splitlines()
        except UnicodeDecodeError as exc:
            raise DiagnosticError("fixed metadata file is invalid") from exc
        result = {}
        for line in lines:
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator or key in result:
                raise DiagnosticError("fixed metadata file is invalid")
            result[key] = value
        return result

    def _preflight(self, request: Request) -> None:
        if self.hostname() != DEPLOY_HOST:
            raise DiagnosticError("host identity mismatch")
        release = self._exact_env("release.env")
        image = release.get("HERMES_IMAGE")
        if release != {
            "HERMES_IMAGE": image,
            "HERMES_DEPLOY_ENV": ENVIRONMENT,
            "HERMES_SOURCE_SHA": request.expected_source_sha,
        } or not isinstance(image, str) or not _IMAGE.fullmatch(image):
            raise DiagnosticError("release metadata mismatch")
        runtime = self._exact_env("runtime.env")
        expected_runtime = {"HERMES_DATA_DIR": str(self.data_root), "HERMES_UID": str(self.expected_uid), "HERMES_GID": str(self.expected_gid)}
        if runtime != expected_runtime:
            raise DiagnosticError("runtime metadata mismatch")
        env_output = self._command((DOCKER, "inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}", CONTAINER), 15)
        env_lines = env_output.splitlines()
        expected_env = {
            "HERMES_SOURCE_SHA": request.expected_source_sha,
            "HERMES_DEPLOY_ENV": ENVIRONMENT,
            "HERMES_HOME": "/opt/data",
        }
        if any(
            [line for line in env_lines if line.startswith(name + "=")] != [name + "=" + value]
            for name, value in expected_env.items()
        ):
            raise DiagnosticError("container environment mismatch")
        configured_image = self._command((DOCKER, "inspect", "--format", "{{.Config.Image}}", CONTAINER), 15).strip()
        if configured_image != image:
            raise DiagnosticError("container image mismatch")
        mount = self._command((DOCKER, "inspect", "--format", '{{range .Mounts}}{{if eq .Destination "/opt/data"}}{{println .Source}}{{end}}{{end}}', CONTAINER), 15).strip()
        if mount != str(self.data_root):
            raise DiagnosticError("container mount mismatch")
        self._wait_healthy()
        self._effective(False)
        snapshot = self.config.snapshot()
        if snapshot.existed:
            parsed = json.loads(snapshot.content.decode("utf-8"))
            value = parsed.get("platforms", {}).get("telegram", {}).get("extra", {}).get("socket_diagnostics")
            if value is True:
                raise ConfigError("on-disk diagnostics are already literal true")

    def _health_status(self) -> str:
        return self._command(
            (
                DOCKER, "inspect", "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
                CONTAINER,
            ),
            15,
        ).strip()

    def _wait_healthy(self) -> None:
        deadline = min(self.deadline, time.monotonic() + 120)
        while time.monotonic() < deadline:
            if self._health_status() == "healthy":
                return
            self.sleep(3)
        raise CommandError("gateway health deadline expired")

    def _effective(self, enabled: bool) -> None:
        script = _EFFECTIVE_TRUE if enabled else _EFFECTIVE_FALSE
        result = self._command(
            (
                DOCKER, "exec", "--env", "HERMES_HOME=/opt/data", "--user", "hermes",
                CONTAINER, CONTAINER_PYTHON, "-c", script,
            ),
            60,
        ).strip()
        if result != ("true" if enabled else "false"):
            raise CommandError("effective diagnostic verification failed")

    def _restart(self) -> None:
        self._command((DOCKER, "restart", "--time", "90", CONTAINER), 120)
        self._wait_healthy()

    def _start(self) -> None:
        self._command((DOCKER, "start", CONTAINER), 120)
        self._wait_healthy()

    def _stop(self) -> None:
        self._command((DOCKER, "stop", "--time", "90", CONTAINER), 120)

    def _container_identity(self) -> str:
        value = self._command(
            (DOCKER, "inspect", "--format", "{{.Id}} {{.State.StartedAt}}", CONTAINER), 15
        ).strip()
        container_id, separator, started_at = value.partition(" ")
        if not separator or not re.fullmatch(r"[0-9a-f]{64}", container_id) or not started_at.endswith("Z"):
            raise CommandError("container start identity is invalid")
        return value

    def _sleep_bounded(self, seconds: float) -> None:
        if time.monotonic() + seconds >= self.deadline:
            raise CommandError("global diagnostic deadline cannot fit requested wait")
        self.sleep(seconds)

    @staticmethod
    def _observation_since() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    @staticmethod
    def _aggregate(raw: str) -> dict[str, object]:
        counts: dict[tuple[str, str, str], int] = {}
        balances: dict[tuple[str, str, str, str, str], int] = {}
        requests: set[tuple[str, str, str]] = set()
        opening_events = {"socket-opened": "socket", "response-created": "response"}
        terminal_events = {
            "socket-closed": "socket",
            "socket-close-error": "socket",
            "response-closed": "response",
        }
        checkpoint_events = {
            "socket-close-started": "socket",
            "response-close-error": "response",
        }
        total_opened = 0
        for line in raw.splitlines():
            match = _EVENT.fullmatch(line)
            if not match:
                if "[Telegram socket]" in line:
                    raise DiagnosticError("socket lifecycle record is malformed")
                continue
            event, owner, route, request_id, port = match.groups()
            if request_id == "none":
                raise DiagnosticError("socket lifecycle record is malformed")
            if route != "primary":
                try:
                    address = ipaddress.ip_address(route)
                except ValueError as exc:
                    raise DiagnosticError("socket lifecycle record is malformed") from exc
                if address.version != 4 or not address.is_global or address.is_multicast:
                    raise DiagnosticError("socket lifecycle record is malformed")

            request_key = (owner, route, request_id or "")
            if event in {"request-started", "request-cancelled", "request-failed"}:
                if request_id is None or port != "none":
                    raise DiagnosticError("socket lifecycle record is malformed")
                if event == "request-started":
                    if request_key in requests:
                        raise DiagnosticError("socket lifecycle events are causally incomplete")
                    requests.add(request_key)
                elif request_key not in requests:
                    raise DiagnosticError("socket lifecycle events are causally incomplete")
            else:
                if event == "socket-close-started" and request_id is None:
                    raise DiagnosticError("socket lifecycle record is malformed")
                if port == "none":
                    raise DiagnosticError("socket lifecycle record is malformed")
                if port == "unknown":
                    raise DiagnosticError("socket lifecycle events are incomplete: unknown local port")
                if not 1 <= int(port) <= 65535:
                    raise DiagnosticError("socket lifecycle record is malformed")
                if request_id is not None and request_key not in requests:
                    raise DiagnosticError("socket lifecycle events are causally incomplete")

                port_key = (
                    opening_events.get(event)
                    or terminal_events.get(event)
                    or checkpoint_events[event],
                    owner,
                    route,
                    request_id or "",
                    port,
                )
                if event in opening_events:
                    balances[port_key] = balances.get(port_key, 0) + 1
                    total_opened += 1
                elif event in checkpoint_events:
                    if balances.get(port_key, 0) <= 0:
                        raise DiagnosticError("socket lifecycle events are causally incomplete")
                else:
                    if balances.get(port_key, 0) <= 0:
                        raise DiagnosticError("socket lifecycle events are causally incomplete")
                    balances[port_key] -= 1
            key = (owner, route, event)
            counts[key] = min(counts.get(key, 0) + 1, 2**31 - 1)
        if not counts:
            raise DiagnosticError("no socket lifecycle events observed")
        if len(counts) > 256 or len(balances) > 4096 or len(requests) > 4096:
            raise DiagnosticError("diagnostic aggregate exceeds bound")
        if total_opened == 0 or any(balance != 0 for balance in balances.values()):
            raise DiagnosticError("socket lifecycle events are incomplete")
        return {
            "counts": [{"owner": o, "route": r, "event": e, "count": c} for (o, r, e), c in sorted(counts.items())],
            "created_without_terminal": [],
        }

    def _after_forward_transition(self, tx: Transaction, state_name: str) -> None:
        if self.crash_barrier is not None:
            self.crash_barrier.after_transition(tx, state_name)

    def _restore(
        self, tx: Transaction, snapshot: ConfigSnapshot, mutated_hash: str, *, allow_crash: bool = False
    ) -> None:
        self.states.begin_restore(tx)
        if allow_crash:
            self._after_forward_transition(tx, "RESTORING")
        guard_name = self.states.load_guard_name(tx)
        quarantine_fd = self.states.open_transaction(tx)
        last_error: BaseException | None = None
        try:
            try:
                settled = self.config.matches(snapshot) and self._health_status() == "healthy"
            except BaseException as exc:
                last_error = exc
            else:
                if settled:
                    try:
                        self.config.restore(snapshot, mutated_hash, quarantine_fd, guard_name)
                        self._effective(False)
                        self.states.transition(tx, "RESTORED")
                        return
                    except BaseException as exc:
                        last_error = exc
                for attempt in range(3):
                    try:
                        self._stop()
                        self.config.restore(snapshot, mutated_hash, quarantine_fd, guard_name)
                        self._start()
                        self._effective(False)
                        self.states.transition(tx, "RESTORED")
                        return
                    except BaseException as exc:
                        last_error = exc
                        if attempt < 2:
                            self._sleep_bounded(3)
        finally:
            os.close(quarantine_fd)
        self.states.fail_restore(tx)
        raise DiagnosticError("bounded restore retries exhausted") from last_error

    def run(self, request: Request) -> dict[str, object]:
        self._start_deadline(FORWARD_DEADLINE_SECONDS)
        lock_fd = self._open_lock()
        self.active_lock_fd = lock_fd
        try:
            self._preflight(request)
            tx = self.states.prepare(request)
            if tx.state == "RESTORED":
                result = tx.record.get("result")
                return result if isinstance(result, dict) else {"replayed": True}
            if tx.state != "PREPARED":
                raise TransactionConflictError("active transaction requires recovery")
            snapshot = self.config.snapshot()
            self.states.save_snapshot(tx, snapshot)
            guard_name = self.states.load_guard_name(tx)
            quarantine_fd = self.states.open_transaction(tx)
            try:
                if os.fstat(quarantine_fd).st_dev != self.config.device():
                    self.states.abort_prepared(tx)
                    raise StateError("transaction and data directories are on different filesystems")
            finally:
                os.close(quarantine_fd)
            self.states.transition(tx, "ARMED")
            self._after_forward_transition(tx, "ARMED")
            mutated_hash = _sha(self.config.enabled_payload(snapshot))
            result = None
            try:
                self._stop()
                actual_hash = self.config.enable(snapshot, guard_name)
                if actual_hash != mutated_hash:
                    raise StateError("mutated config hash mismatch")
                self.states.record_mutated_hash(tx, mutated_hash)
                self.states.transition(tx, "MUTATED")
                self._after_forward_transition(tx, "MUTATED")
                observation_since = self._observation_since()
                self._restart()
                self.states.transition(tx, "ENABLED")
                self._after_forward_transition(tx, "ENABLED")
                self._effective(True)
                container_identity = self._container_identity()
                self.states.transition(tx, "OBSERVING")
                self._after_forward_transition(tx, "OBSERVING")
                self._sleep_bounded(request.observation_seconds)
                if self._container_identity() != container_identity:
                    raise DiagnosticError("container identity changed during observation")
                self._stop()
                if self._container_identity() != container_identity:
                    raise DiagnosticError("container identity changed during observation shutdown")
                raw = self._command((DOCKER, "logs", "--since", observation_since, CONTAINER), 60, MAX_COMMAND_OUTPUT_BYTES)
                if self._container_identity() != container_identity:
                    raise DiagnosticError("container identity changed during log collection")
                result = self._aggregate(raw)
                result["observation_collected"] = True
            finally:
                self._start_deadline(RESTORE_DEADLINE_SECONDS)
                self._restore(tx, snapshot, mutated_hash, allow_crash=True)
            tx.record["result"] = result
            self.states._write(tx)
            return result
        finally:
            self.active_lock_fd = None
            os.close(lock_fd)

    def recover(self) -> dict[str, int]:
        self._start_deadline(RESTORE_DEADLINE_SECONDS)
        lock_fd = self._open_lock()
        self.active_lock_fd = lock_fd
        try:
            recovered = 0
            aborted = 0
            failed = 0
            for tx in self.states.transactions():
                if tx.state in TERMINAL_STATES:
                    continue
                if tx.state == "PREPARED":
                    self.states.abort_prepared(tx)
                    aborted += 1
                    continue
                try:
                    snapshot = self.states.load_snapshot(tx)
                    mutated_hash = str(tx.record.get("mutated_sha256") or _sha(self.config.enabled_payload(snapshot)))
                    self._restore(tx, snapshot, mutated_hash)
                    recovered += 1
                except DiagnosticError:
                    if tx.state != "RESTORE_FAILED":
                        self.states.fail_restore(tx)
                    failed += 1
            if failed:
                raise DiagnosticError("one or more recovery transactions failed")
            return {"recovered": recovered, "aborted": aborted}
        finally:
            self.active_lock_fd = None
            os.close(lock_fd)


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        mode = parse_cli(argv, dict(os.environ), os.geteuid())  # windows-footgun: ok
        if mode == "recover":
            executor = DiagnosticExecutor()
            result = executor.recover()
        else:
            authorize_caller(dict(os.environ), os.geteuid())  # windows-footgun: ok
            request = parse_request(sys.stdin.buffer)
            executor = DiagnosticExecutor(crash_barrier=CrashBarrier())
            result = executor.run(request)
        rendered = json.dumps({"ok": True, **result}, sort_keys=True, separators=(",", ":"))
        if len(rendered.encode()) > MAX_OUTPUT_BYTES:
            raise DiagnosticError("sanitized result exceeded output bound")
        print(rendered)
        return 0
    except DiagnosticError as exc:
        print(render_error(exc), file=sys.stderr)
        return exc.exit_code
    except BaseException as exc:
        print(render_error(DiagnosticError("unexpected internal failure")), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
