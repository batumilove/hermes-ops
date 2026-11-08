"""Sampled service health never proves configuration or natural request routing."""

from dataclasses import replace

import pytest

from honcho_ops import monitor as hm


def _healthy_snapshot(probe):
    return hm.HonchoSnapshot(
        services={"api_ok": True, "deriver_up": True, "db_ok": True, "redis_ok": True},
        pipeline={"embedding": {
            "model": "embedding", "base_url": "https://embedding.example/v1",
            "vector_dimensions": "768",
        }},
        db={}, queue={}, queue_by_type={}, errors={}, spark_goat=probe, deriver={},
    )


@pytest.mark.parametrize("probe,label", [
    ({}, "Provider probe: UNVERIFIED (no probe record)"),
    ({"skipped": True, "routing_unknown": True}, "Provider probe: SKIPPED (not a failed request)"),
])
def test_absent_or_skipped_probe_is_advisory_not_a_service_outage(probe, label):
    snapshot = _healthy_snapshot(probe)
    raw_alerts = hm.build_alerts(snapshot)
    report = hm.format_report(snapshot)
    assert label in report
    assert "Service/processing checks: nominal (sampled checks only" in report
    assert "Inspected configuration: UNKNOWN" in report
    assert "Actual request routing: UNKNOWN / UNVERIFIED" in report
    assert "not evidence of outage" in report
    assert "spark-goat chat failed" not in report
    assert hm.build_alerts(snapshot) == raw_alerts


def test_failed_request_and_stale_remote_sample_remain_visible():
    snapshot = _healthy_snapshot({"ok": False, "latency_s": 1})
    report = hm.format_report(snapshot)
    assert "Service/processing checks: ALERT" in report
    assert "spark-goat chat failed" in report
    assert "spark-goat chat failed" in hm.build_alerts(snapshot)
    stale = replace(snapshot, services={**snapshot.services, "ssh_ok": False})
    assert "Service/processing checks: UNKNOWN (remote sample stale)" in hm.format_report(stale)


@pytest.mark.parametrize("route_status", ["failed", "delayed", "pending", "observed"])
def test_receipt_advisory_preserves_independent_configuration_and_attempt_evidence(route_status):
    snapshot = _healthy_snapshot({"skipped": True})
    snapshot = replace(snapshot, ingestion={
        "config_receipts": {"status": "reconstructed"},
        "routing_receipts": {"status": route_status},
    })
    report = hm.format_report(snapshot)
    assert "Inspected configuration: MATCHED" in report
    assert "configuration=reconstructed" in report
    assert f"proxy attempts={route_status}" in report
    assert "configuration provenance UNKNOWN" not in report
    assert "complete stage coverage" in report


def test_configuration_mismatch_is_reported_as_such():
    snapshot = replace(_healthy_snapshot({"skipped": True}), ingestion={"config_receipts":{"status":"mismatch"}})
    report = hm.format_report(snapshot)
    assert "Inspected configuration: MISMATCH" in report
    assert "Honcho API/deriver route configuration mismatch" in hm.build_alerts(snapshot)


def test_stage_report_uses_explicit_attribution_with_shared_model():
    snapshot = replace(_healthy_snapshot({"skipped": True}), ingestion={"routing_receipts": {
        "status":"observed", "successful":2, "failed":0, "pending":0,
        "stage_coverage":{"deriver":"observed","summary":"observed","dream":"not_observed"},
        "routes":[{"stage":stage,"operation":"chat","model":"shared-model","endpoint":"https://provider.example"} for stage in ("deriver","summary")],
    }})
    report = hm.format_report(snapshot)
    assert "deriver/chat: shared-model" in report
    assert "summary/chat: shared-model" in report
    assert "dream=not_observed" in report
