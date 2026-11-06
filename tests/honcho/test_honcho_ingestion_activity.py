"""Local eligibility must follow the selected database, independent of remote health."""

import sqlite3
import time

import pytest

from honcho_ops import monitor as hm


def create_activity_database(path, *, active=False):
    """Create the metadata schema used by the monitor without a gateway process."""
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT);
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY, session_id TEXT, timestamp REAL,
                role TEXT, platform_message_id TEXT, finish_reason TEXT
            );
            INSERT INTO sessions VALUES ('human', 'telegram');
        """)
        if active:
            connection.execute(
                "INSERT INTO messages VALUES (1, 'human', ?, 'assistant', NULL, 'stop')",
                (time.time(),),
            )


@pytest.fixture
def offline_snapshot(monkeypatch, tmp_path):
    """Simulate an API outage while exercising real local database reads."""
    monkeypatch.setattr(hm, "STATE_PATH", tmp_path / "monitor.json")
    monkeypatch.setattr(
        hm, "ssh", lambda command, **kwargs:
        "honcho-deriver-1 Up\nhoncho-database-1 Up\nhoncho-redis-1 Up"
        if "compose ps" in command else "",
    )
    monkeypatch.setattr(hm, "collect_recent_deriver_errors", lambda: {})
    return hm.collect_snapshot


@pytest.mark.parametrize("filename", ["custom.db", "activity #?.db"])
def test_custom_database_filename_does_not_read_idle_sibling(tmp_path, monkeypatch, filename):
    """A normal state.db beside the override must not determine its classification."""
    custom = tmp_path / filename
    create_activity_database(tmp_path / "state.db")
    create_activity_database(custom, active=True)
    monkeypatch.setenv("HONCHO_MONITOR_HERMES_STATE_DB", str(custom))

    activity = hm.local_ingestion_activity()

    assert activity["status"] == "unknown"
    assert activity["reason"] == "completion_binding_unavailable"
    assert activity["local_latest_ts"] is None


def test_unresolved_database_home_does_not_hide_service_alerts(monkeypatch, offline_snapshot):
    """Invalid path evidence stays advisory and cannot prevent an API outage alert."""
    monkeypatch.setenv(
        "HONCHO_MONITOR_HERMES_STATE_DB", "~__hermes_monitor_no_such_account__/state.db"
    )

    snapshot, _ = offline_snapshot()

    assert snapshot.ingestion["status"] == "unknown"
    assert snapshot.ingestion["reason"] == "local_query_unavailable"
    assert "API down" in hm.build_alerts(snapshot)


def test_empty_local_window_stays_idle_without_remote_timestamp(tmp_path, monkeypatch, offline_snapshot):
    """Remote persistence age does not change a complete local activity observation."""
    database = tmp_path / "state.db"
    create_activity_database(database)
    monkeypatch.setenv("HONCHO_MONITOR_HERMES_STATE_DB", str(database))

    snapshot, _ = offline_snapshot()

    assert snapshot.ingestion["status"] == "idle"
    assert snapshot.ingestion["downstream_age_s"] is None
    assert "N/A (no recent activity" in hm.format_report(snapshot)
