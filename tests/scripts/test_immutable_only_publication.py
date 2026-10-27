"""Immutable-only must only remove promotion, never source admission."""
import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / '.github/workflows/build-image.yml'


def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def named(job, name):
    return next(s for s in workflow()['jobs'][job]['steps'] if s.get('name') == name)


def test_immutable_only_is_typed_opt_in_and_only_gates_mutable_promotion():
    w = workflow()
    inputs = w.get('on', w.get(True))['workflow_call']['inputs']
    assert inputs['immutable_only'] == {
        'description': 'Publish and verify only the source-SHA image; do not move candidate',
        'required': False, 'default': False, 'type': 'boolean',
    }
    promotion = named('publish-image', 'Promote scanned image to candidate')
    assert promotion['if'] == '${{ inputs.immutable_only != true }}'
    for job in w['jobs'].values():
        for step in job['steps']:
            if step is not promotion and step.get('name') != promotion['name']:
                assert 'immutable_only' not in step.get('if', '')
    assert w['jobs']['publish-image']['needs'] == 'wait-for-required-ci'
    assert 'if' not in w['jobs']['wait-for-required-ci']
    assert 'if' not in w['jobs']['publish-image']


@pytest.mark.parametrize('sha,context,repo,ref,branch,accepted', [
    ('a'*40, 'a'*40, 'batumilove/hermes-agent', 'refs/heads/batumi/live', 'batumi/live', True),
    ('a'*40, 'a'*40, 'batumilove/hermes-agent', 'refs/heads/rebuild/batumi-live', 'rebuild/batumi-live', True),
    ('a'*40, 'b'*40, 'batumilove/hermes-agent', 'refs/heads/batumi/live', 'batumi/live', False),
    ('a'*40, 'a'*40, 'attacker/fork', 'refs/heads/batumi/live', 'batumi/live', False),
    ('a'*40, 'a'*40, 'batumilove/hermes-agent', 'refs/tags/batumi/live', 'batumi/live', False),
    ('a'*40, 'a'*40, 'batumilove/hermes-agent', 'refs/heads/topic', 'batumi/live', False),
    ('a'*40, 'a'*40, 'batumilove/hermes-agent', 'refs/heads/batumi/live', 'rebuild/batumi-live', False),
    ('', '', 'batumilove/hermes-agent', 'refs/heads/batumi/live', 'batumi/live', False),
    ('A'*40, 'A'*40, 'batumilove/hermes-agent', 'refs/heads/batumi/live', 'batumi/live', False),
    ('a'*40+'\n', 'a'*40+'\n', 'batumilove/hermes-agent', 'refs/heads/batumi/live', 'batumi/live', False),
])
def test_actual_caller_binding_shell(sha, context, repo, ref, branch, accepted):
    step = named('wait-for-required-ci', 'Bind protected caller source')
    env = dict(os.environ, EXPECTED_SHA=sha, GITHUB_SHA=context,
               GITHUB_REPOSITORY=repo, GITHUB_REF=ref, source_branch=branch,
               GITHUB_REF_PROTECTED='true')
    result = subprocess.run(['bash', '-c', step['run']], env=env, capture_output=True)
    assert (result.returncode == 0) is accepted


@pytest.mark.parametrize('protected', ['', 'false', 'TRUE', 'true\n'])
def test_unprotected_caller_is_rejected(protected):
    step = named('wait-for-required-ci', 'Bind protected caller source')
    env = dict(os.environ, EXPECTED_SHA='a'*40, GITHUB_SHA='a'*40,
               GITHUB_REPOSITORY='batumilove/hermes-agent',
               GITHUB_REF='refs/heads/batumi/live', source_branch='batumi/live',
               GITHUB_REF_PROTECTED=protected)
    assert subprocess.run(['bash', '-c', step['run']], env=env, capture_output=True).returncode != 0


def test_admission_provenance_permissions_and_immutable_tag_are_retained():
    w = workflow()
    assert w['permissions'] == {'contents': 'read'}
    gate = w['jobs']['wait-for-required-ci']
    assert gate['permissions'] == {'contents': 'read', 'checks': 'read'}
    publish = w['jobs']['publish-image']
    assert publish['permissions'] == {'contents': 'read', 'packages': 'write',
        'id-token': 'write', 'attestations': 'write'}
    assert publish['concurrency'] == {
        'group': 'hermes-image-${{ github.repository }}-${{ inputs.source_sha }}',
        'cancel-in-progress': False,
    }
    steps = gate['steps']
    assert [s['name'] for s in steps].index('Bind protected caller source') < [s['name'] for s in steps].index('Wait for aggregate CI on the exact source')
    ci = named('wait-for-required-ci', 'Wait for aggregate CI on the exact source')['run']
    assert '--actual-sha "$latest"' in ci
    assert '--required-context "$required_context"' in ci
    assert 'completed:success) exit 0' in ci
    build = named('publish-image', 'Build and publish immutable image')
    assert build['if'] == "steps.resolve.outputs.exists == 'false'"
    assert build['with']['tags'].strip() == '${{ env.IMAGE_REPOSITORY }}:sha-${{ inputs.source_sha }}'
    assert build['with']['provenance'] == 'mode=max'
    assert build['with']['sbom'] is True
    assert named('publish-image', 'Resolve existing source-SHA image')['run'].count('--reference "sha-$SOURCE_SHA"') == 1
    reused = named('publish-image', 'Verify reused image provenance')
    assert reused['if'] == "steps.resolve.outputs.exists == 'true'"
    for flag in ('--source-digest "$EXPECTED_SHA"', '--source-ref "$GITHUB_REF"', '--signer-workflow', '--deny-self-hosted-runners'):
        assert flag in reused['run']
    assert 'if' not in named('publish-image', 'Block fixable high and critical vulnerabilities')
    attest = named('publish-image', 'Attest image provenance')
    assert attest['if'] == "steps.resolve.outputs.exists == 'false'"
    assert attest['with']['push-to-registry'] is True
    assert named('publish-image', 'Check out exact application source')['with']['ref'] == '${{ inputs.source_sha }}'
