from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "build-image.yml"
TRIVY_ACTION_SHA = "ed142fd0673e97e23eac54620cfb913e5ce36c25"


def _steps() -> list[dict]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["publish-image"]["steps"]


def _named_step(name: str) -> tuple[int, dict]:
    for index, step in enumerate(_steps()):
        if step.get("name") == name:
            return index, step
    raise AssertionError(f"missing workflow step: {name}")


def test_fixable_high_and_critical_vulnerabilities_block_candidate_promotion() -> None:
    build_index, build = _named_step("Build and publish immutable image")
    attest_index, _attest = _named_step("Attest image provenance")
    scan_index, scan = _named_step("Block fixable high and critical vulnerabilities")
    promote_index, promote = _named_step("Promote scanned image to candidate")

    assert scan["uses"] == f"aquasecurity/trivy-action@{TRIVY_ACTION_SHA}"
    assert scan["with"] == {
        "scan-type": "image",
        "image-ref": "${{ env.IMAGE_REPOSITORY }}@${{ steps.resolve.outputs.digest || steps.build.outputs.digest }}",
        "vuln-type": "os,library",
        "severity": "HIGH,CRITICAL",
        "ignore-unfixed": True,
        "exit-code": "1",
        "timeout": "10m",
    }
    assert ":candidate" not in build["with"]["tags"]
    assert promote["env"]["TARGET_DIGEST"] == (
        "${{ steps.resolve.outputs.digest || steps.build.outputs.digest }}"
    )
    assert '"${IMAGE_REPOSITORY}:candidate"' in promote["run"]
    assert '"${IMAGE_REPOSITORY}@${TARGET_DIGEST}"' in promote["run"]
    assert build_index < attest_index < scan_index < promote_index


def test_reused_images_are_scanned_without_skipping_buildx_setup() -> None:
    _setup_index, setup = _named_step("Set up Docker Buildx")
    _scan_index, scan = _named_step("Block fixable high and critical vulnerabilities")

    assert "if" not in setup
    assert "if" not in scan
