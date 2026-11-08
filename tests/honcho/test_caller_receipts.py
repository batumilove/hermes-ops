import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "plugins/honcho-ops"))
from honcho_ops.receipts import caller_receipts

spec = importlib.util.spec_from_file_location(
    "caller_probe", ROOT / "plugins/honcho-ops/honcho_ops/caller_receipt_probe.py"
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def stores(rows):
    return {"api": {"rows": rows}, "deriver": {"rows": []}}


def row(outcome="success", finished=999, started=998):
    return dict(id="a" * 32, started=started, finished=finished, outcome=outcome)


def test_pre_proxy_timeout_and_cancel_are_failures():
    value = caller_receipts(
        lambda *a, **k: json.dumps(stores([row("error"), row("cancelled")])),
        {},
        now=1000,
    )
    assert value["status"] == "failed"
    assert value["failed"] == 2
    assert value["proxy_unmatched"] == 2


def test_unmatched_success_is_not_a_provider_outage():
    value = caller_receipts(lambda *a, **k: json.dumps(stores([row()])), {}, now=1000)
    assert value["status"] == "observed"
    assert value["proxy_unmatched"] == 1


def test_request_id_joins_are_bounded_application_attempts():
    value = caller_receipts(
        lambda *a, **k: json.dumps(stores([row()])),
        {"request_ids": ["a" * 32]},
        now=1000,
    )
    assert value["proxy_matched"] == 1
    assert value["proxy_unmatched"] == 0


def test_old_unfinished_debt_still_alerts():
    value = caller_receipts(
        lambda *a, **k: json.dumps(stores([row(None, None, 1)])), {}, now=1000
    )
    assert value["status"] == "delayed"


def test_missing_store_cannot_mean_idle():
    value = caller_receipts(
        lambda *a, **k: json.dumps(
            {"api": {"unavailable": True}, "deriver": {"rows": []}}
        ),
        {},
        now=1000,
    )
    assert value["status"] == "unverified"
    assert value["unavailable_stores"] == ["api"]


def test_remote_probe_missing_schema_and_old_debt(tmp_path):
    with closing(sqlite3.connect(tmp_path / "api.sqlite3")) as c:
        c.execute(
            "CREATE TABLE requests(id, started, finished, stage, purpose, model, endpoint, outcome, error_class, http_status)"
        )
        c.execute(
            "INSERT INTO requests VALUES(?,1,NULL,'dream',NULL,'model','http://example',NULL,NULL,NULL)",
            ("a" * 32,),
        )
        c.commit()
    result = probe.collect(tmp_path, 1000)
    assert result["api"]["overdue"]
    assert len(result["api"]["rows"]) == 1
    assert result["deriver"]["unavailable"]
    assert not (tmp_path / "deriver.sqlite3").exists()


def test_truncation_keeps_debt_status():
    data = stores([])
    data["api"].update(truncated=True, overdue=True)
    assert (
        caller_receipts(lambda *a, **k: json.dumps(data), {}, now=1000)["status"]
        == "delayed"
    )
