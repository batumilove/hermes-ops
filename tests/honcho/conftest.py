"""Run the external monitor contracts with a disposable Hermes profile."""

from pathlib import Path
import sys

import pytest


PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "honcho-ops"
sys.path.insert(0, str(PLUGIN))


@pytest.fixture(autouse=True)
def isolated_profile(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(home / ".hermes"))


def pytest_configure(config):
    config.addinivalue_line("markers", "platforms(*names): run only on the named native hosts")


def pytest_collection_modifyitems(items):
    for item in items:
        marker = item.get_closest_marker("platforms")
        if marker is not None and sys.platform not in marker.args:
            item.add_marker(pytest.mark.skip(reason="requires a different native host"))
