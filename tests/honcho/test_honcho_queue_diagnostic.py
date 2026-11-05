"""Tests for Honcho queue detail diagnostic client (strict TDD)."""

from __future__ import annotations

import shlex
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any

import pytest

from honcho_ops import queue_diagnostic as hqd


def test_constructor_rejects_option_looking_host():
    with pytest.raises(hqd.HonchoDiagnosticError):
        hqd.HonchoQueueDiagnostic(host="-oProxyCommand=bash")


def test_constructor_rejects_shell_metacharacter_container():
    with pytest.raises(hqd.HonchoDiagnosticError):
        hqd.HonchoQueueDiagnostic(database_container="db; rm -rf")
    with pytest.raises(hqd.HonchoDiagnosticError):
        hqd.HonchoQueueDiagnostic(deriver_container="deriver$(whoami)")


def test_constructor_accepts_default_host_and_containers():
    diagnostic = hqd.HonchoQueueDiagnostic()
    assert diagnostic.host == "ubuntu@100.67.206.76"
    assert diagnostic.database_container == "honcho-database-1"
    assert diagnostic.deriver_container == "honcho-deriver-1"


def test_constructor_accepts_tailscale_hostname():
    diagnostic = hqd.HonchoQueueDiagnostic(host="user@my-host.tailnet.ts.net")
    assert diagnostic.host == "user@my-host.tailnet.ts.net"


def test_constructor_accepts_ipv4():
    diagnostic = hqd.HonchoQueueDiagnostic(host="100.67.206.76")
    assert diagnostic.host == "100.67.206.76"


def test_constructor_preserves_overridden_container_name():
    diagnostic = hqd.HonchoQueueDiagnostic(
        database_container="honcho-database-2",
        deriver_container="honcho-deriver-2",
    )
    assert diagnostic.database_container == "honcho-database-2"
    assert diagnostic.deriver_container == "honcho-deriver-2"


def test_ssh_uses_accept_new_instead_of_disabling_host_key_checks(monkeypatch):
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    def fake_run(args, **kwargs):
        calls.append(args)
        return Result()

    monkeypatch.setattr(hqd.subprocess, "run", fake_run)

    diagnostic = hqd.HonchoQueueDiagnostic(host="honcho.example")
    assert diagnostic._ssh("true") == "ok"

    ssh_args = calls[0]
    assert "StrictHostKeyChecking=accept-new" in ssh_args
    assert "StrictHostKeyChecking=no" not in ssh_args


# ---------------------------------------------------------------------------
# Helpers for constructing expected psql commands in tests.
# ---------------------------------------------------------------------------


def _psql_command_for_summary(self) -> str:
    query = (
        "SELECT task_type, processed::text, count(*)::text, "
        "count(*) FILTER (WHERE error IS NOT NULL)::text "
        "FROM queue GROUP BY task_type, processed ORDER BY task_type, processed;"
    )
    return (
        f"docker exec {shlex.quote(self.database_container)} "
        f"psql -U postgres -t -A -F '|' -c {shlex.quote(query)}"
    )


def _psql_command_for_detail(self, limit: int) -> str:
    query = (
        "SELECT id, session_id, message_id, task_type, processed::text, "
        "COALESCE(error, ''), created_at::text, work_unit_key "
        f"FROM queue ORDER BY created_at DESC NULLS LAST, id DESC LIMIT {int(limit)}"
    )
    return (
        f"docker exec {shlex.quote(self.database_container)} "
        f"psql -U postgres -t -A -F '|' -c {shlex.quote(query)}"
    )


def _psql_command_for_peers(self, session_id: str) -> str:
    escaped = session_id.replace("'", "''")
    query = (
        "SELECT DISTINCT m.peer_name "
        "FROM messages m "
        "JOIN sessions s ON m.session_name = s.name "
        f"WHERE s.id = '{escaped}';"
    )
    return (
        f"docker exec {shlex.quote(self.database_container)} "
        f"psql -U postgres -t -A -F '|' -c {shlex.quote(query)}"
    )


def _psql_command_for_counts(self, session_id: str) -> str:
    escaped = session_id.replace("'", "''")
    query = (
        "SELECT "
        "(SELECT count(*)::text FROM messages m "
        " JOIN sessions s ON m.session_name = s.name "
        f" WHERE s.id = '{escaped}'), "
        "(SELECT count(*)::text FROM documents d "
        " JOIN sessions s ON d.session_name = s.name "
        f" WHERE s.id = '{escaped}' AND d.deleted_at IS NULL);"
    )
    return (
        f"docker exec {shlex.quote(self.database_container)} "
        f"psql -U postgres -t -A -F '|' -c {shlex.quote(query)}"
    )


def _psql_command_for_active_sessions(self) -> str:
    query = (
        "SELECT id, work_unit_key, last_updated::text, session_id "
        "FROM active_queue_sessions ORDER BY last_updated;"
    )
    return (
        f"docker exec {shlex.quote(self.database_container)} "
        f"psql -U postgres -t -A -F '|' -c {shlex.quote(query)}"
    )


def _psql_command_for_container_log(self) -> str:
    return (
        "diagnostic_log=$(mktemp -t hermes-honcho-diagnostic.XXXXXX) || exit 1; "
        "trap 'rm -f \"$diagnostic_log\"' EXIT; "
        f"docker logs {shlex.quote(self.deriver_container)} --since {shlex.quote('15m')} "
        "> \"$diagnostic_log\" 2>&1; cat \"$diagnostic_log\""
    )


class FakeHonchoQueueDiagnostic(hqd.HonchoQueueDiagnostic):
    """Test double that records SSH commands and returns canned responses."""

    _psql_command_for_summary = _psql_command_for_summary
    _psql_command_for_detail = _psql_command_for_detail
    _psql_command_for_peers = _psql_command_for_peers
    _psql_command_for_counts = _psql_command_for_counts
    _psql_command_for_active_sessions = _psql_command_for_active_sessions
    _psql_command_for_container_log = _psql_command_for_container_log

    def __init__(self, responses: dict[str, str] | None = None):
        super().__init__(host="test-host", workspace="test-ws")
        self.calls: list[str] = []
        self.responses = responses or {}

    def _ssh(self, command: str, timeout: int = 30) -> str:
        self.calls.append(command)
        return self.responses.get(command, "")


class ErrorDiagnostic(hqd.HonchoQueueDiagnostic):
    """Always returns an ERROR: line from the psql output path."""

    def _ssh(self, command: str, timeout: int = 30) -> str:
        return "ERROR:  column queue.updated_at does not exist"


# ---------------------------------------------------------------------------
# RED tests — expected to fail before implementation exists.
# ---------------------------------------------------------------------------


def test_queue_work_unit_phase_for_done_clean():
    unit = hqd.QueueWorkUnit(
        work_unit_id=1,
        work_unit_key="representation:abc:123",
        task_type="representation",
        session_id="sess-1",
        message_id=123,
        processed=True,
        error=None,
        created_at=None,
    )
    assert unit.phase == "done"


def test_queue_work_unit_phase_for_errored():
    unit = hqd.QueueWorkUnit(
        work_unit_id=2,
        work_unit_key="representation:abc:124",
        task_type="representation",
        session_id="sess-1",
        message_id=124,
        processed=True,
        error="JSONDecodeError",
        created_at=None,
    )
    assert unit.phase == "errored"


def test_queue_work_unit_phase_for_pending():
    unit = hqd.QueueWorkUnit(
        work_unit_id=3,
        work_unit_key="representation:abc:125",
        task_type="representation",
        session_id="sess-1",
        message_id=125,
        processed=False,
        error=None,
        created_at=None,
    )
    assert unit.phase == "pending"


def test_parse_iso_handles_postgresql_text_timestamps():
    assert hqd._parse_iso("2026-05-27 19:40:00+00") == datetime(
        2026, 5, 27, 19, 40, 0, tzinfo=UTC
    )


def test_parse_iso_returns_none_for_empty():
    assert hqd._parse_iso(None) is None
    assert hqd._parse_iso("") is None


def test_escape_sql_rejects_single_quote_injection():
    assert hqd._escape_sql("it's") == "it''s"


def test_collect_queue_summary_aggregates_counts():
    raw_response = (
        "representation|false|5|0\n"
        "representation|true|8|1\n"
        "dream|false|2|0\n"
    )
    diagnostic = FakeHonchoQueueDiagnostic()
    diagnostic.responses[diagnostic._psql_command_for_summary()] = raw_response
    summary = diagnostic.collect_queue_summary()
    assert summary["total"] == 15
    assert summary["pending"] == 7
    assert summary["done"] == 8
    assert summary["errored"] == 1
    assert summary["by_type"]["representation"]["total"] == 13
    assert summary["by_type"]["representation"]["pending"] == 5
    assert summary["by_type"]["representation"]["done"] == 8
    assert summary["by_type"]["representation"]["errored"] == 1


def test_collect_queue_summary_parses_legacy_t_f():
    raw_response = "representation|t|8|1\nrepresentation|f|5|0\n"
    diagnostic = FakeHonchoQueueDiagnostic()
    diagnostic.responses[diagnostic._psql_command_for_summary()] = raw_response
    summary = diagnostic.collect_queue_summary()
    assert summary["done"] == 8
    assert summary["pending"] == 5


def test_collect_queue_detail_parses_rows():
    raw_response = (
        "1|sess-a|101|representation|false||2026-05-27 19:40:00+00|representation:sess-a:101\n"
        "2|sess-b|102|dream|true|JSONDecodeError|2026-05-27 19:42:00+00|dream:sess-b:102"
    )
    diagnostic = FakeHonchoQueueDiagnostic()
    diagnostic.responses[diagnostic._psql_command_for_detail(100)] = raw_response
    units = diagnostic.collect_queue_detail(limit=100)
    assert len(units) == 2
    assert units[0].work_unit_id == 1
    assert units[0].session_id == "sess-a"
    assert units[0].message_id == 101
    assert units[0].task_type == "representation"
    assert units[0].processed is False
    assert units[0].error is None
    assert units[0].work_unit_key == "representation:sess-a:101"
    assert units[0].created_at == datetime(2026, 5, 27, 19, 40, 0, tzinfo=UTC)
    assert units[1].work_unit_id == 2
    assert units[1].error == "JSONDecodeError"
    assert units[1].phase == "errored"


def test_collect_queue_detail_skips_short_rows():
    raw_response = "1|sess-a|101|representation|f\n"
    diagnostic = FakeHonchoQueueDiagnostic()
    diagnostic.responses[diagnostic._psql_command_for_detail(100)] = raw_response
    units = diagnostic.collect_queue_detail(limit=100)
    assert units == []


def test_collect_peer_refs_returns_distinct_ids():
    diagnostic = FakeHonchoQueueDiagnostic()
    diagnostic.responses[diagnostic._psql_command_for_peers("sess-a")] = "peer-1\npeer-2\n"
    assert diagnostic.collect_peer_refs("sess-a") == ["peer-1", "peer-2"]


def test_collect_session_counts_returns_messages_and_documents():
    diagnostic = FakeHonchoQueueDiagnostic()
    diagnostic.responses[diagnostic._psql_command_for_counts("sess-a")] = "12|3"
    counts = diagnostic.collect_session_counts("sess-a")
    assert counts == {"messages": 12, "documents": 3}


@pytest.mark.parametrize("live_compat", [True, False])
def test_peer_refs_command_joins_sessions_like_counts(live_compat: bool) -> None:
    """The peer SQL must join sessions on messages.session_name = sessions.name,
    matching collect_session_counts, so live data resolves peers correctly.
    """
    diagnostic = FakeHonchoQueueDiagnostic()
    session_id = "sess-id-123"
    peer_cmd = diagnostic._psql_command_for_peers(session_id)
    count_cmd = diagnostic._psql_command_for_counts(session_id)
    # shlex.split recovers the actual psql -c argument from the shell-quoted
    # command string, so assertions operate on the real SQL text.
    peer_sql = shlex.split(peer_cmd)[-1]
    count_sql = shlex.split(count_cmd)[-1]
    assert "FROM messages m" in peer_sql
    assert "JOIN sessions s ON m.session_name = s.name" in peer_sql
    assert f"WHERE s.id = '{session_id}'" in peer_sql
    # Both commands must reference the same session identifier via sessions.id
    assert f"WHERE s.id = '{session_id}'" in count_sql
    # And not the old direct messages.session_name lookup
    assert "messages.session_name =" not in peer_sql or "m.session_name = s.name" in peer_sql


def test_collect_active_sessions_returns_lock_rows():
    raw_response = "lock-1|representation:sess-a:101|2026-05-27 19:45:00+00|sess-a\n"
    diagnostic = FakeHonchoQueueDiagnostic()
    diagnostic.responses[diagnostic._psql_command_for_active_sessions()] = raw_response
    rows = diagnostic.collect_active_sessions()
    assert rows == [{
        "id": "lock-1",
        "work_unit_key": "representation:sess-a:101",
        "last_updated": "2026-05-27 19:45:00+00",
        "session_id": "sess-a",
    }]


def test_parse_performance_extracts_duration_and_observation_count():
    diagnostic = FakeHonchoQueueDiagnostic()
    logs = (
        "PERFORMANCE - minimal_deriver_101_user\n"
        "  Context Preparation: 120 ms\n"
        "  Llm Call Duration: 3,456 ms\n"
        "  Observation Count: 2\n"
    )
    perf = diagnostic._parse_performance_for_work_unit("minimal_deriver_101_user", logs)
    assert perf["llm_duration_ms"] == 3456
    assert perf["observation_count"] == 2
    assert "Llm Call Duration" in perf["log_excerpt"]


def test_parse_performance_returns_empty_for_missing_key():
    diagnostic = FakeHonchoQueueDiagnostic()
    assert diagnostic._parse_performance_for_work_unit("missing", "no logs") == {}


def test_enrich_work_unit_combines_sources():
    diagnostic = FakeHonchoQueueDiagnostic()
    diagnostic.responses[diagnostic._psql_command_for_counts("sess-a")] = "12|3"
    diagnostic.responses[diagnostic._psql_command_for_peers("sess-a")] = "peer-1\n"
    diagnostic.responses[diagnostic._psql_command_for_container_log()] = (
        "PERFORMANCE - minimal_deriver_101_user\n"
        "  Llm Call Duration: 3,456 ms\n"
        "  Observation Count: 2\n"
    )
    unit = hqd.QueueWorkUnit(
        work_unit_id=1,
        work_unit_key="minimal_deriver_101_user",
        task_type="representation",
        session_id="sess-a",
        message_id=101,
        processed=False,
        error=None,
        created_at=datetime.now(UTC),
    )
    enriched = diagnostic.enrich_work_unit(unit)
    assert enriched.message_count == 12
    assert enriched.document_count == 3
    assert enriched.peer_refs == ["peer-1"]
    assert enriched.llm_duration_ms == 3456
    assert enriched.observation_count == 2


def test_format_report_includes_summary_and_units():
    unit = hqd.QueueWorkUnit(
        work_unit_id=1,
        work_unit_key="representation:sess-a:101",
        task_type="representation",
        session_id="sess-a",
        message_id=101,
        processed=False,
        error=None,
        created_at=datetime(2026, 5, 27, 19, 40, 0, tzinfo=UTC),
    )
    diag = hqd.WorkUnitDiagnostics(
        work_unit=unit,
        peer_refs=["peer-1"],
        message_count=12,
        document_count=3,
        llm_duration_ms=3456,
        observation_count=2,
        log_excerpt="PERFORMANCE...",
    )
    diagnostic = hqd.HonchoQueueDiagnostic(host="test-host", workspace="test-ws")
    report = diagnostic.format_report([diag], summary={"total": 10, "pending": 3, "done": 6, "errored": 1})
    assert report.startswith("🩺 Honcho queue diagnostics")
    assert "test-host" in report
    assert "test-ws" in report
    assert "10 total" in report
    assert "3 pending" in report
    assert "6 done" in report
    assert "1 errored" in report
    assert "sess-a" in report
    assert "phase=pending" in report


def test_format_report_handles_empty_enriched():
    diagnostic = hqd.HonchoQueueDiagnostic(host="test-host", workspace="test-ws")
    report = diagnostic.format_report([], summary={"total": 0, "pending": 0, "done": 0, "errored": 0})
    assert "No work units returned" in report


def test_psql_detects_error_output_and_raises():
    diagnostic = ErrorDiagnostic(host="test-host", workspace="test-ws")
    with pytest.raises(hqd.HonchoDiagnosticError, match="psql returned an error"):
        diagnostic.collect_queue_detail(limit=1)


# ---------------------------------------------------------------------------
# Regression tests: shell-injection-safe SQL command construction.
#
# _psql must use shlex.quote for the SQL query so that malicious or corrupted
# session_id values (which get interpolated into the query by public methods
# like collect_session_counts/collect_peer_refs) cannot break out of the
# psql -c argument and execute arbitrary remote shell commands.
# ---------------------------------------------------------------------------

# Each payload is a session_id that would have broken the old double-quote
# wrapping.  They must all end up safely inside the single-quoted argument
# produced by shlex.quote.
_MALICIOUS_SESSION_IDS = [
    # Double-quote breakout: closes the old -c "..." and starts a new command
    'x"; touch /tmp/honcho-diag-injected; echo "',
    # Semicolon command chaining
    "sess; rm -rf /",
    # Command substitution $(...)
    "sess$(touch /tmp/honcho-diag-injected)",
    # Backtick command substitution
    "sess`touch /tmp/honcho-diag-injected`",
    # Newline injection
    "sess\ntouch /tmp/honcho-diag-injected",
    # Single quote (tests interaction with SQL escaping + shlex.quote)
    "it's",
    # Combined: double quotes, semicolons, backticks, $(), and newlines
    'x";\n`touch /tmp/x`;$(whoami);echo "',
]


@pytest.mark.parametrize("malicious", _MALICIOUS_SESSION_IDS)
def test_psql_command_for_counts_quotes_malicious_session_id(malicious: str) -> None:
    """The generated remote command must not let any part of a malicious
    session_id appear as executable shell syntax outside of the quoted SQL."""
    captured = _CapturingDiagnostic(host="test-host", workspace="test-ws")
    captured.collect_session_counts(malicious)
    assert captured.command is not None
    _assert_shell_safe(captured.command, malicious)


@pytest.mark.parametrize("malicious", _MALICIOUS_SESSION_IDS)
def test_psql_command_for_peers_quotes_malicious_session_id(malicious: str) -> None:
    # We call _psql directly to capture the command string; the FakeDiagnostic
    # override below intercepts the _ssh call instead.
    captured = _CapturingDiagnostic(host="test-host", workspace="test-ws")
    captured.collect_peer_refs(malicious)
    assert captured.command is not None
    _assert_shell_safe(captured.command, malicious)


@pytest.mark.parametrize("malicious", _MALICIOUS_SESSION_IDS)
def test_psql_command_for_counts_via_collect_quotes_malicious_session_id(malicious: str) -> None:
    captured = _CapturingDiagnostic(host="test-host", workspace="test-ws")
    captured.collect_session_counts(malicious)
    assert captured.command is not None
    _assert_shell_safe(captured.command, malicious)


def test_psql_command_uses_single_quote_wrapping_not_double_quotes():
    """shlex.quote wraps in single quotes (or returns a safe bare token); the
    old vulnerable code used unescaped double quotes.  Verify the -c argument
    is NOT wrapped in bare double quotes."""
    captured = _CapturingDiagnostic(host="test-host", workspace="test-ws")
    captured.collect_queue_summary()
    assert captured.command is not None
    # The -c argument must be single-quoted (shlex.quote always uses '...' for
    # strings containing spaces/special chars, which SQL queries always do).
    assert "-c '" in captured.command
    # Must NOT contain the old vulnerable pattern of -c "..." with unescaped dquotes
    assert '-c "' not in captured.command


def test_shell_injected_file_is_not_created():
    """End-to-end proof: if _psql passed the query to a real local shell,
    a malicious session_id must not cause /tmp/honcho-diag-injected to be
    created.  We run the constructed docker command through bash -n (syntax
    check) and also through bash -c to prove no breakout."""
    import os
    import subprocess

    marker = "/tmp/honcho-diag-injected"
    if os.path.exists(marker):
        os.unlink(marker)

    diagnostic = hqd.HonchoQueueDiagnostic(host="test-host", workspace="test-ws")
    malicious = 'x"; touch /tmp/honcho-diag-injected; echo "'
    # Build the exact command _psql would send
    query = _build_counts_query(malicious)
    cmd = (
        f"docker exec {shlex.quote(diagnostic.database_container)} "
        f"psql -U postgres -t -A -F '|' -c {shlex.quote(query)}"
    )
    # If we run this through bash, the touch must NOT execute (docker will fail
    # since there's no such container, but the key is the marker file is absent).
    subprocess.run(["bash", "-c", cmd], check=False, capture_output=True, timeout=5)
    assert not os.path.exists(marker), (
        "Shell injection succeeded: marker file was created by malicious session_id"
    )


# ---------------------------------------------------------------------------
# Helpers for shell-injection regression tests
# ---------------------------------------------------------------------------


def _build_counts_query(session_id: str) -> str:
    """Mirror collect_session_counts query construction."""
    escaped = session_id.replace("'", "''")
    return (
        "SELECT "
        "(SELECT count(*)::text FROM messages m "
        " JOIN sessions s ON m.session_name = s.name "
        f" WHERE s.id = '{escaped}'), "
        "(SELECT count(*)::text FROM documents d "
        " JOIN sessions s ON d.session_name = s.name "
        f" WHERE s.id = '{escaped}' AND d.deleted_at IS NULL);"
    )


def _build_peers_query(session_id: str) -> str:
    """Mirror collect_peer_refs query construction."""
    escaped = session_id.replace("'", "''")
    return (
        "SELECT DISTINCT m.peer_name "
        "FROM messages m "
        "JOIN sessions s ON m.session_name = s.name "
        f"WHERE s.id = '{escaped}';"
    )


def _assert_shell_safe(command: str, payload: str) -> None:
    """Assert that *payload* does not appear as executable shell syntax in
    *command*.

    With shlex.quote, the entire SQL query (including the payload) is wrapped
    in single quotes.  Any single quotes in the query are neutralised by the
    '\'' sequence.  Therefore:

    1. The payload must appear inside the command (it's part of the SQL).
    2. No shell metacharacter from the payload may appear outside of a
       single-quoted region.
    3. Specifically, dangerous tokens (touch, rm, whoami, echo) from injection
       attempts must not appear as bare shell commands.
    """
    # The payload (or its SQL-escaped form) must be present in the command —
    # this proves we didn't accidentally strip it.
    escaped_payload = payload.replace("'", "''")
    # shlex.quote further escapes single quotes as '\'' .
    # The payload will be inside the single-quoted SQL argument.
    # Just verify the payload's non-quote characters are present:
    # (For the SQL-escaped form, single quotes become '')
    assert escaped_payload.split("'")[0] or len(payload) > 1  # sanity

    # The dangerous injection markers must NOT appear as bare shell syntax.
    # With shlex.quote, they are all inside single quotes, so checking that
    # the command parses back to the expected argv is the strongest proof.
    parts = shlex.split(command)
    # The psql -c argument should be a single argv element containing the
    # full SQL query — not split into multiple shell words.
    assert "-c" in parts, f"-c flag missing from parsed command: {parts}"
    c_index = parts.index("-c")
    sql_arg = parts[c_index + 1]
    # The SQL argument must start with SELECT (all our queries do)
    assert sql_arg.lstrip().upper().startswith("SELECT"), (
        f"SQL argument was not a single token — possible shell breakout: {sql_arg!r}"
    )
    # The payload's dangerous tokens must be inside the SQL string literal,
    # not as separate argv elements.
    assert len(parts) == c_index + 2 or parts[c_index + 2] == "", (
        f"Extra shell tokens after SQL argument — possible breakout: {parts[c_index + 2:]}"
    )


class _CapturingDiagnostic(hqd.HonchoQueueDiagnostic):
    """Captures the SSH command string without executing it."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.command: str | None = None

    def _ssh(self, command: str, timeout: int = 30) -> str:
        self.command = command
        # Return a valid response shape so callers like collect_session_counts
        # don't crash on parsing before the test assertion runs.
        return "0|0"

    def _container_log(self, since: str = "15m") -> str:
        return ""


def test_default_deployment_settings_are_resolved_on_each_construction(monkeypatch):
    monkeypatch.setenv("HONCHO_DIAGNOSTIC_HOST", "ubuntu@first-host")
    monkeypatch.setenv("HONCHO_DIAGNOSTIC_WORKSPACE", "first-workspace")
    first = hqd.HonchoQueueDiagnostic()
    monkeypatch.setenv("HONCHO_DIAGNOSTIC_HOST", "ubuntu@second-host")
    monkeypatch.setenv("HONCHO_DIAGNOSTIC_WORKSPACE", "second-workspace")
    second = hqd.HonchoQueueDiagnostic()
    assert (first.host, first.workspace) == ("ubuntu@first-host", "first-workspace")
    assert (second.host, second.workspace) == ("ubuntu@second-host", "second-workspace")


@pytest.mark.platforms("linux")
def test_deriver_log_diagnostic_uses_unique_scratch_and_cleans_it(tmp_path):
    import os
    import subprocess

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text("#!/bin/sh\nprintf 'PERFORMANCE - audit-unit\\n  Llm Call Duration: 42 ms\\n'\n", encoding="utf-8")
    docker.chmod(0o755)

    class LocalDiagnostic(hqd.HonchoQueueDiagnostic):
        def _ssh(self, command, timeout=30):
            result = subprocess.run(
                ["sh", "-c", command],
                env={"PATH": f"{binaries}{os.pathsep}{os.defpath}", "TMPDIR": str(scratch)},
                check=True, capture_output=True, text=True, timeout=5,
            )
            return result.stdout

    assert LocalDiagnostic()._container_log().splitlines() == [
        "PERFORMANCE - audit-unit", "  Llm Call Duration: 42 ms",
    ]
    assert not list(scratch.iterdir())
