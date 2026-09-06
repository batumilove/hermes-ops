#!/usr/bin/env python3
"""Prune obsolete Hermes deployment images after a verified deployment.

The helper validates the complete target-repository inventory before removing
anything. It preserves the active digest, a bounded unique rollback history,
and every image referenced by any container on the host.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path


IMAGE_REPOSITORY = "ghcr.io/batumilove/hermes-agent-deploy"
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
SUCCESS_RESULTS = frozenset({"deployed", "rollback", "automatic-rollback"})


class RetentionError(RuntimeError):
    pass


def _run(argv: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RetentionError(f"docker inventory command failed: {argv[:2]}") from exc


def _require_success(completed: subprocess.CompletedProcess[str], label: str) -> str:
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit {completed.returncode}"
        raise RetentionError(f"{label} failed: {detail}")
    return completed.stdout


def _history_policy(
    history_file: Path, active_digest: str, rollback_images: int
) -> tuple[list[str], set[str]]:
    try:
        metadata = history_file.lstat()
        text = history_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise RetentionError("deployment history is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or history_file.is_symlink():
        raise RetentionError("deployment history is unsafe")

    retained = [active_digest]
    records: list[tuple[str, str]] = []
    known_digests: set[str] = set()
    active_success_seen = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        fields = line.split("\t")
        if len(fields) != 5:
            raise RetentionError(f"invalid deployment history line {line_number}")
        result, digest = fields[1], fields[4]
        if digest != "unknown" and not DIGEST_RE.fullmatch(digest):
            raise RetentionError(f"invalid deployment history digest on line {line_number}")
        records.append((result, digest))
        if digest != "unknown":
            known_digests.add(digest)
        if result in SUCCESS_RESULTS and digest == active_digest:
            active_success_seen = True

    if not active_success_seen:
        raise RetentionError("active digest is absent from successful deployment history")

    for result, digest in reversed(records):
        if result not in SUCCESS_RESULTS or digest == active_digest or digest in retained:
            continue
        retained.append(digest)
        if len(retained) == rollback_images + 1:
            break
    return retained, known_digests


def _image_inventory(repository: str) -> dict[str, str]:
    output = _require_success(
        _run(
            [
                "docker",
                "image",
                "ls",
                "--all",
                "--digests",
                "--no-trunc",
                "--format",
                "{{json .}}",
            ]
        ),
        "docker image inventory",
    )
    inventory: dict[str, str] = {}
    for line in output.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RetentionError("unsafe target image inventory: invalid JSON") from exc
        if not isinstance(row, dict) or row.get("Repository") != repository:
            continue
        digest, image_id = row.get("Digest"), row.get("ID")
        if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
            raise RetentionError("unsafe target image inventory: invalid digest")
        if not isinstance(image_id, str) or not IMAGE_ID_RE.fullmatch(image_id):
            raise RetentionError("unsafe target image inventory: invalid image ID")
        prior = inventory.setdefault(digest, image_id)
        if prior != image_id:
            raise RetentionError("unsafe target image inventory: conflicting image IDs")
    return inventory


def _container_image_ids() -> set[str]:
    identifiers = _require_success(
        _run(["docker", "container", "ls", "--all", "--quiet", "--no-trunc"]),
        "docker container inventory",
    ).splitlines()
    if not identifiers:
        return set()
    output = _require_success(
        _run(["docker", "inspect", "--format", "{{.Image}}", *identifiers]),
        "docker container image inventory",
    )
    image_ids = set(output.splitlines())
    if len(image_ids) > len(identifiers) or any(not IMAGE_ID_RE.fullmatch(value) for value in image_ids):
        raise RetentionError("unsafe container image inventory")
    return image_ids


def _docker_root() -> Path:
    raw = _require_success(
        _run(["docker", "info", "--format", "{{.DockerRootDir}}"]),
        "docker root discovery",
    ).strip()
    path = Path(raw)
    if not raw.startswith("/") or raw == "/" or ".." in path.parts:
        raise RetentionError("unsafe Docker root")
    try:
        if not path.is_dir() or path.is_symlink():
            raise RetentionError("unsafe Docker root")
    except OSError as exc:
        raise RetentionError("unsafe Docker root") from exc
    return path


def _available_bytes(path: Path) -> int:
    stats = os.statvfs(path)
    return stats.f_bavail * stats.f_frsize


def prune(repository: str, active_digest: str, history_file: Path, rollback_images: int) -> dict:
    if repository != IMAGE_REPOSITORY:
        raise RetentionError("unexpected image repository")
    if not DIGEST_RE.fullmatch(active_digest):
        raise RetentionError("invalid active digest")
    if not 0 <= rollback_images <= 10:
        raise RetentionError("rollback image count must be 0..10")

    retained, history_digests = _history_policy(history_file, active_digest, rollback_images)
    inventory = _image_inventory(repository)
    if active_digest not in inventory:
        raise RetentionError("unsafe target image inventory: active digest is absent")
    missing_retained = [digest for digest in retained if digest not in inventory]
    if missing_retained:
        raise RetentionError(
            f"unsafe target image inventory: retained rollback digests are absent: {missing_retained}"
        )
    container_ids = _container_image_ids()
    docker_root = _docker_root()

    protected = sorted(
        digest
        for digest, image_id in inventory.items()
        if image_id in container_ids and digest not in retained
    )
    inventory_only = sorted(set(inventory) - history_digests)
    candidates = sorted(history_digests & set(inventory) - set(retained) - set(protected))

    available_before = _available_bytes(docker_root)
    removed: list[str] = []
    for digest in candidates:
        reference = f"{repository}@{digest}"
        completed = _run(["docker", "image", "rm", "--no-prune", reference])
        if completed.returncode != 0:
            detail = completed.stderr.strip() or f"exit {completed.returncode}"
            raise RetentionError(
                f"failed to remove deployment image {digest}: {detail}; "
                f"removed_before_failure={removed}"
            )
        removed.append(digest)
    available_after = _available_bytes(docker_root)

    return {
        "version": 1,
        "repository": repository,
        "active_digest": active_digest,
        "retained_digests": retained,
        "container_protected_digests": protected,
        "inventory_only_digests": inventory_only,
        "removed_digests": removed,
        "reclaimed_bytes": max(0, available_after - available_before),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--active-digest", required=True)
    parser.add_argument("--history-file", required=True, type=Path)
    parser.add_argument("--rollback-images", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        report = prune(
            args.repository,
            args.active_digest,
            args.history_file,
            args.rollback_images,
        )
    except RetentionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
