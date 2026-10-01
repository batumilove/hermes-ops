from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / "scripts" / "deploy" / "prune_deployment_images.py"
IMAGE = "ghcr.io/batumilove/hermes-agent-deploy"
ACTIVE = "sha256:" + "1" * 64
PREVIOUS_ONE = "sha256:" + "2" * 64
PREVIOUS_TWO = "sha256:" + "3" * 64
OLD = "sha256:" + "4" * 64
CONTAINER_PROTECTED = "sha256:" + "5" * 64
FOREIGN = "sha256:" + "6" * 64
INVENTORY_ONLY = "sha256:" + "7" * 64


def _record(digest: str, image_id: str, repository: str = IMAGE) -> dict[str, str]:
    return {
        "Containers": "0",
        "CreatedAt": "2026-09-01 00:00:00 +0000 UTC",
        "Digest": digest,
        "ID": image_id,
        "Repository": repository,
        "Size": "4GB",
        "Tag": "<none>",
    }


def _fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker-rm.log"
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
if args[:2] == ["info", "--format"]:
    print(os.environ["FAKE_DOCKER_ROOT"])
    raise SystemExit(0)
if args[:2] == ["image", "ls"]:
    for row in json.loads(os.environ["FAKE_IMAGE_ROWS"]):
        print(json.dumps(row, sort_keys=True))
    raise SystemExit(0)
if args[:3] == ["container", "ls", "--all"]:
    print("container-one")
    raise SystemExit(0)
if args[:2] == ["inspect", "--format"]:
    for image_id in json.loads(os.environ["FAKE_CONTAINER_IMAGE_IDS"]):
        print(image_id)
    raise SystemExit(0)
if args[:2] == ["image", "rm"]:
    with open(os.environ["FAKE_RM_LOG"], "a", encoding="utf-8") as handle:
        handle.write(args[-1] + "\\n")
    if os.environ.get("FAKE_RM_FAIL") == "1":
        print("injected removal failure", file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(0)
print(f"unexpected docker invocation: {args}", file=sys.stderr)
raise SystemExit(64)
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return bin_dir, log


def _history(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                f"2026-09-01T00:00:00Z\tdeployed\tbatumi-staging\t{'a' * 40}\t{OLD}",
                f"2026-09-02T00:00:00Z\tpull-failed\tbatumi-staging\t{'b' * 40}\t{CONTAINER_PROTECTED}",
                f"2026-09-03T00:00:00Z\tdeployed\tbatumi-staging\t{'c' * 40}\t{PREVIOUS_TWO}",
                f"2026-09-04T00:00:00Z\trollback\tbatumi-staging\t{'d' * 40}\t{PREVIOUS_ONE}",
                f"2026-09-05T00:00:00Z\tdeployed\tbatumi-staging\t{'e' * 40}\t{ACTIVE}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _run(tmp_path: Path, rows: list[dict[str, str]], container_ids: list[str], *, fail_rm: bool = False):
    history = tmp_path / "history.tsv"
    _history(history)
    bin_dir, log = _fake_docker(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_IMAGE_ROWS": json.dumps(rows),
        "FAKE_CONTAINER_IMAGE_IDS": json.dumps(container_ids),
        "FAKE_RM_LOG": str(log),
        "FAKE_RM_FAIL": "1" if fail_rm else "0",
        "FAKE_DOCKER_ROOT": str(tmp_path),
    }
    result = subprocess.run(
        [
            "python3",
            str(HELPER),
            "--repository",
            IMAGE,
            "--active-digest",
            ACTIVE,
            "--history-file",
            str(history),
            "--rollback-images",
            "2",
        ],
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )
    removed = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result, removed


def test_retains_active_two_previous_and_every_container_referenced_image(tmp_path: Path) -> None:
    rows = [
        _record(ACTIVE, "sha256:" + "a" * 64),
        _record(PREVIOUS_ONE, "sha256:" + "b" * 64),
        _record(PREVIOUS_TWO, "sha256:" + "c" * 64),
        _record(OLD, "sha256:" + "d" * 64),
        _record(CONTAINER_PROTECTED, "sha256:" + "e" * 64),
        _record(INVENTORY_ONLY, "sha256:" + "7" * 64),
        _record(FOREIGN, "sha256:" + "f" * 64, "example.invalid/foreign"),
    ]

    result, removed = _run(tmp_path, rows, ["sha256:" + "e" * 64])

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["retained_digests"] == [ACTIVE, PREVIOUS_ONE, PREVIOUS_TWO]
    assert report["container_protected_digests"] == [CONTAINER_PROTECTED]
    assert report["inventory_only_digests"] == [INVENTORY_ONLY]
    assert report["removed_digests"] == [OLD]
    assert isinstance(report["reclaimed_bytes"], int)
    assert report["reclaimed_bytes"] >= 0
    assert removed == [f"{IMAGE}@{OLD}"]


def test_invalid_target_inventory_fails_closed_before_any_removal(tmp_path: Path) -> None:
    rows = [
        _record(ACTIVE, "sha256:" + "a" * 64),
        _record(OLD, "not-an-image-id"),
    ]

    result, removed = _run(tmp_path, rows, [])

    assert result.returncode != 0
    assert "unsafe target image inventory" in result.stderr
    assert removed == []


def test_missing_retained_rollback_digest_fails_closed_before_any_removal(tmp_path: Path) -> None:
    rows = [
        _record(ACTIVE, "sha256:" + "a" * 64),
        _record(PREVIOUS_ONE, "sha256:" + "b" * 64),
        _record(OLD, "sha256:" + "d" * 64),
    ]

    result, removed = _run(tmp_path, rows, [])

    assert result.returncode != 0
    assert "retained rollback digests are absent" in result.stderr
    assert removed == []


def test_removal_failure_is_reported_nonzero_with_partial_result(tmp_path: Path) -> None:
    rows = [
        _record(ACTIVE, "sha256:" + "a" * 64),
        _record(PREVIOUS_ONE, "sha256:" + "b" * 64),
        _record(PREVIOUS_TWO, "sha256:" + "c" * 64),
        _record(OLD, "sha256:" + "d" * 64),
    ]

    result, removed = _run(tmp_path, rows, [], fail_rm=True)

    assert result.returncode != 0
    assert "failed to remove deployment image" in result.stderr
    assert removed == [f"{IMAGE}@{OLD}"]
