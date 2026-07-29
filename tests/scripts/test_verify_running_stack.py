from __future__ import annotations

import json
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "deploy" / "verify_running_stack.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_running_stack", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_verify = _module()
AcceptanceError = _verify.AcceptanceError
accept_running_stack = _verify.accept_running_stack
verify_inspect = _verify.verify_inspect
verify_release_env = _verify.verify_release_env


IMAGE = "ghcr.io/batumilove/hermes-agent-deploy"
DIGEST = "sha256:" + "1" * 64
SOURCE_SHA = "a" * 40
ENVIRONMENT = "batumi-staging"


def _inspect(*, restart_count: int = 0, source_sha: str = SOURCE_SHA) -> dict:
    return {
        "Config": {
            "Image": f"{IMAGE}@{DIGEST}",
            "Labels": {"org.opencontainers.image.revision": source_sha},
            "Env": [
                f"HERMES_SOURCE_SHA={source_sha}",
                f"HERMES_DEPLOY_ENV={ENVIRONMENT}",
                "HERMES_HOME=/opt/data",
            ],
        },
        "State": {
            "Running": True,
            "Health": {"Status": "healthy"},
            "Pid": 1234,
            "StartedAt": "2026-07-24T16:00:00.000000000Z",
        },
        "RestartCount": restart_count,
    }


def test_release_env_requires_exact_candidate_identity(tmp_path: Path) -> None:
    release = tmp_path / "release.env"
    release.write_text(
        f"HERMES_IMAGE={IMAGE}@{DIGEST}\n"
        f"HERMES_DEPLOY_ENV={ENVIRONMENT}\n"
        f"HERMES_SOURCE_SHA={SOURCE_SHA}\n",
        encoding="utf-8",
    )

    assert verify_release_env(release, ENVIRONMENT, IMAGE, DIGEST, SOURCE_SHA) == {
        "HERMES_IMAGE": f"{IMAGE}@{DIGEST}",
        "HERMES_DEPLOY_ENV": ENVIRONMENT,
        "HERMES_SOURCE_SHA": SOURCE_SHA,
    }


@pytest.mark.parametrize(
    "bad_line",
    [
        "EXTRA=value",
        f"HERMES_SOURCE_SHA={'b' * 40}",
        f"HERMES_IMAGE={IMAGE}:mutable",
    ],
)
def test_release_env_rejects_extra_or_mismatched_state(tmp_path: Path, bad_line: str) -> None:
    release = tmp_path / "release.env"
    lines = [
        f"HERMES_IMAGE={IMAGE}@{DIGEST}",
        f"HERMES_DEPLOY_ENV={ENVIRONMENT}",
        f"HERMES_SOURCE_SHA={SOURCE_SHA}",
    ]
    if bad_line.startswith("EXTRA="):
        lines.append(bad_line)
    else:
        key = bad_line.split("=", 1)[0]
        lines = [line for line in lines if not line.startswith(f"{key}=")]
        lines.append(bad_line)
    release.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(AcceptanceError):
        verify_release_env(release, ENVIRONMENT, IMAGE, DIGEST, SOURCE_SHA)


def test_acceptance_rejects_a_deployment_root_that_resolves_to_root() -> None:
    with pytest.raises(AcceptanceError, match="absolute and non-root"):
        accept_running_stack(ENVIRONMENT, IMAGE, DIGEST, SOURCE_SHA, Path("/"))


def test_inspect_accepts_exact_healthy_fresh_runtime() -> None:
    evidence = verify_inspect(
        _inspect(),
        ENVIRONMENT,
        IMAGE,
        DIGEST,
        SOURCE_SHA,
    )

    assert evidence == {
        "container": "hermes-batumi-staging-gateway",
        "image": f"{IMAGE}@{DIGEST}",
        "source_sha": SOURCE_SHA,
        "running": True,
        "health": "healthy",
        "restart_count": 0,
        "pid": 1234,
        "started_at": "2026-07-24T16:00:00.000000000Z",
    }
    json.dumps(evidence)


@pytest.mark.parametrize(
    ("inspect", "message"),
    [
        (_inspect(restart_count=1), "restart count"),
        (_inspect(source_sha="b" * 40), "source SHA"),
        ({**_inspect(), "State": {**_inspect()["State"], "Running": False}}, "not running"),
        ({**_inspect(), "State": {**_inspect()["State"], "Health": {"Status": "unhealthy"}}}, "not healthy"),
    ],
)
def test_inspect_fails_closed_on_runtime_drift(inspect: dict, message: str) -> None:
    with pytest.raises(AcceptanceError, match=message):
        verify_inspect(inspect, ENVIRONMENT, IMAGE, DIGEST, SOURCE_SHA)
