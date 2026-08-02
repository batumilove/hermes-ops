from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "deploy" / "evaluate_deployment_gate.py"
WORKFLOW = REPO / ".github" / "workflows" / "build-image.yml"
EXPECTED_SHA = "e62465f3a0fcb05cd977ebd092c4e2ccc9d4aa51"


def _module() -> ModuleType:
    assert SCRIPT.exists(), "deployment gate evaluator is missing"
    spec = importlib.util.spec_from_file_location("evaluate_deployment_gate", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(*, name: str, status: str, conclusion: str | None, app_id: int = 15368) -> dict:
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "started_at": "2026-08-02T19:35:35Z",
        "app": {"id": app_id},
    }


def test_exact_live_sha_and_github_actions_shadow_aggregate_passes() -> None:
    payload = {
        "check_runs": [
            _run(name="shadow / shadow", status="completed", conclusion="success")
        ]
    }

    result = _module().evaluate_gate(
        expected_sha=EXPECTED_SHA,
        actual_sha=EXPECTED_SHA,
        required_context="shadow / shadow",
        payload=payload,
    )

    assert result == "completed:success"


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"check_runs": []}, "missing:pending"),
        (
            {
                "check_runs": [
                    _run(
                        name="shadow / shadow",
                        status="completed",
                        conclusion="failure",
                    )
                ]
            },
            "completed:failure",
        ),
        (
            {
                "check_runs": [
                    _run(
                        name="shadow / shadow",
                        status="completed",
                        conclusion="success",
                        app_id=999,
                    )
                ]
            },
            "missing:pending",
        ),
    ],
)
def test_missing_failed_and_wrong_app_checks_fail_closed(
    payload: dict, expected: str
) -> None:
    result = _module().evaluate_gate(
        expected_sha=EXPECTED_SHA,
        actual_sha=EXPECTED_SHA,
        required_context="shadow / shadow",
        payload=payload,
    )

    assert result == expected


def test_stale_branch_head_is_rejected_before_check_evaluation() -> None:
    with pytest.raises(ValueError, match="Refusing stale deployment"):
        _module().evaluate_gate(
            expected_sha=EXPECTED_SHA,
            actual_sha="0" * 40,
            required_context="shadow / shadow",
            payload={
                "check_runs": [
                    _run(
                        name="shadow / shadow",
                        status="completed",
                        conclusion="success",
                    )
                ]
            },
        )


def test_workflow_uses_reviewed_gate_evaluator() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "evaluate_deployment_gate.py" in text
    assert 'and run.get("app", {}).get("id") == 15368' not in text