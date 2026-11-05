"""Installed CLI discovery and per-profile state through the real plugin loader."""

import json
import importlib
import os
import shutil
import subprocess
import sys

from conftest import PLUGIN


def installed_home(tmp_path):
    home = tmp_path / "profile"
    shutil.copytree(PLUGIN, home / "plugins" / "honcho-ops")
    (home / "config.yaml").write_text("plugins:\n  enabled: [honcho-ops]\n")
    return home


def test_installed_plugin_exposes_both_commands_without_network(tmp_path):
    home = installed_home(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "honcho-ops", "--help"],
        env=dict(os.environ, HERMES_HOME=str(home)),
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "monitor" in result.stdout
    assert "queue" in result.stdout
    assert not (home / "cache" / "honcho_monitor.json").exists()


def test_discovered_monitor_keeps_state_in_each_owning_profile(tmp_path, monkeypatch, capsys):
    from hermes_cli.plugins import PluginManager
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    home = installed_home(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = PluginManager()
    manager.discover_and_load()
    command = manager._cli_commands["honcho-ops"]
    import argparse
    parser = argparse.ArgumentParser()
    command["setup_fn"](parser)
    handler = command["handler_fn"]
    # Patch the actual module loaded by production, whose synthetic namespace
    # differs from the ordinary package used by the pure monitor tests.
    monitor = importlib.import_module(handler.__module__.rsplit(".", 1)[0] + ".monitor")
    snapshot = monitor.HonchoSnapshot(
        services={"ssh_ok": True}, pipeline={}, db={}, queue={},
        queue_by_type={}, errors={}, spark_goat={}, deriver={},
    )
    monkeypatch.setattr(monitor, "collect_snapshot", lambda: (snapshot, {"sample": "owned"}))
    monkeypatch.setattr(monitor, "should_emit_report", lambda *a, **k: False)
    for name in ("one", "two"):
        profile = tmp_path / name
        profile.mkdir()
        token = set_hermes_home_override(profile)
        try:
            assert handler(parser.parse_args(["monitor"])) == 0
        finally:
            reset_hermes_home_override(token)
        state = json.loads((profile / "cache" / "honcho_monitor.json").read_text())
        assert state["sample"] == "owned"
    assert capsys.readouterr().out == ""
    assert not (home / "cache" / "honcho_monitor.json").exists()
