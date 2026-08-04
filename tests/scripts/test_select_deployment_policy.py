from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "deploy" / "select_deployment_policy.py"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("select_deployment_policy", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("lane", "expected"),
    [
        ("live", ("batumi/live", "shadow / shadow")),
        ("candidate", ("rebuild/batumi-live", "shadow / shadow")),
    ],
)
def test_select_policy_returns_only_reviewed_ref_gate_pairs(
    lane: str, expected: tuple[str, str]
) -> None:
    assert _module().select_policy(lane) == expected


def test_select_policy_rejects_unknown_lane() -> None:
    with pytest.raises(ValueError, match="unknown deployment lane"):
        _module().select_policy("preview")
