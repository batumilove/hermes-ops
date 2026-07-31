from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "compose-deploy.yml"


def _workflow() -> tuple[dict, str]:
    text = WORKFLOW.read_text(encoding="utf-8")
    return yaml.safe_load(text), text


def test_deploy_requires_an_exact_digest_for_every_operation() -> None:
    workflow, text = _workflow()
    inputs = workflow[True]["workflow_call"]["inputs"]

    assert inputs["image_digest"]["required"] is True
    assert "[[ \"$IMAGE_DIGEST\" =~ ^sha256:[0-9a-f]{64}$ ]]" in text
    assert "if [[ \"$DEPLOY_OPERATION\" == deploy ]]" not in text


def test_workflow_executes_only_the_installed_host_controller() -> None:
    _workflow_data, text = _workflow()

    assert "/usr/local/libexec/hermes-deployment-controller apply" in text
    assert "sudo -n" in text
    assert "printf -v remote_command '%q '" in text
    assert "actions/checkout" not in text
    assert "scp " not in text
    assert "DEPLOY_ROOT" not in text
    assert "hermes-compose-deploy.sh" not in text


def test_controller_request_carries_auditable_github_identity() -> None:
    _workflow_data, text = _workflow()

    assert 'controller="github-actions:compose-deploy:${GITHUB_RUN_ID}"' in text
    assert 'authorization="github:${actor}"' in text
    assert '"$GITHUB_RUN_ID" "$GITHUB_RUN_ATTEMPT"' in text
