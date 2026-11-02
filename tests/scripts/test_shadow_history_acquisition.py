"""Exercise the policy acquisition step against real disconnected Git histories."""
import json
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


@pytest.mark.parametrize("diverged", [False, True])
def test_policy_acquires_traversable_source_history(tmp_path, diverged):
    remote = tmp_path / "remote"
    remote.mkdir()
    git(remote, "init", "-q")
    git(remote, "config", "user.name", "Fixture")
    git(remote, "config", "user.email", "fixture@example.invalid")
    git(remote, "commit", "--allow-empty", "-qm", "upstream")
    base = git(remote, "rev-parse", "HEAD")
    if diverged:
        git(remote, "checkout", "--orphan", "divergent")
    for index in range(12):
        git(remote, "commit", "--allow-empty", "-qm", f"source {index}")
    source = git(remote, "rev-parse", "HEAD")
    git(remote, "checkout", "--orphan", "candidate")
    github = remote / ".github"
    github.mkdir()
    (github / "upstream-base").write_text(base + "\n")
    (github / "upstream-sync-provenance.json").write_text(json.dumps({"source_head": source, "fetch_depth": 8}))
    git(remote, "add", ".")
    git(remote, "commit", "-qm", "candidate")
    # The recorded source remains fetchable by SHA but is not an advertised branch.
    branches = git(remote, "for-each-ref", "--format=%(refname)", "refs/heads").splitlines()
    for branch in branches:
        if branch != "refs/heads/candidate":
            git(remote, "update-ref", "-d", branch)
    clone = tmp_path / "checkout"
    subprocess.run(["git", "clone", "--quiet", "--no-local", remote.as_uri(), str(clone)], check=True)
    workflow = yaml.safe_load((ROOT / ".github/workflows/shadow-validation.yml").read_text())
    step = next(step for step in workflow["jobs"]["policy"]["steps"] if step["name"] == "Fetch recorded upstream base")
    command = step["run"].replace("https://github.com/NousResearch/hermes-agent.git", remote.as_uri())
    subprocess.run(["bash", "-c", command], cwd=clone, check=True)
    # Mirror the caller's provenance acquisition: bounded only if already shallow.
    depth = ["--depth=8"] if git(clone, "rev-parse", "--is-shallow-repository") == "true" else []
    git(clone, "fetch", "--no-tags", *depth, "origin", source)
    result = subprocess.run(["git", "merge-base", "--is-ancestor", base, source], cwd=clone)
    assert result.returncode == (1 if diverged else 0)
    assert git(clone, "rev-parse", "--is-shallow-repository") == "false"
