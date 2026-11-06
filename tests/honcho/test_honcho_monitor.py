"""Tests for the Honcho production smoke monitor."""

from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from honcho_ops import monitor as hm


def test_unbound_completion_is_unknown_without_ingestion_alarm():
    activity = hm.classify_local_ingestion_activity([
        {"role": "user", "platform_message_id": "123"},
        {"role": "assistant", "finish_reason": "stop"},
    ])
    assert activity["status"] == "unknown"
    assert activity["local_latest_ts"] is None
    alerts = []
    snapshot = hm.HonchoSnapshot({}, {}, {}, {}, {}, {}, {}, {}, ingestion=activity)
    hm._ingestion_alerts(snapshot, alerts)
    assert alerts == []


def test_empty_truncated_activity_does_not_prove_idle():
    assert hm.classify_local_ingestion_activity([])["status"] == "idle"
    assert hm.classify_local_ingestion_activity([], truncated=True)["status"] == "unknown"


@pytest.mark.parametrize("human_rows, expected", [(0, "idle"), (1, "unknown"), (121, "unknown")])
def test_local_ingestion_reads_only_owning_profile_activity(tmp_path, human_rows, expected):
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as con:
        con.executescript("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT);
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY, session_id TEXT, timestamp REAL,
                role TEXT, platform_message_id TEXT, finish_reason TEXT
            );
            INSERT INTO sessions VALUES ('human', 'telegram'), ('scheduled', 'cron');
        """)
        con.execute(
            "INSERT INTO messages VALUES (1, 'scheduled', ?, 'assistant', NULL, 'stop')",
            (hm.time.time(),),
        )
        con.executemany(
            "INSERT INTO messages VALUES (?, 'human', ?, 'assistant', NULL, 'stop')",
            [(i + 2, hm.time.time()) for i in range(human_rows)],
        )
    activity = hm.local_ingestion_activity(db_path)
    assert activity["status"] == expected
    assert activity["local_latest_ts"] is None
    if human_rows == 121:
        assert activity["reason"] == "activity_window_truncated"


def test_missing_local_database_keeps_eligibility_unknown(tmp_path):
    activity = hm.local_ingestion_activity(tmp_path / "state.db")
    assert activity["status"] == "unknown"
    assert activity["local_latest_ts"] is None
    assert not (tmp_path / "state.db").exists()


def test_short_host_maps_both_dgx_spark_nodes():
    assert hm.short_host("http://192.168.10.211:8001/v1") == "spark-goat"
    assert hm.short_host("http://100.69.54.37:8001/v1") == "spark-goat"
    assert hm.short_host("http://100.71.155.95:11435/v1") == "spark-polarbear"
    assert hm.short_host("http://100.71.155.95:18081/v1") == "spark-polarbear"


def test_base_url_credentials_and_signed_query_are_redacted():
    secret_url = "https://alice:password@example.test:8443/v1?token=secret#private"

    assert hm.sanitize_base_url(secret_url) == "https://example.test:8443/v1"
    assert hm.short_host(secret_url) == "example.test:8443"

    parsed = hm.parse_pipeline_env(
        "EMBEDDING_MODEL_CONFIG__OVERRIDES__BASE_URL=" + secret_url
    )
    assert parsed["embedding"]["base_url"] == "https://example.test:8443/v1"

    snapshot = {
        "services": {},
        "pipeline": {
            "embedding": {"model": "m", "base_url": secret_url},
        },
        "db": {},
        "queue": {},
        "errors": {},
        "spark_goat": {},
        "deriver": {},
    }
    report = hm.format_report(snapshot)
    assert "alice" not in report
    assert "password" not in report
    assert "secret" not in report
    assert "private" not in report
    assert "base_url=https://example.test:8443/v1" in report


def test_honcho_target_defaults_to_lan_ssh_to_avoid_tailscale_approval_gate():
    assert hm.HONCHO_TARGET == "ubuntu@honcho.teleport.batumi.works"


def test_parse_pipeline_env_extracts_loaded_embedding_config():
    raw = "\n".join(
        [
            "EMBEDDING_MODEL_CONFIG__MODEL=qwen3-embedding-8b-1536",
            "EMBEDDING_MODEL_CONFIG__OVERRIDES__BASE_URL=http://100.69.54.37:11435/v1",
            "EMBEDDING_MODEL_CONFIG__DIMENSIONS_MODE=never",
            "EMBEDDING_VECTOR_DIMENSIONS=1536",
            "DERIVER_MODEL_CONFIG__MODEL=aeon-ultimate",
            "DERIVER_MODEL_CONFIG__OVERRIDES__BASE_URL=http://100.69.54.37:8001/v1",
            "DERIVER_WORKERS=1",
            "SUMMARY_MODEL_CONFIG__MODEL=qwen3.5-397b",
            "SUMMARY_MODEL_CONFIG__OVERRIDES__BASE_URL=http://100.110.104.77:8087/v1",
            "DREAM_DEDUCTION_MODEL_CONFIG__MODEL=aeon-ultimate",
            "DREAM_DEDUCTION_MODEL_CONFIG__OVERRIDES__BASE_URL=http://100.69.54.37:8001/v1",
            "DIALECTIC_LEVELS__low__MODEL_CONFIG__MODEL=aeon-ultimate",
            "DIALECTIC_LEVELS__low__MODEL_CONFIG__OVERRIDES__BASE_URL=http://100.69.54.37:8001/v1",
        ]
    )

    parsed = hm.parse_pipeline_env(raw)

    assert parsed["embedding"]["model"] == "qwen3-embedding-8b-1536"
    assert parsed["embedding"]["base_url"] == "http://100.69.54.37:11435/v1"
    assert parsed["embedding"]["dimensions_mode"] == "never"
    assert parsed["embedding"]["vector_dimensions"] == "1536"
    assert parsed["deriver"]["model"] == "aeon-ultimate"
    assert parsed["deriver"]["workers"] == "1"
    assert parsed["summary"]["base_url"] == "http://100.110.104.77:8087/v1"
    assert parsed["dialectic"]["base_url"] == "http://100.69.54.37:8001/v1"


def test_format_report_shows_loaded_config_dimensions_latency_and_alerts(tmp_path: Path):
    snapshot = {
        "services": {"api_ok": True, "deriver_up": True, "db_ok": True, "redis_ok": True},
        "pipeline": {
            "embedding": {
                "model": "text-embedding-3-small",
                "base_url": "https://api.openai.com/v1",
                "dimensions_mode": "",
                "vector_dimensions": "1536",
            },
            "deriver": {"model": "aeon-ultimate", "base_url": "http://100.69.54.37:8001/v1"},
            "summary": {"model": "qwen3.5-397b", "base_url": "http://100.110.104.77:8087/v1"},
            "dream": {"model": "aeon-ultimate", "base_url": "http://100.69.54.37:8001/v1"},
            "dialectic": {"model": "aeon-ultimate", "base_url": "http://100.69.54.37:8001/v1"},
        },
        "db": {
            "documents_total": 94,
            "documents_with_embeddings": 89,
            "documents_dims": 768,
            "messages_total": 80,
            "messages_with_embeddings": 80,
            "messages_dims": 768,
        },
        "queue": {"pending": 20, "done": 80},
        "queue_by_type": {"representation": {"pending": 15, "done": 75}, "reconciler": {"pending": 5, "done": 5}},
        "errors": {"save_representation": 2, "four_oh_one": 1},
        "spark_goat": {"ok": True, "latency_s": 1.25, "thinking": False},
        "deriver": {"runs_15m": 6, "last_duration_s": 39, "conclusions": 2602},
    }
    previous = {"queue_done": 70, "documents_total": 91, "queue_by_type": {"representation": {"pending": 10, "done": 70}}}

    report = hm.format_report(snapshot, previous_state=previous, now=hm.datetime(2026, 5, 28, 23, 51))

    assert report.startswith("🩺 Honcho —")
    assert "Embedding: text-embedding-3-small @ openai" in report
    assert "Embedding env: model=text-embedding-3-small base_url=https://api.openai.com/v1" in report
    assert "Embedding DB: docs 89/94 dims=768 · message-embedding rows 80/80 (global) dims=768" in report
    assert "Δ since previous sample: representation +5 · reconciler +5 · webhook +0 · dream +0 · docs +3" in report
    assert "Recent errors: save-repr=2 · 401=1" in report
    assert "spark-goat chat: 1.2s" in report
    assert "⚠️" in report
    assert "Embedding config looks like OpenAI fallback" in report
    assert "Representation queue advancing faster than documents" in report


def test_format_report_infers_vector_dimensions_from_matching_db_dims():
    snapshot = {
        "services": {"api_ok": True, "deriver_up": True, "db_ok": True, "redis_ok": True},
        "pipeline": {
            "embedding": {
                "model": "qwen3-embedding-8b-1536",
                "base_url": "http://100.69.54.37:11435/v1",
                "dimensions_mode": "never",
                "vector_dimensions": "",
            },
            "deriver": {"model": "gemma12b-polar-gpustack", "base_url": "http://100.71.155.95:18081/v1"},
            "summary": {"model": "mlx-community--Qwen3.5-4B-4bit", "base_url": "http://192.168.10.104:8000/v1"},
            "dream": {"model": "aeon-ultimate", "base_url": "http://100.69.54.37:8001/v1"},
            "dialectic": {"model": "mlx-community--Qwen3.5-9B-4bit", "base_url": "http://192.168.10.104:8000/v1"},
        },
        "db": {
            "documents_total": 88520,
            "documents_with_embeddings": 88520,
            "documents_dims": 1536,
            "messages_total": 13273,
            "messages_with_embeddings": 13270,
            "messages_dims": 1536,
        },
        "queue": {"pending": 0, "done": 153},
        "queue_by_type": {"representation": {"pending": 0, "done": 8}, "reconciler": {"pending": 0, "done": 137}},
        "errors": {"save_representation": 0, "four_oh_one": 0},
        "spark_goat": {"ok": True, "latency_s": 1.1, "thinking": False, "model": "aeon-ultimate"},
        "deriver": {"runs_15m": 1, "last_duration_s": 2, "conclusions": 0},
    }

    report = hm.format_report(snapshot, now=hm.datetime(2026, 7, 8, 14, 3))

    assert "vector_dims=1536 (inferred from DB)" in report


def test_state_round_trip(tmp_path: Path):
    state_path = tmp_path / "state.json"
    state = {"queue_done": 80, "documents_total": 100, "messages_total": 200}

    hm.save_state(state_path, state)

    assert hm.load_state(state_path) == state


def test_failed_atomic_state_replace_preserves_previous_baseline(tmp_path: Path, monkeypatch):
    state_path = tmp_path / "state.json"
    previous = {"queue_done": 80, "documents_total": 100}
    hm.save_state(state_path, previous)

    def fail_replace(_src, _dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(hm.os, "replace", fail_replace)

    with pytest.raises(OSError, match="No space left on device"):
        hm.save_state(state_path, {"queue_done": 81, "documents_total": 101})

    assert hm.load_state(state_path) == previous
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_local_observer_disk_pressure_is_reported_and_alerted():
    snapshot = hm.HonchoSnapshot(
        services={"api_ok": True, "deriver_up": True, "db_ok": True, "redis_ok": True},
        pipeline={},
        db={},
        queue={},
        queue_by_type={},
        errors={},
        spark_goat={},
        deriver={},
        observer={"disk_free_bytes": 512 * 1024 * 1024, "disk_used_percent": 99},
    )

    alerts = hm.build_alerts(snapshot)
    report = hm.format_report(snapshot, now=hm.datetime(2026, 7, 19, 12, 27))

    assert "Local observer disk critically low (512.0 MiB free, 99% used)" in alerts
    assert "💾 Observer disk: 512.0 MiB free · 99% used" in report


def test_main_reports_state_persistence_failure(monkeypatch, capsys):
    snapshot = hm.HonchoSnapshot(
        services={"api_ok": True, "deriver_up": True, "db_ok": True, "redis_ok": True},
        pipeline={},
        db={},
        queue={},
        queue_by_type={},
        errors={},
        spark_goat={},
        deriver={},
        observer={"disk_free_bytes": 20 * 1024**3, "disk_used_percent": 50},
    )
    monkeypatch.setattr(hm, "collect_snapshot", lambda: (snapshot, {}))
    monkeypatch.setattr(hm, "load_state", lambda _path: {})
    monkeypatch.setattr(hm, "save_state", lambda _path, _state: (_ for _ in ()).throw(OSError(28, "No space left on device")))

    assert hm.main() == 1
    assert "Local observer state save failed: [Errno 28] No space left on device" in capsys.readouterr().out


def test_failed_db_probe_does_not_replace_last_valid_counter_baseline():
    snapshot = hm.HonchoSnapshot(
        services={"db_ok": True},
        pipeline={},
        db={"probe_ok": False, "documents_total": 0, "messages_total": 0},
        queue={},
        queue_by_type={},
        errors={},
        spark_goat={},
        deriver={},
    )
    current = {"documents_total": 0, "messages_total": 0, "queue_done": 141}
    previous = {"documents_total": 182500, "messages_total": 19200, "queue_done": 138}

    merged = hm.preserve_last_valid_probe_state(snapshot, current, previous)

    assert merged["documents_total"] == 182500
    assert merged["messages_total"] == 19200
    assert merged["queue_done"] == 141


def test_ssh_uses_accept_new_instead_of_disabling_host_key_checks(monkeypatch):
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    def fake_run(args, **kwargs):
        calls.append(args)
        return Result()

    monkeypatch.setattr(hm.subprocess, "run", fake_run)
    monkeypatch.setattr(hm, "HONCHO_TARGET", "honcho.example")

    assert hm.ssh("true") == "ok"

    ssh_args = calls[0]
    assert "StrictHostKeyChecking=accept-new" in ssh_args
    assert "StrictHostKeyChecking=no" not in ssh_args


def test_ssh_uses_tbot_config_when_available(monkeypatch):
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    def fake_run(args, **kwargs):
        calls.append(args)
        return Result()

    monkeypatch.setattr(hm.subprocess, "run", fake_run)
    monkeypatch.setattr(hm, "HONCHO_TARGET", "honcho.example")
    monkeypatch.setattr(hm.Path, "exists", lambda self: str(self) == "/var/lib/tbot/hermes-cron-ssh/ssh_config")

    assert hm.ssh("true") == "ok"

    ssh_args = calls[0]
    assert ssh_args[:3] == ["ssh", "-F", "/var/lib/tbot/hermes-cron-ssh/ssh_config"]


def test_ssh_retries_one_transient_transport_failure(monkeypatch):
    calls: list[list[str]] = []

    class Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    results = [Result(255, stderr="connection timed out"), Result(0, stdout="ok\n")]

    def fake_run(args, **kwargs):
        calls.append(args)
        return results.pop(0)

    monkeypatch.setattr(hm.subprocess, "run", fake_run)
    monkeypatch.setattr(hm.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(hm, "HONCHO_TARGET", "honcho.example")

    assert hm.ssh("true") == "ok"
    assert len(calls) == 2


def test_queue_parse_handles_postgresql_boolean_text():
    # Reproduce the live failure: PostgreSQL returns 'true'/'false' text
    queue_raw = "false|15\ntrue|128\n"
    parsed = hm.parse_queue_raw(queue_raw)
    assert parsed == {"pending": 15, "done": 128}


def test_queue_parse_falls_back_to_f_t_letters():
    queue_raw = "f|12\nt|96\n"
    parsed = hm.parse_queue_raw(queue_raw)
    assert parsed == {"pending": 12, "done": 96}


def test_queue_parse_totals_from_three_column_per_type_rows():
    queue_raw = (
        "dream|false|4\n"
        "dream|true|4\n"
        "reconciler|false|10\n"
        "reconciler|true|115\n"
        "representation|false|5\n"
        "representation|true|6\n"
        "webhook|false|3\n"
        "webhook|true|5\n"
    )
    parsed = hm.parse_queue_raw(queue_raw)
    assert parsed == {"pending": 22, "done": 130}


def _spark_latency_snapshot(
    *, latency_s: float, active_dream_count: int, active_dream_age_s: int | None = None
) -> hm.HonchoSnapshot:
    if active_dream_age_s is None:
        active_dream_age_s = 14 * 60 if active_dream_count else 0
    return hm.HonchoSnapshot(
        services={"api_ok": True, "deriver_up": True, "db_ok": True, "redis_ok": True},
        pipeline={
            "embedding": {
                "model": "qwen3-embedding-8b-1536",
                "base_url": "http://192.168.10.211:11435/v1",
                "dimensions_mode": "never",
                "vector_dimensions": "1536",
            }
        },
        db={"documents_dims": 1536, "messages_dims": 1536},
        queue={},
        queue_by_type={},
        errors={"save_representation": 0, "four_oh_one": 0},
        spark_goat={
            "ok": True,
            "latency_s": latency_s,
            "thinking": False,
            "model": "aeon-ultimate",
        },
        deriver={
            "active_dream_count": active_dream_count,
            "active_dream_oldest_age_s": active_dream_age_s,
        },
    )


def test_first_spark_goat_latency_sample_during_fresh_dream_is_debounced():
    snapshot = _spark_latency_snapshot(latency_s=12.5, active_dream_count=1)
    alerts = hm.build_alerts(snapshot)

    assert alerts == []
    assert hm.should_emit_report(snapshot) is False


def test_second_spark_goat_latency_sample_during_fresh_dream_alerts():
    snapshot = _spark_latency_snapshot(latency_s=12.5, active_dream_count=1)
    previous_state = {"dream_contention_streak": 1}
    alerts = hm.build_alerts(
        snapshot,
        previous_state=previous_state,
    )

    assert "spark-goat dream contention (chat latency 12.5s)" in alerts
    assert hm.should_emit_report(snapshot, previous_state=previous_state) is True


def test_critical_spark_goat_latency_during_fresh_dream_alerts_immediately():
    alerts = hm.build_alerts(_spark_latency_snapshot(latency_s=30.0, active_dream_count=1))

    assert "spark-goat dream contention (chat latency 30.0s)" in alerts


def test_dream_contention_streak_tracks_only_fresh_dream_latency():
    snapshot = _spark_latency_snapshot(latency_s=12.5, active_dream_count=1)
    thinking_snapshot = _spark_latency_snapshot(latency_s=12.5, active_dream_count=1)
    thinking_snapshot.spark_goat["thinking"] = True

    assert hm.next_dream_contention_streak(snapshot, {"dream_contention_streak": 1}) == 2
    assert hm.next_dream_contention_streak(
        _spark_latency_snapshot(latency_s=1.0, active_dream_count=1),
        {"dream_contention_streak": 2},
    ) == 0
    assert hm.next_dream_contention_streak(
        thinking_snapshot,
        {"dream_contention_streak": 2},
    ) == 0


def test_report_marks_degraded_chat_amber_and_omits_zero_error_warning():
    report = hm.format_report(
        _spark_latency_snapshot(latency_s=12.5, active_dream_count=1),
        now=hm.datetime(2026, 8, 10, 16, 25),
    )

    assert "🟠 spark-goat chat: 12.5s" in report
    assert "Recent errors" not in report


def test_spark_goat_latency_without_active_dream_remains_degradation_alert():
    alerts = hm.build_alerts(_spark_latency_snapshot(latency_s=12.5, active_dream_count=0))

    assert "spark-goat chat latency degraded" in alerts
    assert not any("dream contention" in alert for alert in alerts)


def test_stale_active_dream_does_not_mask_chat_degradation():
    alerts = hm.build_alerts(
        _spark_latency_snapshot(
            latency_s=12.5,
            active_dream_count=1,
            active_dream_age_s=hm.DREAM_STALE_ACTIVE_SECONDS,
        )
    )

    assert "spark-goat chat latency degraded" in alerts
    assert not any("dream contention" in alert for alert in alerts)


def test_spark_goat_failure_is_blocking_alert():
    snapshot = {
        "services": {"api_ok": True, "deriver_up": True, "db_ok": True, "redis_ok": True},
        "pipeline": {
            "embedding": {
                "model": "qwen3-embedding-8b-1536",
                "base_url": "http://100.69.54.37:11435/v1",
                "dimensions_mode": "never",
                "vector_dimensions": "1536",
            },
            "deriver": {"model": "qwen3.5-397b", "base_url": "http://100.110.104.77:8087/v1"},
            "summary": {"model": "qwen3.5-397b", "base_url": "http://100.110.104.77:8087/v1"},
            "dream": {"model": "aeon-ultimate", "base_url": "http://100.69.54.37:8001/v1"},
            "dialectic": {"model": "aeon-ultimate", "base_url": "http://100.69.54.37:8001/v1"},
        },
        "db": {
            "documents_total": 100,
            "documents_with_embeddings": 95,
            "documents_dims": 1536,
            "messages_total": 80,
            "messages_with_embeddings": 80,
            "messages_dims": 1536,
        },
        "queue": {"pending": 0, "done": 128},
        "errors": {"save_representation": 0, "four_oh_one": 0},
        "spark_goat": {"ok": False, "latency_s": 0.1, "thinking": False, "model": "aeon-ultimate"},
        "deriver": {"runs_15m": 6, "last_duration_s": 39, "conclusions": 2602},
    }

    report = hm.format_report(snapshot, now=hm.datetime(2026, 5, 28, 23, 51))

    assert "📋 Queue: 0 pending · 128 done" in report
    assert "🔴 spark-goat chat: 0.1s" in report
    assert "spark-goat chat failed" in report
    assert "✅ All nominal" not in report


def test_missing_vector_dimensions_raises_alert_when_db_dims_are_unavailable():
    snapshot = {
        "services": {"api_ok": True, "deriver_up": True, "db_ok": True, "redis_ok": True},
        "pipeline": {
            "embedding": {
                "model": "qwen3-embedding-8b-1536",
                "base_url": "http://100.69.54.37:11435/v1",
                "dimensions_mode": "never",
                "vector_dimensions": "",
            },
            "deriver": {"model": "aeon-ultimate", "base_url": "http://100.69.54.37:8001/v1"},
            "summary": {"model": "qwen3.5-397b", "base_url": "http://100.110.104.77:8087/v1"},
            "dream": {"model": "aeon-ultimate", "base_url": "http://100.69.54.37:8001/v1"},
            "dialectic": {"model": "aeon-ultimate", "base_url": "http://100.69.54.37:8001/v1"},
        },
        "db": {
            "documents_total": 100,
            "documents_with_embeddings": 95,
            "documents_dims": 0,
            "messages_total": 80,
            "messages_with_embeddings": 80,
            "messages_dims": 0,
        },
        "queue": {"pending": 0, "done": 128},
        "queue_by_type": {"representation": {"pending": 0, "done": 128}},
        "errors": {"save_representation": 0, "four_oh_one": 0},
        "spark_goat": {"ok": True, "latency_s": 1.2, "thinking": False, "model": "aeon-ultimate"},
        "deriver": {"runs_15m": 6, "last_duration_s": 39, "conclusions": 2602},
    }

    alerts = hm.build_alerts(hm.HonchoSnapshot(**snapshot))

    assert "Embedding vector dimensions missing from env" in alerts


def test_missing_vector_dimensions_is_not_alert_when_db_dims_match():
    snapshot = {
        "services": {"api_ok": True, "deriver_up": True, "db_ok": True, "redis_ok": True},
        "pipeline": {
            "embedding": {
                "model": "qwen3-embedding-8b-1536",
                "base_url": "http://100.69.54.37:11435/v1",
                "dimensions_mode": "never",
                "vector_dimensions": "",
            },
            "deriver": {"model": "aeon-ultimate", "base_url": "http://100.69.54.37:8001/v1"},
            "summary": {"model": "qwen3.5-397b", "base_url": "http://100.110.104.77:8087/v1"},
            "dream": {"model": "aeon-ultimate", "base_url": "http://100.69.54.37:8001/v1"},
            "dialectic": {"model": "aeon-ultimate", "base_url": "http://100.69.54.37:8001/v1"},
        },
        "db": {
            "documents_total": 100,
            "documents_with_embeddings": 95,
            "documents_dims": 1536,
            "messages_total": 80,
            "messages_with_embeddings": 80,
            "messages_dims": 1536,
        },
        "queue": {"pending": 0, "done": 128},
        "queue_by_type": {"representation": {"pending": 0, "done": 128}},
        "errors": {"save_representation": 0, "four_oh_one": 0},
        "spark_goat": {"ok": True, "latency_s": 1.2, "thinking": False, "model": "aeon-ultimate"},
        "deriver": {"runs_15m": 6, "last_duration_s": 39, "conclusions": 2602},
    }

    alerts = hm.build_alerts(hm.HonchoSnapshot(**snapshot))

    assert "Embedding vector dimensions missing from env" not in alerts


def test_parse_queue_by_type_aggregates_per_task_type_counts():
    raw = (
        "dream|false|4\n"
        "dream|true|4\n"
        "reconciler|false|10\n"
        "reconciler|true|115\n"
        "representation|false|5\n"
        "representation|true|6\n"
        "webhook|false|3\n"
        "webhook|true|5\n"
    )
    parsed = hm.parse_queue_by_type_raw(raw)
    assert parsed["dream"] == {"pending": 4, "done": 4}
    assert parsed["reconciler"] == {"pending": 10, "done": 115}
    assert parsed["representation"] == {"pending": 5, "done": 6}
    assert parsed["webhook"] == {"pending": 3, "done": 5}


def test_reconciler_churn_does_not_trigger_queue_doc_drift_alert():
    """Live-shaped case: most queue churn is reconciler, not representation."""
    prev = {
        "queue_done": 80,
        "documents_total": 91,
        "queue_by_type": {
            "representation": {"pending": 5, "done": 75},
            "reconciler": {"pending": 0, "done": 5},
        },
    }
    snapshot = {
        "services": {"api_ok": True, "deriver_up": True, "db_ok": True, "redis_ok": True},
        "pipeline": {
            "embedding": {
                "model": "qwen3-embedding-8b-1536",
                "base_url": "http://100.69.54.37:11435/v1",
                "dimensions_mode": "never",
                "vector_dimensions": "1536",
            },
            "deriver": {"model": "aeon-ultimate", "base_url": "http://100.69.54.37:8001/v1"},
            "summary": {"model": "qwen3.5-397b", "base_url": "http://100.110.104.77:8087/v1"},
            "dream": {"model": "aeon-ultimate", "base_url": "http://100.69.54.37:8001/v1"},
            "dialectic": {"model": "aeon-ultimate", "base_url": "http://100.69.54.37:8001/v1"},
        },
        "db": {
            "documents_total": 97,
            "documents_with_embeddings": 95,
            "documents_dims": 1536,
            "messages_total": 80,
            "messages_with_embeddings": 80,
            "messages_dims": 1536,
        },
        "queue": {"pending": 0, "done": 210},
        "queue_by_type": {
            "representation": {"pending": 0, "done": 81},
            "reconciler": {"pending": 0, "done": 120},
            "dream": {"pending": 0, "done": 4},
            "webhook": {"pending": 0, "done": 5},
        },
        "errors": {"save_representation": 0, "four_oh_one": 0},
        "spark_goat": {"ok": True, "latency_s": 1.2, "thinking": False, "model": "aeon-ultimate"},
        "deriver": {"runs_15m": 6, "last_duration_s": 39, "conclusions": 2602},
    }

    report = hm.format_report(snapshot, previous_state=prev, now=hm.datetime(2026, 5, 28, 23, 51))

    assert "Δ since previous sample: representation +6 · reconciler +115 · webhook +5 · dream +4 · docs +6" in report
    assert "Representation queue advancing faster than documents" not in report


def test_one_item_representation_document_gap_does_not_trigger_drift_alert():
    """A single-sample visibility gap is normal and must not page the operator."""
    prev = {
        "documents_total": 485776,
        "queue_by_type": {"representation": {"pending": 0, "done": 100}},
    }
    snapshot = hm.HonchoSnapshot(
        services={"api_ok": True, "deriver_up": True, "db_ok": True, "redis_ok": True},
        pipeline={
            "embedding": {
                "model": "qwen3-embedding-8b-1536",
                "base_url": "http://192.168.10.211:11435/v1",
                "vector_dimensions": "1536",
            }
        },
        db={
            "probe_ok": True,
            "documents_total": 485795,
            "documents_dims": 1536,
            "messages_dims": 1536,
        },
        queue={"pending": 0, "done": 2050},
        queue_by_type={"representation": {"pending": 0, "done": 120}},
        errors={"save_representation": 0, "four_oh_one": 0},
        spark_goat={"ok": True, "latency_s": 1.5, "thinking": False},
        deriver={},
    )

    alerts = hm.build_alerts(snapshot, previous_state=prev)

    assert "Representation queue advancing faster than documents" not in alerts


def test_representation_queue_outpacing_docs_triggers_drift_alert():
    """Representation work should still be correlated with document growth."""
    prev = {
        "queue_done": 80,
        "documents_total": 91,
        "queue_by_type": {"representation": {"pending": 0, "done": 75}},
    }
    snapshot = {
        "services": {"api_ok": True, "deriver_up": True, "db_ok": True, "redis_ok": True},
        "pipeline": {
            "embedding": {
                "model": "qwen3-embedding-8b-1536",
                "base_url": "http://100.69.54.37:11435/v1",
                "dimensions_mode": "never",
                "vector_dimensions": "1536",
            },
            "deriver": {"model": "aeon-ultimate", "base_url": "http://100.69.54.37:8001/v1"},
            "summary": {"model": "qwen3.5-397b", "base_url": "http://100.110.104.77:8087/v1"},
            "dream": {"model": "aeon-ultimate", "base_url": "http://100.69.54.37:8001/v1"},
            "dialectic": {"model": "aeon-ultimate", "base_url": "http://100.69.54.37:8001/v1"},
        },
        "db": {
            "documents_total": 92,
            "documents_with_embeddings": 92,
            "documents_dims": 1536,
            "messages_total": 80,
            "messages_with_embeddings": 80,
            "messages_dims": 1536,
        },
        "queue": {"pending": 0, "done": 90},
        "queue_by_type": {"representation": {"pending": 0, "done": 85}},
        "errors": {"save_representation": 0, "four_oh_one": 0},
        "spark_goat": {"ok": True, "latency_s": 1.2, "thinking": False, "model": "aeon-ultimate"},
        "deriver": {"runs_15m": 6, "last_duration_s": 39, "conclusions": 2602},
    }

    alerts = hm.build_alerts(hm.HonchoSnapshot(**snapshot), previous_state=prev)

    assert "Representation queue advancing faster than documents" in alerts


def _representation_backlog_without_progress(
    *, active_count: int = 0, active_age_s: int = 0, runs_15m: int = 0,
    previous_stall_streak: int = 0,
) -> tuple[hm.HonchoSnapshot, dict]:
    previous_state = {
        "documents_total": 91594,
        "queue_by_type": {"representation": {"pending": 7050, "done": 373}},
        "representation_stall_streak": previous_stall_streak,
    }
    snapshot = hm.HonchoSnapshot(
        services={"api_ok": True, "deriver_up": True, "db_ok": True, "redis_ok": True},
        pipeline={
            "embedding": {
                "model": "qwen3-embedding-8b-1536",
                "base_url": "http://100.69.54.37:11435/v1",
                "dimensions_mode": "never",
                "vector_dimensions": "1536",
            },
        },
        db={
            "documents_total": 91594,
            "documents_with_embeddings": 91594,
            "documents_dims": 1536,
            "messages_total": 22130,
            "messages_with_embeddings": 22130,
            "messages_dims": 1536,
        },
        queue={"pending": 7365, "done": 658},
        queue_by_type={"representation": {"pending": 7052, "done": 373}},
        errors={"save_representation": 0, "four_oh_one": 0},
        spark_goat={"ok": True, "latency_s": 1.2, "thinking": False, "model": "aeon-ultimate"},
        deriver={
            "runs_15m": runs_15m,
            "last_duration_s": 0,
            "conclusions": 0,
            "active_count": active_count,
            "active_oldest_age_s": active_age_s,
            "active_oldest_work_unit_key": (
                "representation:hermes:session:hermes" if active_count else ""
            ),
        },
    )
    return snapshot, previous_state


def test_first_representation_backlog_sample_with_no_progress_does_not_alert():
    snapshot, previous_state = _representation_backlog_without_progress()

    alerts = hm.build_alerts(snapshot, previous_state=previous_state)

    assert "Deriver stalled: representation backlog with no progress" not in alerts
    assert hm.next_representation_stall_streak(snapshot, previous_state) == 1


def test_second_representation_backlog_sample_with_no_progress_alerts():
    snapshot, previous_state = _representation_backlog_without_progress(
        previous_stall_streak=1
    )

    alerts = hm.build_alerts(snapshot, previous_state=previous_state)

    assert "Deriver stalled: representation backlog with no progress" in alerts
    assert hm.next_representation_stall_streak(snapshot, previous_state) == 2


def test_recent_deriver_runs_reset_representation_stall_streak():
    snapshot, previous_state = _representation_backlog_without_progress(
        runs_15m=10, previous_stall_streak=1
    )

    alerts = hm.build_alerts(snapshot, previous_state=previous_state)

    assert "Deriver stalled: representation backlog with no progress" not in alerts
    assert hm.next_representation_stall_streak(snapshot, previous_state) == 0


def test_fresh_active_representation_suppresses_backlog_stall_alert():
    snapshot, previous_state = _representation_backlog_without_progress(
        active_count=1, active_age_s=2
    )

    alerts = hm.build_alerts(snapshot, previous_state=previous_state)

    assert "Deriver stalled: representation backlog with no progress" not in alerts
    assert not any("active work stale" in alert for alert in alerts)


def test_stale_representation_alerts_when_older_dream_is_still_within_grace():
    snapshot, previous_state = _representation_backlog_without_progress(
        active_count=2, active_age_s=1200
    )
    snapshot.deriver.update(
        {
            "active_oldest_work_unit_key": "dream:omni:hermes:session:hermes",
            "active_representation_count": 1,
            "active_representation_oldest_age_s": 601,
            "active_dream_count": 1,
            "active_dream_oldest_age_s": 1200,
        }
    )

    alerts = hm.build_alerts(snapshot, previous_state=previous_state)

    assert "Deriver stalled: representation backlog with no progress" not in alerts
    assert "Representation active work stale (1 active, oldest 10m)" in alerts
    assert not any(alert.startswith("Dream active work stale") for alert in alerts)


def test_fresh_dream_suppresses_representation_backlog_stall_with_single_worker():
    snapshot, previous_state = _representation_backlog_without_progress(
        active_count=1, active_age_s=9 * 60
    )
    snapshot.pipeline["deriver"] = {"workers": "1"}
    snapshot.deriver.update(
        {
            "active_oldest_work_unit_key": "dream:omni:hermes:session:hermes",
            "active_representation_count": 0,
            "active_representation_oldest_age_s": 0,
            "active_dream_count": 1,
            "active_dream_oldest_age_s": 9 * 60,
            "active_other_count": 0,
            "active_other_oldest_age_s": 0,
        }
    )

    alerts = hm.build_alerts(snapshot, previous_state=previous_state)

    assert "Deriver stalled: representation backlog with no progress" not in alerts
    assert not any("active work stale" in alert for alert in alerts)


def test_fresh_dream_does_not_suppress_representation_backlog_stall_with_multiple_workers():
    snapshot, previous_state = _representation_backlog_without_progress(
        active_count=1, active_age_s=9 * 60, previous_stall_streak=1
    )
    snapshot.pipeline["deriver"] = {"workers": "2"}
    snapshot.deriver.update(
        {
            "active_oldest_work_unit_key": "dream:omni:hermes:session:hermes",
            "active_representation_count": 0,
            "active_representation_oldest_age_s": 0,
            "active_dream_count": 1,
            "active_dream_oldest_age_s": 9 * 60,
            "active_other_count": 0,
            "active_other_oldest_age_s": 0,
        }
    )

    alerts = hm.build_alerts(snapshot, previous_state=previous_state)

    assert "Deriver stalled: representation backlog with no progress" in alerts
    assert not any("active work stale" in alert for alert in alerts)


@pytest.mark.parametrize(
    ("deriver_pipeline", "case"),
    [
        ({}, "missing"),
        ({"workers": "not-an-integer"}, "malformed"),
        ({"workers": "0"}, "zero"),
    ],
    ids=["missing-workers", "malformed-workers", "zero-workers"],
)
def test_fresh_dream_does_not_suppress_representation_backlog_when_worker_count_fails_closed(
    deriver_pipeline: dict[str, str], case: str
):
    snapshot, previous_state = _representation_backlog_without_progress(
        active_count=1, active_age_s=9 * 60, previous_stall_streak=1
    )
    snapshot.pipeline["deriver"] = deriver_pipeline
    snapshot.deriver.update(
        {
            "active_oldest_work_unit_key": "dream:omni:hermes:session:hermes",
            "active_representation_count": 0,
            "active_representation_oldest_age_s": 0,
            "active_dream_count": 1,
            "active_dream_oldest_age_s": 9 * 60,
            "active_other_count": 0,
            "active_other_oldest_age_s": 0,
        }
    )

    alerts = hm.build_alerts(snapshot, previous_state=previous_state)

    assert "Deriver stalled: representation backlog with no progress" in alerts, case
    assert not any("active work stale" in alert for alert in alerts)


def test_stale_dream_still_alerts_with_single_worker():
    snapshot, previous_state = _representation_backlog_without_progress(
        active_count=1, active_age_s=30 * 60
    )
    snapshot.pipeline["deriver"] = {"workers": "1"}
    snapshot.deriver.update(
        {
            "active_oldest_work_unit_key": "dream:omni:hermes:session:hermes",
            "active_representation_count": 0,
            "active_representation_oldest_age_s": 0,
            "active_dream_count": 1,
            "active_dream_oldest_age_s": 30 * 60,
            "active_other_count": 0,
            "active_other_oldest_age_s": 0,
        }
    )

    alerts = hm.build_alerts(snapshot, previous_state=previous_state)

    assert "Dream active work stale (1 active, oldest 30m)" in alerts


def test_stale_representation_and_other_work_have_distinct_alerts():
    snapshot, previous_state = _representation_backlog_without_progress(
        active_count=2, active_age_s=601
    )
    snapshot.deriver.update(
        {
            "active_oldest_work_unit_key": "representation:hermes:session:hermes",
            "active_representation_count": 1,
            "active_representation_oldest_age_s": 601,
            "active_dream_count": 0,
            "active_dream_oldest_age_s": 0,
            "active_other_count": 1,
            "active_other_oldest_age_s": 601,
        }
    )

    alerts = hm.build_alerts(snapshot, previous_state=previous_state)

    assert "Representation active work stale (1 active, oldest 10m)" in alerts
    assert "Other active work stale (1 active, oldest 10m)" in alerts
    assert len(alerts) == len(set(alerts))


def _ssh_failure_snapshot() -> hm.HonchoSnapshot:
    return hm.HonchoSnapshot(
        services=hm._parse_service_status("__SSH_ERROR__ rc=255 stderr=connection timed out"),
        pipeline={
            "embedding": {"model": "", "base_url": "", "dimensions_mode": "", "vector_dimensions": ""},
            "deriver": {"model": "", "base_url": ""},
            "summary": {"model": "", "base_url": ""},
            "dream": {"model": "", "base_url": ""},
            "dialectic": {"model": "", "base_url": ""},
        },
        db={"probe_ok": False, "documents_total": 0, "documents_with_embeddings": 0, "documents_dims": 0, "messages_total": 0, "messages_with_embeddings": 0, "messages_dims": 0},
        queue={"pending": 0, "done": 0},
        queue_by_type={},
        errors={"save_representation": 0, "four_oh_one": 0},
        spark_goat={"ok": True, "latency_s": 1.2, "thinking": False, "model": "aeon-ultimate"},
        deriver={"runs_15m": 0, "last_duration_s": 0, "conclusions": 0},
    )


def test_first_ssh_transport_failure_is_silent_and_services_are_unknown():
    snapshot = _ssh_failure_snapshot()

    alerts = hm.build_alerts(snapshot, previous_state={"ssh_failure_streak": 0})

    assert alerts == []
    assert hm._service_row(snapshot.services) == "⚪ API ⚪ Deriver ⚪ DB ⚪ Redis"


def test_second_ssh_transport_failure_alerts_with_sanitized_reason():
    snapshot = _ssh_failure_snapshot()

    alerts = hm.build_alerts(snapshot, previous_state={"ssh_failure_streak": 1})

    assert alerts == ["Honcho SSH probe failed (2 consecutive): rc=255 stderr=connection timed out"]
    assert "API down" not in alerts
    assert "DB down" not in alerts
    assert "Redis down" not in alerts
    assert "Deriver down" not in alerts


def test_ssh_failure_report_reuses_last_valid_remote_sample():
    snapshot = _ssh_failure_snapshot()
    previous_state = {
        "ssh_failure_streak": 1,
        "pipeline": {
            "embedding": {"model": "qwen3-embedding-8b-1536", "base_url": "http://192.168.10.211:11435/v1", "dimensions_mode": "never", "vector_dimensions": "1536"},
            "deriver": {"model": "gemma12b-polar-gpustack", "base_url": "http://100.71.155.95:18081/v1"},
            "summary": {"model": "qwen3.5-4b", "base_url": "http://192.168.10.104:8000/v1"},
            "dream": {"model": "aeon-ultimate", "base_url": "http://192.168.10.211:8001/v1"},
            "dialectic": {"model": "qwen3.5-9b", "base_url": "http://192.168.10.104:8000/v1"},
        },
        "db": {"probe_ok": True, "documents_total": 187307, "documents_with_embeddings": 187307, "documents_dims": 1536, "messages_total": 19532, "messages_with_embeddings": 19529, "messages_dims": 1536},
        "queue": {"pending": 509, "done": 43},
        "queue_by_type": {"representation": {"pending": 509, "done": 43}},
        "errors": {"save_representation": 0, "four_oh_one": 0},
        "deriver": {"runs_15m": 6, "last_duration_s": 8, "conclusions": 100, "active_count": 1, "active_oldest_age_s": 18},
    }

    restored = hm.restore_last_valid_remote_sample(snapshot, previous_state)
    report = hm.format_report(restored, previous_state=previous_state, now=hm.datetime(2026, 7, 17, 7, 30))

    assert "⚪ API ⚪ Deriver ⚪ DB ⚪ Redis" in report
    assert "Remote sample: stale (SSH unavailable; last valid counters/config shown)" in report
    assert "Embedding: qwen3-embedding-8b-1536 @ spark-goat" in report
    assert "Embedding DB: docs 187307/187307 dims=1536 · message-embedding rows 19529/19532 (global) dims=1536" in report
    assert "Queue: 509 pending · 43 done" in report


def test_db_stats_query_samples_vector_dimensions_instead_of_scanning_every_vector():
    query = hm.build_db_stats_query()

    assert "min(array_length" not in query.lower()
    assert query.count("LIMIT 1") == 2
    assert "count(*) FILTER (WHERE deleted_at IS NULL)" in query
    assert "count(*)::text FROM message_embeddings" in query


def test_db_stats_probe_failure_does_not_fake_zero_embedding_dims():
    snapshot = {
        "services": {"api_ok": True, "deriver_up": True, "db_ok": True, "redis_ok": True},
        "pipeline": {
            "embedding": {
                "model": "qwen3-embedding-8b-1536",
                "base_url": "http://100.69.54.37:11435/v1",
                "dimensions_mode": "never",
                "vector_dimensions": "",
            },
            "deriver": {"model": "gemma12b-polar-gpustack", "base_url": "http://100.71.155.95:18081/v1"},
            "summary": {"model": "mlx-community--Qwen3.5-4B-4bit", "base_url": "http://192.168.10.104:8000/v1"},
            "dream": {"model": "aeon-ultimate", "base_url": "http://100.69.54.37:8001/v1"},
            "dialectic": {"model": "mlx-community--Qwen3.5-9B-4bit", "base_url": "http://192.168.10.104:8000/v1"},
        },
        "db": {
            "probe_ok": False,
            "probe_error": "ERROR: function array_length(vector, integer) does not exist",
            "documents_total": 0,
            "documents_with_embeddings": 0,
            "documents_dims": 0,
            "messages_total": 0,
            "messages_with_embeddings": 0,
            "messages_dims": 0,
        },
        "queue": {"pending": 0, "done": 128},
        "queue_by_type": {"representation": {"pending": 0, "done": 128}},
        "errors": {"save_representation": 0, "four_oh_one": 0},
        "spark_goat": {"ok": True, "latency_s": 1.2, "thinking": False, "model": "aeon-ultimate"},
        "deriver": {"runs_15m": 6, "last_duration_s": 39, "conclusions": 2602},
    }

    alerts = hm.build_alerts(hm.HonchoSnapshot(**snapshot), previous_state={})

    assert alerts == ["DB stats probe failed"]


def test_stale_active_deriver_work_triggers_alert():
    snapshot = {
        "services": {"api_ok": True, "deriver_up": True, "db_ok": True, "redis_ok": True},
        "pipeline": {
            "embedding": {
                "model": "qwen3-embedding-8b-1536",
                "base_url": "http://100.69.54.37:11435/v1",
                "dimensions_mode": "never",
                "vector_dimensions": "1536",
            },
            "deriver": {"model": "gemma12b-polar-gpustack", "base_url": "http://100.71.155.95:18081/v1"},
            "summary": {"model": "mlx-community--Qwen3.5-4B-4bit", "base_url": "http://192.168.10.104:8000/v1"},
            "dream": {"model": "aeon-ultimate", "base_url": "http://100.69.54.37:8001/v1"},
            "dialectic": {"model": "mlx-community--Qwen3.5-9B-4bit", "base_url": "http://192.168.10.104:8000/v1"},
        },
        "db": {
            "documents_total": 96179,
            "documents_with_embeddings": 96179,
            "documents_dims": 1536,
            "messages_total": 22153,
            "messages_with_embeddings": 22153,
            "messages_dims": 1536,
        },
        "queue": {"pending": 6522, "done": 1565},
        "queue_by_type": {"representation": {"pending": 6247, "done": 1201}},
        "errors": {"save_representation": 0, "four_oh_one": 0},
        "spark_goat": {"ok": True, "latency_s": 1.2, "thinking": False, "model": "aeon-ultimate"},
        "deriver": {
            "runs_15m": 0,
            "last_duration_s": 0,
            "conclusions": 0,
            "active_count": 3,
            "active_oldest_age_s": 901,
            "active_oldest_work_unit_key": "representation:session-1",
        },
    }

    alerts = hm.build_alerts(hm.HonchoSnapshot(**snapshot), previous_state={})

    assert "Deriver active work stale (3 active, oldest 15m)" in alerts


def test_active_dream_work_uses_longer_stale_threshold():
    snapshot = {
        "services": {"api_ok": True, "deriver_up": True, "db_ok": True, "redis_ok": True},
        "pipeline": {
            "embedding": {
                "model": "qwen3-embedding-8b-1536",
                "base_url": "http://100.69.54.37:11435/v1",
                "dimensions_mode": "never",
                "vector_dimensions": "1536",
            },
        },
        "db": {
            "documents_total": 96179,
            "documents_with_embeddings": 96179,
            "documents_dims": 1536,
            "messages_total": 22153,
            "messages_with_embeddings": 22153,
            "messages_dims": 1536,
        },
        "queue": {"pending": 2, "done": 3},
        "queue_by_type": {"dream": {"pending": 1, "done": 3}},
        "errors": {"save_representation": 0, "four_oh_one": 0},
        "spark_goat": {"ok": True, "latency_s": 1.2, "thinking": False, "model": "aeon-ultimate"},
        "deriver": {
            "runs_15m": 0,
            "last_duration_s": 0,
            "conclusions": 0,
            "active_count": 1,
            "active_oldest_age_s": 901,
            "active_oldest_work_unit_key": "dream:omni:hermes:407304892:hermes",
        },
    }

    alerts = hm.build_alerts(hm.HonchoSnapshot(**snapshot), previous_state={})

    assert not any("active work stale" in alert for alert in alerts)


def test_stale_active_dream_work_is_labeled_as_dream():
    snapshot = {
        "services": {"api_ok": True, "deriver_up": True, "db_ok": True, "redis_ok": True},
        "pipeline": {
            "embedding": {
                "model": "qwen3-embedding-8b-1536",
                "base_url": "http://100.69.54.37:11435/v1",
                "dimensions_mode": "never",
                "vector_dimensions": "1536",
            },
        },
        "db": {
            "documents_total": 96179,
            "documents_with_embeddings": 96179,
            "documents_dims": 1536,
            "messages_total": 22153,
            "messages_with_embeddings": 22153,
            "messages_dims": 1536,
        },
        "queue": {"pending": 2, "done": 3},
        "queue_by_type": {"dream": {"pending": 1, "done": 3}},
        "errors": {"save_representation": 0, "four_oh_one": 0},
        "spark_goat": {"ok": True, "latency_s": 1.2, "thinking": False, "model": "aeon-ultimate"},
        "deriver": {
            "runs_15m": 0,
            "last_duration_s": 0,
            "conclusions": 0,
            "active_count": 1,
            "active_oldest_age_s": 1801,
            "active_oldest_work_unit_key": "dream:omni:hermes:407304892:hermes",
        },
    }

    alerts = hm.build_alerts(hm.HonchoSnapshot(**snapshot), previous_state={})

    assert "Dream active work stale (1 active, oldest 30m)" in alerts


def test_legacy_state_without_queue_by_type_does_not_crash_or_false_alert():
    """Old state files lack queue_by_type; drift alert should be skipped."""
    prev = {"queue_done": 80, "documents_total": 91}
    snapshot = {
        "services": {"api_ok": True, "deriver_up": True, "db_ok": True, "redis_ok": True},
        "pipeline": {
            "embedding": {
                "model": "qwen3-embedding-8b-1536",
                "base_url": "http://100.69.54.37:11435/v1",
                "dimensions_mode": "never",
                "vector_dimensions": "1536",
            },
            "deriver": {"model": "aeon-ultimate", "base_url": "http://100.69.54.37:8001/v1"},
            "summary": {"model": "qwen3.5-397b", "base_url": "http://100.110.104.77:8087/v1"},
            "dream": {"model": "aeon-ultimate", "base_url": "http://100.69.54.37:8001/v1"},
            "dialectic": {"model": "aeon-ultimate", "base_url": "http://100.69.54.37:8001/v1"},
        },
        "db": {
            "documents_total": 92,
            "documents_with_embeddings": 92,
            "documents_dims": 1536,
            "messages_total": 80,
            "messages_with_embeddings": 80,
            "messages_dims": 1536,
        },
        "queue": {"pending": 0, "done": 210},
        "queue_by_type": {
            "representation": {"pending": 0, "done": 81},
            "reconciler": {"pending": 0, "done": 120},
        },
        "errors": {"save_representation": 0, "four_oh_one": 0},
        "spark_goat": {"ok": True, "latency_s": 1.2, "thinking": False, "model": "aeon-ultimate"},
        "deriver": {"runs_15m": 6, "last_duration_s": 39, "conclusions": 2602},
    }

    alerts = hm.build_alerts(hm.HonchoSnapshot(**snapshot), previous_state=prev)

    assert "Representation queue advancing faster than documents" not in alerts


def test_spark_model_selection_falls_back_when_deriver_routed_elsewhere():
    # When deriver points to mac-studio, the spark-goat chat smoke should not
    # use that model; it should pick aeon-ultimate known to be on spark-goat.
    pipeline = {
        "deriver": {"model": "qwen3.5-397b", "base_url": "http://100.110.104.77:8087/v1"},
    }
    assert hm.select_spark_model(pipeline) == "aeon-ultimate"


def test_spark_model_selection_uses_deriver_when_on_spark_goat():
    pipeline = {
        "deriver": {"model": "custom-model", "base_url": "http://100.69.54.37:8001/v1"},
    }
    assert hm.select_spark_model(pipeline) == "custom-model"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "2026-09-04 15:40:00,123 - src.llm.api - ERROR - Error code: 401 - invalid_api_key",
            {"save_representation": 0, "four_oh_one": 1},
        ),
        (
            "2026-09-04 15:40:01 - src.llm.api - ERROR - Unauthorized request rejected",
            {"save_representation": 0, "four_oh_one": 1},
        ),
        (
            "2026-09-04 15:40:02 - src.llm.api - CRITICAL - request failed: 401 Unauthorized",
            {"save_representation": 0, "four_oh_one": 1},
        ),
        (
            "2026-09-04 15:40:03 - src.llm.api - ERROR - POST https://api.openai.com/v1/responses returned 401",
            {"save_representation": 0, "four_oh_one": 1},
        ),
        (
            "openai.AuthenticationError: Error code: 401 - invalid_api_key",
            {"save_representation": 0, "four_oh_one": 1},
        ),
        (
            "openai.APIStatusError: Error code: 401 - Unauthorized",
            {"save_representation": 0, "four_oh_one": 1},
        ),
        (
            "2026-09-04 15:40:04,456 - src.deriver - ERROR - Failed to save representation for observer user",
            {"save_representation": 1, "four_oh_one": 0},
        ),
        (
            "ERROR - Failed to save representation for observer user",
            {"save_representation": 1, "four_oh_one": 0},
        ),
    ],
)
def test_parse_deriver_error_counts_recognizes_each_real_error_line(line: str, expected: dict[str, int]):
    assert hm.parse_deriver_error_counts(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "2026-07-08 10:16:25,401 - src.deriver.queue_manager - DEBUG - Claimed 1 work units",
        '      INFO   172.18.0.1:40146 - "POST /v3/workspaces HTTP/1.1" 201',
        "│  Droid failed due to 401 Unauthorized errors. The review was successfully    │",
        "│  Error code: 401 quoted from an earlier incident                                │",
        "│  invalid_api_key appeared in archived message content                           │",
        "│  POST https://api.openai.com/v1/responses returned 401 in an old transcript     │",
        "│  Unauthorized request rejected                                                  │",
        "│  openai.AuthenticationError quoted from an earlier traceback                    │",
        "│  openai.APIStatusError: Error code: 401 - invalid_api_key                        │",
        "│  Failed to save representation for observer user                                │",
        "openai.APIStatusError: Error code: 500 - server_error",
        "2026-09-04 15:55:19,401 - src.llm.api - INFO - Error code: 401 quoted in tool output",
        "2026-09-04 15:55:20,401 - src.llm.api - WARNING - Failed to save representation quoted in diagnostics",
    ],
)
def test_parse_deriver_error_counts_ignores_each_benign_or_quoted_line(line: str):
    assert hm.parse_deriver_error_counts(line) == {
        "save_representation": 0,
        "four_oh_one": 0,
    }


def test_collect_recent_deriver_errors_uses_python_parser_on_raw_log_lines():
    commands: list[tuple[str, int]] = []

    def fake_ssh(command: str, timeout: int = 20) -> str:
        commands.append((command, timeout))
        return "\n".join(
            [
                "2026-09-04 15:40:00,123 - src.llm.api - ERROR - Error code: 401 - invalid_api_key",
                "│  Error code: 401 - invalid_api_key quoted in derived content  │",
                "2026-09-04 15:40:02,456 - src.deriver - ERROR - Failed to save representation for observer user",
            ]
        )

    assert hm.collect_recent_deriver_errors(ssh_fn=fake_ssh) == {
        "save_representation": 1,
        "four_oh_one": 1,
    }
    assert commands == [("docker logs honcho-deriver-1 --since 15m 2>&1", 30)]


def test_collect_snapshot_propagates_recent_deriver_error_counts(monkeypatch, tmp_path):
    def fake_ssh(command: str, timeout: int = 20) -> str:
        if "compose ps" in command:
            return "honcho-api-1 Up\nhoncho-deriver-1 Up\nhoncho-database-1 Up\nhoncho-redis-1 Up"
        if "FROM documents" in command and "message_embeddings" in command:
            return "0|0||0|0|"
        if "active_queue_sessions" in command:
            return "0|0||0|0|0|0|0|0"
        if "Observation Count" in command:
            return "count|0\nlast|"
        return ""

    expected = {"save_representation": 2, "four_oh_one": 3}
    monkeypatch.setattr(hm, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(hm, "ssh", fake_ssh)
    monkeypatch.setattr(hm, "collect_recent_deriver_errors", lambda: expected.copy())
    monkeypatch.setattr(hm, "latest_local_message_timestamp", lambda: None)
    monkeypatch.setattr(hm, "curl_post_json", lambda *args, **kwargs: (False, None, 0.0))

    snapshot, current_state = hm.collect_snapshot()

    assert snapshot.errors == expected
    assert current_state["errors"] == expected


def test_latest_local_message_timestamp_ignores_cron_sessions(tmp_path: Path):
    db_path = tmp_path / "state.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT);
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT,
            timestamp REAL,
            role TEXT,
            content TEXT
        );
        INSERT INTO sessions (id, source) VALUES
            ('telegram-session', 'telegram'),
            ('cron-session', 'cron');
        INSERT INTO messages (session_id, timestamp, role, content) VALUES
            ('telegram-session', 1000, 'assistant', 'eligible completed turn'),
            ('cron-session', 2000, 'assistant', 'newer cron output');
        """
    )
    con.close()

    assert hm.latest_local_message_timestamp(db_path) == 1000.0


def test_build_alerts_detects_hermes_to_honcho_ingestion_staleness():
    snapshot = {
        "services": {"api_ok": True, "deriver_up": True, "db_ok": True, "redis_ok": True},
        "pipeline": {
            "embedding": {
                "model": "qwen3-embedding-8b-1536",
                "base_url": "http://100.69.54.37:11435/v1",
                "dimensions_mode": "never",
                "vector_dimensions": "1536",
            },
            "deriver": {"model": "gemma12b-polar-gpustack", "base_url": "http://100.71.155.95:18081/v1"},
            "summary": {"model": "mlx-community--Qwen3.5-4B-4bit", "base_url": "http://192.168.10.104:8000/v1"},
            "dream": {"model": "aeon-ultimate", "base_url": "http://100.69.54.37:8001/v1"},
            "dialectic": {"model": "mlx-community--Qwen3.5-9B-4bit", "base_url": "http://192.168.10.104:8000/v1"},
        },
        "db": {
            "documents_total": 88520,
            "documents_with_embeddings": 88520,
            "documents_dims": 1536,
            "messages_total": 13273,
            "messages_with_embeddings": 13270,
            "messages_dims": 1536,
        },
        "queue": {"pending": 0, "done": 123},
        "queue_by_type": {"representation": {"pending": 0, "done": 8}},
        "errors": {"save_representation": 0, "four_oh_one": 0},
        "spark_goat": {"ok": True, "latency_s": 1.3, "thinking": False, "model": "aeon-ultimate"},
        "deriver": {"runs_15m": 1, "last_duration_s": 5.0, "conclusions": 0},
        "ingestion": {"source_fresh": True, "downstream_fresh": False, "drift_s": 7200, "source_age_s": 60, "downstream_age_s": 7260},
    }

    alerts = hm.build_alerts(hm.HonchoSnapshot(**snapshot))

    assert "Hermes→Honcho ingestion stale (2.0h drift)" in alerts


def test_should_emit_report_is_false_for_nominal_snapshot(monkeypatch):
    snapshot = hm.HonchoSnapshot(
        services={"api_ok": True, "deriver_up": True, "db_ok": True, "redis_ok": True},
        pipeline={
            "embedding": {
                "model": "qwen3-embedding-8b-1536",
                "base_url": "http://100.69.54.37:11435/v1",
                "dimensions_mode": "never",
                "vector_dimensions": "1536",
            },
            "deriver": {"model": "gemma12b-polar-gpustack", "base_url": "http://100.71.155.95:18081/v1"},
            "summary": {"model": "mlx-community--Qwen3.5-4B-4bit", "base_url": "http://192.168.10.104:8000/v1"},
            "dream": {"model": "aeon-ultimate", "base_url": "http://100.69.54.37:8001/v1"},
            "dialectic": {"model": "mlx-community--Qwen3.5-9B-4bit", "base_url": "http://192.168.10.104:8000/v1"},
        },
        db={
            "documents_total": 88520,
            "documents_with_embeddings": 88520,
            "documents_dims": 1536,
            "messages_total": 13273,
            "messages_with_embeddings": 13270,
            "messages_dims": 1536,
        },
        queue={"pending": 0, "done": 123},
        queue_by_type={"representation": {"pending": 0, "done": 8}, "reconciler": {"pending": 0, "done": 111}},
        errors={"save_representation": 0, "four_oh_one": 0},
        spark_goat={"ok": True, "latency_s": 1.3, "thinking": False, "model": "aeon-ultimate"},
        deriver={"runs_15m": 1, "last_duration_s": 5.0, "conclusions": 0},
    )
    previous = {
        "queue_done": 117,
        "documents_total": 88520,
        "queue_by_type": {"representation": {"pending": 0, "done": 8}, "reconciler": {"pending": 0, "done": 105}},
    }

    monkeypatch.delenv("HONCHO_MONITOR_ALWAYS_PRINT", raising=False)

    assert hm.build_alerts(snapshot, previous_state=previous) == []
    assert hm.should_emit_report(snapshot, previous_state=previous) is False


def test_should_emit_report_can_be_forced_for_manual_debug(monkeypatch):
    snapshot = hm.HonchoSnapshot(
        services={"api_ok": True, "deriver_up": True, "db_ok": True, "redis_ok": True},
        pipeline={
            "embedding": {
                "model": "qwen3-embedding-8b-1536",
                "base_url": "http://100.69.54.37:11435/v1",
                "dimensions_mode": "never",
                "vector_dimensions": "1536",
            },
            "deriver": {"model": "gemma12b-polar-gpustack", "base_url": "http://100.71.155.95:18081/v1"},
            "summary": {"model": "mlx-community--Qwen3.5-4B-4bit", "base_url": "http://192.168.10.104:8000/v1"},
            "dream": {"model": "aeon-ultimate", "base_url": "http://100.69.54.37:8001/v1"},
            "dialectic": {"model": "mlx-community--Qwen3.5-9B-4bit", "base_url": "http://192.168.10.104:8000/v1"},
        },
        db={
            "documents_total": 88520,
            "documents_with_embeddings": 88520,
            "documents_dims": 1536,
            "messages_total": 13273,
            "messages_with_embeddings": 13270,
            "messages_dims": 1536,
        },
        queue={"pending": 0, "done": 123},
        queue_by_type={"representation": {"pending": 0, "done": 8}},
        errors={"save_representation": 0, "four_oh_one": 0},
        spark_goat={"ok": True, "latency_s": 1.3, "thinking": False, "model": "aeon-ultimate"},
        deriver={"runs_15m": 1, "last_duration_s": 5.0, "conclusions": 0},
    )
    monkeypatch.setenv("HONCHO_MONITOR_ALWAYS_PRINT", "1")

    assert hm.should_emit_report(snapshot, previous_state={}) is True


def test_deriver_conclusions_uses_db_count_not_stale_log_grep(monkeypatch):
    """The conclusions metric must come from the DB (inductive+deductive
    documents), because the 'N total conclusions' log line no longer exists in
    the deployed Honcho version and the grep always yielded 0."""
    ssh_calls: list[tuple[str, int]] = []

    def fake_ssh(cmd: str, timeout: int = 20) -> str:
        ssh_calls.append((cmd, timeout))
        if "level IN ('inductive','deductive')" in cmd and "psql" in cmd:
            return "2083"
        if "Observation Count" in cmd:
            return "count|6\nlast|  4,000 ms\n"
        raise AssertionError(f"unexpected ssh command: {cmd}")

    monkeypatch.setattr(hm, "ssh", fake_ssh)

    conclusions = hm.collect_deriver_conclusions(ssh_fn=fake_ssh)

    assert conclusions == 2083
    assert any(
        "level IN ('inductive','deductive')" in cmd and "psql" in cmd
        for cmd, _ in ssh_calls
    )


def test_default_monitor_state_follows_served_profile(tmp_path, monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    monkeypatch.delenv("HONCHO_MONITOR_STATE", raising=False)
    monkeypatch.delenv("HONCHO_MONITOR_HERMES_STATE_DB", raising=False)
    for name in ("first", "second"):
        home = tmp_path / name
        token = set_hermes_home_override(home)
        try:
            assert hm._state_path() == home / "cache" / "honcho_monitor.json"
            assert hm._state_db_path() == home / "state.db"
        finally:
            reset_hermes_home_override(token)


@pytest.mark.platforms("linux")
def test_deriver_log_probe_uses_unique_scratch_and_cleans_it(tmp_path):
    import os
    import subprocess

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text("#!/bin/sh\nprintf 'Observation Count 1\\nObservation Count 2\\nLlm Call Duration 1800 ms\\n'\n", encoding="utf-8")
    docker.chmod(0o755)
    result = subprocess.run(
        ["sh", "-c", hm._deriver_log_command()],
        env={"PATH": f"{binaries}{os.pathsep}{os.defpath}", "TMPDIR": str(scratch)},
        check=True, capture_output=True, text=True, timeout=5,
    )
    assert result.stdout.splitlines() == ["count|2", "last|Llm Call Duration 1800 ms"]
    assert not list(scratch.iterdir())
