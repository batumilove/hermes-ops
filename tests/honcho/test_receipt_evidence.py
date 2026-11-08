import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "plugins/honcho-ops"))
from honcho_ops.receipts import ingestion_receipts, routing_receipts  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "proxy", ROOT / "scripts/honcho/nous_auth_proxy.py"
)
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.path = self.home / "cache/honcho-receipts/receipts.sqlite3"
        self.path.parent.mkdir(parents=True)
        with sqlite3.connect(self.path) as c:
            c.executescript(
                "CREATE TABLE turns(id,source_session,workspace,session_key,remote_session,created,updated,human,expected,status,error_class); CREATE TABLE parts(turn_id,ordinal,role,remote_id);"
            )

    def seed(self, status="acknowledged", created=999, session="session"):
        with sqlite3.connect(self.path) as c:
            c.execute(
                "INSERT INTO turns VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "turn",
                    "local",
                    "workspace",
                    "key",
                    session,
                    created,
                    created,
                    1,
                    1,
                    status,
                    None,
                ),
            )
            c.execute(
                "INSERT INTO parts VALUES(?,?,?,?)", ("turn", 0, "user", "remote")
            )

    def test_exact_remote_identity_confirms(self):
        self.seed()
        seen = []

        def remote(cmd, **kw):
            seen.append(cmd)
            return json.dumps([["remote", "workspace", "session", "turn", "0"]])

        r = ingestion_receipts(self.home, remote, now=1000)
        self.assertEqual((r["status"], r["verified"]), ("verified", 1))
        self.assertIn("base64 -d", seen[0])

    def test_remote_wrong_session_is_failure(self):
        self.seed()
        r = ingestion_receipts(
            self.home,
            lambda *a, **k: json.dumps([["remote", "workspace", "wrong", "turn", "0"]]),
            now=1000,
        )
        self.assertEqual(r["status"], "failed")

    def test_transport_failure_stays_unverified(self):
        self.seed()
        r = ingestion_receipts(self.home, lambda *a, **k: "__SSH_ERROR__", now=1000)
        self.assertEqual(r["status"], "unverified")

    def test_aged_pending_is_delayed_without_remote_query(self):
        self.seed(status="pending", created=1)
        r = ingestion_receipts(
            self.home, lambda *a, **k: self.fail("no query expected"), now=1000
        )
        self.assertEqual(r["status"], "delayed")

    def test_ambiguous_write_stays_unknown(self):
        self.seed(status="unknown")
        self.assertEqual(
            ingestion_receipts(self.home, None, now=1000)["status"], "unknown"
        )

    def test_empty_is_idle_not_loss_free(self):
        r = ingestion_receipts(self.home, None, now=1000)
        self.assertEqual(r["status"], "idle")
        self.assertIn("boundary", r["scope"])

    def test_missing_store_unverified(self):
        self.path.unlink()
        self.assertEqual(ingestion_receipts(self.home, None)["status"], "unverified")

    def test_route_success_failure_and_no_request_body(self):
        with patch.dict("os.environ", {"HERMES_HOME": str(self.home)}):
            i = proxy.route_begin(
                "client", "/v1/embeddings?secret=never", "embedding-model"
            )
            proxy.route_finish(i, 200)
            r = routing_receipts(self.home)
            self.assertEqual(r["status"], "observed")
            i = proxy.route_begin("client", "/v1/chat/completions", "model")
            proxy.route_finish(i, 502, "TimeoutError")
            self.assertEqual(routing_receipts(self.home)["status"], "failed")
        with sqlite3.connect(self.path.parent / "routes.sqlite3") as c:
            rows = repr(c.execute("SELECT * FROM routes").fetchall())
            self.assertNotIn("secret", rows)

    def test_handler_failure_is_recorded_without_exception_text(self):
        h = object.__new__(proxy.H)
        h.path = "/v1/chat/completions"
        h.headers = {"Content-Length": "37"}
        h.rfile = io.BytesIO(b'{"model":"m","messages":["private"]}')
        h.client_address = ("client", 1)
        replies = []
        h._reply = lambda code, body, *args: replies.append((code, body))
        with (
            patch.dict("os.environ", {"HERMES_HOME": str(self.home)}),
            patch.object(proxy, "live_token", return_value="fake"),
            patch.object(
                proxy.urllib.request,
                "urlopen",
                side_effect=TimeoutError("private token"),
            ),
        ):
            h.do_POST()
        self.assertEqual(replies[-1][0], 502)
        self.assertNotIn("private", str(replies))
        self.assertEqual(routing_receipts(self.home)["status"], "failed")


class ConfigReceiptTests(unittest.TestCase):
    def test_mismatched_container_routes_are_not_healthy(self):
        from honcho_ops.receipts import config_receipts

        values = iter(
            [
                {"status": "reconstructed", "routes": {"model": "a"}},
                {"status": "reconstructed", "routes": {"model": "b"}},
            ]
        )
        self.assertEqual(
            config_receipts(lambda *a, **k: json.dumps(next(values)))["status"],
            "mismatch",
        )

    def test_partial_parser_evidence_stays_unknown(self):
        from honcho_ops.receipts import config_receipts

        self.assertEqual(
            config_receipts(lambda *a, **k: "__SSH_ERROR__")["status"], "unknown"
        )


if __name__ == "__main__":
    unittest.main()
