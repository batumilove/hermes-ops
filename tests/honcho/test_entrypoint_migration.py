"""Preserve the cron path and recovery bytes while selecting the installed CLI."""

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "migrate_monitor_entrypoint", ROOT / "scripts/honcho/migrate_monitor_entrypoint.py"
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def entrypoint(tmp_path):
    home = tmp_path / "profile with spaces"
    target = home / "scripts/honcho_monitor.py"
    target.parent.mkdir(parents=True)
    original = b"#!/usr/bin/env python3\nfrom hermes_cli.honcho_monitor import main\n"
    target.write_bytes(original)
    target.chmod(0o750)
    cli = tmp_path / "installed hermes"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "if '--help' not in sys.argv:\n"
        " print(json.dumps({'args': sys.argv[1:], 'home': os.environ['HERMES_HOME']}))\n"
    )
    cli.chmod(0o750)
    return home, target, original, cli


def test_migrated_script_invokes_installed_command_and_preserves_recovery(tmp_path, monkeypatch):
    home, target, original, cli = entrypoint(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    backup = migration.migrate(home, cli, hashlib.sha256(original).hexdigest())
    assert backup.read_bytes() == original
    assert target.stat().st_mode & 0o777 == 0o750
    result = subprocess.run([sys.executable, str(target)], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"args": ["honcho-ops", "monitor"], "home": str(home)}


def test_changed_entrypoint_is_refused_without_replacing_or_backing_it_up(tmp_path):
    home, target, original, cli = entrypoint(tmp_path)
    with pytest.raises(ValueError, match="changed"):
        migration.migrate(home, cli, "0" * 64)
    assert target.read_bytes() == original
    assert list(target.parent.glob("*.before-honcho-ops-*")) == []
