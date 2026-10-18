"""Exercise the workflow's shell gate with Tailscale process responses."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


class TailnetReadinessTest(unittest.TestCase):
    def test_gate_rejects_unauthenticated_or_unreadable_status(self):
        workflow = yaml.safe_load(
            (Path(__file__).resolve().parents[2] / '.github/workflows/compose-deploy.yml').read_text()
        )
        gate = next((step['run'] for step in workflow['jobs']['deploy']['steps']
                    if step['name'] == 'Require authenticated tailnet before SSH'), '')
        cases = [
            ('running', {'BackendState': 'Running', 'Self': {'TailscaleIPs': ['100.64.0.1']}}, 0, True),
            ('logged-out', {'BackendState': 'NeedsLogin', 'Self': {'TailscaleIPs': []}}, 0, False),
            ('no-address', {'BackendState': 'Running', 'Self': {}}, 0, False),
            ('malformed', None, 0, False),
            ('cli-error', {'BackendState': 'Running', 'Self': {'TailscaleIPs': ['100.64.0.1']}}, 1, False),
        ]
        with tempfile.TemporaryDirectory() as scratch:
            cli = Path(scratch) / 'tailscale'
            cli.write_text('#!/bin/sh\nprintf "%s" "$TEST_STATUS"\nexit "$TEST_EXIT"\n')
            cli.chmod(0o700)
            for name, status, exit_code, accepted in cases:
                with self.subTest(name=name):
                    env = dict(os.environ, PATH=scratch + os.pathsep + os.environ['PATH'],
                               TEST_STATUS=json.dumps(status) if status is not None else 'invalid json',
                               TEST_EXIT=str(exit_code))
                    result = subprocess.run(['bash', '-c', gate + '\necho DEPLOY_REACHED'],
                                            env=env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode == 0, accepted)
                    self.assertEqual('DEPLOY_REACHED' in result.stdout, accepted)


if __name__ == '__main__':
    unittest.main()
