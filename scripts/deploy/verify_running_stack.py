#!/usr/bin/env python3
"""Fail-closed acceptance for an exact Compose-deployed Hermes gateway."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


IMAGE_REPOSITORY = "ghcr.io/batumilove/hermes-agent-deploy"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
_ENVIRONMENT = re.compile(r"^[a-z][a-z0-9-]{1,31}$")


class AcceptanceError(RuntimeError):
    """The deployed runtime does not match the accepted release contract."""


def _exact_env(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AcceptanceError(f"cannot read release metadata: {exc}") from exc
    result: dict[str, str] = {}
    for line in lines:
        if not line or "=" not in line:
            raise AcceptanceError("release metadata contains an invalid line")
        key, value = line.split("=", 1)
        if not key or key in result:
            raise AcceptanceError("release metadata contains a duplicate or empty key")
        result[key] = value
    return result


def verify_release_env(
    path: Path,
    environment: str,
    image: str,
    digest: str,
    source_sha: str,
) -> dict[str, str]:
    expected = {
        "HERMES_IMAGE": f"{image}@{digest}",
        "HERMES_DEPLOY_ENV": environment,
        "HERMES_SOURCE_SHA": source_sha,
    }
    actual = _exact_env(path)
    if actual != expected:
        raise AcceptanceError("release metadata does not exactly match the candidate")
    return actual


def _runtime_env(entries: Any) -> dict[str, str]:
    if not isinstance(entries, list):
        raise AcceptanceError("container environment is not a list")
    result: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, str) or "=" not in entry:
            continue
        key, value = entry.split("=", 1)
        if key in {"HERMES_SOURCE_SHA", "HERMES_DEPLOY_ENV"}:
            if key in result:
                raise AcceptanceError(f"container has duplicate {key}")
            result[key] = value
    return result


def verify_inspect(
    inspect: dict[str, Any],
    environment: str,
    image: str,
    digest: str,
    source_sha: str,
) -> dict[str, Any]:
    container = f"hermes-{environment}-gateway"
    try:
        config = inspect["Config"]
        state = inspect["State"]
        actual_image = config["Image"]
        labels = config.get("Labels") or {}
        runtime_env = _runtime_env(config["Env"])
        running = state["Running"]
        health = state["Health"]["Status"]
        restart_count = inspect["RestartCount"]
        pid = state["Pid"]
        started_at = state["StartedAt"]
    except (KeyError, TypeError) as exc:
        raise AcceptanceError(f"container inspect is missing required state: {exc}") from exc

    expected_image = f"{image}@{digest}"
    if actual_image != expected_image:
        raise AcceptanceError("container image does not match the accepted digest")
    if labels.get("org.opencontainers.image.revision") != source_sha:
        raise AcceptanceError("container image label does not match the source SHA")
    if runtime_env.get("HERMES_SOURCE_SHA") != source_sha:
        raise AcceptanceError("container source SHA does not match the accepted source SHA")
    if runtime_env.get("HERMES_DEPLOY_ENV") != environment:
        raise AcceptanceError("container deployment environment mismatch")
    if running is not True:
        raise AcceptanceError("container is not running")
    if health != "healthy":
        raise AcceptanceError("container is not healthy")
    if not isinstance(restart_count, int) or isinstance(restart_count, bool) or restart_count != 0:
        raise AcceptanceError("container restart count is not zero")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise AcceptanceError("container PID is invalid")
    if not isinstance(started_at, str) or not started_at:
        raise AcceptanceError("container start time is missing")

    return {
        "container": container,
        "image": expected_image,
        "source_sha": source_sha,
        "running": True,
        "health": health,
        "restart_count": restart_count,
        "pid": pid,
        "started_at": started_at,
    }


def _run(argv: list[str]) -> str:
    try:
        completed = subprocess.run(
            argv,
            check=True,
            text=True,
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AcceptanceError(f"runtime probe failed: {argv[1] if len(argv) > 1 else argv[0]}") from exc
    return completed.stdout


def accept_running_stack(
    environment: str,
    image: str,
    digest: str,
    source_sha: str,
    deploy_root: Path,
) -> dict[str, Any]:
    if not _ENVIRONMENT.fullmatch(environment):
        raise AcceptanceError("invalid deployment environment")
    if image != IMAGE_REPOSITORY:
        raise AcceptanceError("unexpected image repository")
    if not _DIGEST.fullmatch(digest):
        raise AcceptanceError("invalid image digest")
    if not _SOURCE_SHA.fullmatch(source_sha):
        raise AcceptanceError("invalid source SHA")
    if not deploy_root.is_absolute():
        raise AcceptanceError("deployment root must be absolute and non-root")
    try:
        deploy_root = deploy_root.resolve()
    except (OSError, RuntimeError) as exc:
        raise AcceptanceError(f"cannot resolve deployment root: {exc}") from exc
    if deploy_root == Path("/"):
        raise AcceptanceError("deployment root must be absolute and non-root")

    verify_release_env(deploy_root / "release.env", environment, image, digest, source_sha)
    container = f"hermes-{environment}-gateway"
    try:
        payload = json.loads(_run(["docker", "inspect", container]))
    except json.JSONDecodeError as exc:
        raise AcceptanceError("container inspect returned invalid JSON") from exc
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise AcceptanceError("container inspect returned an unexpected object count")
    evidence = verify_inspect(payload[0], environment, image, digest, source_sha)

    supervisor = _run(
        ["docker", "exec", container, "/command/s6-svstat", "/run/service/main-hermes"]
    ).strip()
    if not supervisor.startswith("up "):
        raise AcceptanceError("gateway supervisor is not up")
    evidence["supervisor"] = supervisor
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--deploy-root", required=True, type=Path)
    args = parser.parse_args()
    try:
        evidence = accept_running_stack(
            args.environment,
            args.image,
            args.digest,
            args.source_sha,
            args.deploy_root,
        )
    except AcceptanceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
