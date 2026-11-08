import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
import unittest
from types import SimpleNamespace
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
        block = patch(
            "socket.socket.connect",
            side_effect=AssertionError("provider network forbidden"),
        )
        block.start()
        self.addCleanup(block.stop)
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

    def test_final_upstream_origin_is_recorded_without_credentials_or_query(self):
        with patch.dict("os.environ", {"HERMES_HOME": str(self.home)}):
            i = proxy.route_begin("client", "/v1/chat/completions", "model")
            proxy.route_finish(
                i,
                200,
                actual_url="https://user:secret@final.example:8443/path?token=hidden",
            )
        r = routing_receipts(self.home)
        self.assertEqual(r["routes"][0]["endpoint"], "https://final.example:8443")

    def test_handler_failure_is_recorded_without_exception_text(self):
        h = object.__new__(proxy.H)
        h.path = "/v1/chat/completions"
        body = b'{"model":"m","messages":["private"]}'
        h.headers = {"Content-Length": str(len(body))}
        h.rfile = io.BytesIO(body)
        h.connection = SimpleNamespace(settimeout=lambda seconds: None)
        h.client_address = ("127.0.0.1", 1)
        replies = []
        h._reply = lambda code, body, *args: replies.append((code, body))
        with (
            patch.dict("os.environ", {"HERMES_HOME": str(self.home)}),
            patch.object(proxy, "live_token", return_value="fake"),
            patch.object(
                proxy,
                "open_upstream",
                side_effect=TimeoutError("private token"),
            ),
        ):
            h.do_POST()
        self.assertEqual(replies[-1][0], 502)
        self.assertNotIn("private", str(replies))
        self.assertEqual(routing_receipts(self.home)["status"], "failed")

    def test_unfinished_request_does_not_disappear_after_recent_window(self):
        with patch.dict("os.environ", {"HERMES_HOME": str(self.home)}):
            old = proxy.route_begin("client", "/v1/chat/completions", "model")
            new = proxy.route_begin("client", "/v1/embeddings", "embedding")
            proxy.route_finish(new, 200)
        with sqlite3.connect(self.path.parent / "routes.sqlite3") as c:
            c.execute(
                "UPDATE routes SET started=? WHERE id=?", (time.time() - 4000, old)
            )
            indexes = {r[1] for r in c.execute("PRAGMA index_list(routes)")}
        self.assertTrue({"routes_finished", "routes_started"} <= indexes)
        result = routing_receipts(self.home)
        self.assertEqual((result["status"], result["delayed"]), ("delayed", 1))

    def test_explicit_proxy_path_is_independent_of_monitor_profile(self):
        with patch.dict("os.environ", {"HERMES_HOME": str(self.home)}):
            row = proxy.route_begin("intended", "/v1/embeddings", "embedding")
            proxy.route_finish(row, 200)
        other = self.home / "other-profile"
        self.assertEqual(routing_receipts(other)["status"], "unverified")
        store = self.path.parent / "routes.sqlite3"
        self.assertEqual(
            routing_receipts(other, path=store, caller="intended")["status"], "observed"
        )
        self.assertEqual(
            routing_receipts(other, path=store, caller="foreign")["status"], "idle"
        )

    def test_many_parts_are_bounded_before_remote_query(self):
        self.seed()
        with sqlite3.connect(self.path) as c:
            c.executemany(
                "INSERT INTO parts VALUES(?,?,?,?)",
                [("turn", i, "user", str(i)) for i in range(1, 3000)],
            )
        result = ingestion_receipts(
            self.home, lambda *a, **k: self.fail("no oversized remote query"), now=1000
        )
        self.assertEqual(result["reason"], "receipt_part_window_truncated")

    def handler(self, body=b"{}", address="127.0.0.1"):
        h = object.__new__(proxy.H)
        h.path = "/v1/chat/completions"
        h.headers = {"Content-Length": str(len(body))}
        h.rfile = io.BytesIO(body)
        h.connection = SimpleNamespace(settimeout=lambda seconds: None)
        h.client_address = (address, 1)
        h.replies = []
        h._reply = lambda code, data, *args: h.replies.append((code, data))
        return h

    def test_unauthorized_caller_never_loads_token_or_uses_upstream(self):
        h = self.handler(address="198.51.100.2")
        with (
            patch.object(
                proxy, "live_token", side_effect=AssertionError("token read forbidden")
            ),
            patch.object(
                proxy, "open_upstream", side_effect=AssertionError("upstream forbidden")
            ),
        ):
            h.do_POST()
            h.do_GET()
        self.assertTrue(all(r[0] == 403 for r in h.replies))
        with patch.dict("os.environ", {"NOUS_PROXY_ALLOWLIST": "192.0.2.2/32"}):
            self.assertTrue(proxy.allowed_caller("192.0.2.2"))
            self.assertFalse(proxy.allowed_caller("192.0.2.3"))

    def test_invalid_or_excessive_body_lengths_are_rejected(self):
        for length in ("-1", str(proxy.MAX_REQUEST_BYTES + 1), "invalid"):
            h = self.handler()
            h.headers["Content-Length"] = length
            with patch.object(
                proxy, "live_token", side_effect=AssertionError("token forbidden")
            ):
                h.do_POST()
            self.assertIn(h.replies[0][0], (400, 413))
            self.assertTrue(h.close_connection)

    def test_incomplete_body_and_absolute_deadline_are_bounded(self):
        h = self.handler(b"x")
        h.headers["Content-Length"] = "2"
        h.do_POST()
        self.assertEqual(h.replies[0][0], 400)
        h = self.handler(b"x")
        with patch.object(proxy.time, "monotonic", side_effect=[0, 0, 21]):
            h.do_POST()
        self.assertEqual(h.replies[0][0], 408)

    def test_connection_reuse_cannot_advertise_previous_receipt(self):
        h = self.handler(b'{"model":"model"}')
        h.route_receipt = "previous-request"
        with (
            patch.object(proxy, "route_begin", side_effect=OSError()),
            patch.object(proxy, "live_token", return_value="fake"),
            patch.object(proxy, "open_upstream", side_effect=TimeoutError()),
        ):
            h.do_POST()
        self.assertIsNone(h.route_receipt)

    def test_redirect_policy_keeps_bearer_on_trusted_origin(self):
        policy = proxy.TrustedRedirect()
        request = urllib.request.Request(
            proxy.UPSTREAM + "/v1/models", headers={"Authorization": "Bearer FAKE"}
        )
        with self.assertRaises(urllib.error.URLError):
            policy.redirect_request(
                request, None, 302, "", {}, "https://foreign.invalid/receive"
            )
        same = policy.redirect_request(
            request, None, 302, "", {}, proxy.UPSTREAM + "/v1/models-next"
        )
        self.assertEqual(same.get_header("Authorization"), "Bearer FAKE")

    def test_streaming_flushes_before_reading_next_upstream_chunk(self):
        events = []
        chunks = iter([b"data: first\n\n", b"data: [DONE]\n\n", b""])

        class Response:
            headers = {"Content-Type": "text/event-stream"}

            def read1(self, size):
                events.append("read")
                return next(chunks)

            def read(self, *args):
                raise AssertionError("must not buffer streaming response")

        h = self.handler()
        h.route_receipt = "receipt"
        headers = []
        h.send_response = lambda code: None
        h.send_header = lambda k, v: headers.append((k, v))
        h.end_headers = lambda: None
        h.wfile = SimpleNamespace(
            write=lambda data: events.append(data), flush=lambda: events.append("flush")
        )
        h._forward(Response(), 200, stream=True)
        self.assertEqual(events[:4], ["read", b"data: first\n\n", "flush", "read"])
        self.assertIn(("Content-Type", "text/event-stream"), headers)
        self.assertTrue(h.close_connection)

    def test_nonstreaming_and_http_error_bodies_preserve_content_type(self):
        h = self.handler()
        replies = []
        h._reply = lambda code, body, ctype: replies.append((code, body, ctype))
        error = urllib.error.HTTPError(
            proxy.UPSTREAM,
            400,
            "Bad Request",
            {"Content-Type": "application/problem+json"},
            io.BytesIO(b'{"error":"invalid"}'),
        )
        h._forward(error, 400)
        self.assertEqual(
            replies, [(400, b'{"error":"invalid"}', "application/problem+json")]
        )

    def test_exact_pipeline_stage_is_not_guessed_from_shared_model(self):
        expected = dict(proxy.PURPOSE_STAGES)
        expected["embedding"] = "embedding"
        with patch.dict("os.environ", {"HERMES_HOME": str(self.home)}):
            for i, (purpose, stage) in enumerate(expected.items()):
                headers = {
                    "X-Honcho-Request-ID": f"{i:032x}",
                    "X-Honcho-Stage": stage,
                    "Authorization": "FAKE-NOT-STORED",
                }
                if stage != "embedding":
                    headers["X-Honcho-Call-Purpose"] = purpose
                path = (
                    "/v1/embeddings" if stage == "embedding" else "/v1/chat/completions"
                )
                row = proxy.route_begin("caller", path, "same-model", headers=headers)
                proxy.route_finish(row, 200)
        result = routing_receipts(self.home)
        self.assertEqual(set(result["stage_coverage"].values()), {"observed"})
        self.assertEqual(result["unattributed"], 0)
        self.assertEqual(
            {r["stage"] for r in result["routes"]},
            {"deriver", "dialectic", "embedding", "summary", "dream"},
        )
        with sqlite3.connect(self.path.parent / "routes.sqlite3") as c:
            self.assertNotIn(
                "FAKE-NOT-STORED",
                repr(c.execute("SELECT * FROM correlations").fetchall()),
            )

    def test_sdk_retry_correlation_does_not_overwrite_http_attempt_identity(self):
        headers = {
            "X-Honcho-Request-ID": "a" * 32,
            "X-Honcho-Stage": "deriver",
            "X-Honcho-Call-Purpose": "deriver.representation",
        }
        with patch.dict("os.environ", {"HERMES_HOME": str(self.home)}):
            first = proxy.route_begin(
                "caller", "/v1/chat/completions", "model", headers
            )
            second = proxy.route_begin(
                "caller", "/v1/chat/completions", "model", headers
            )
            proxy.route_finish(first, 429, "HTTPError")
            proxy.route_finish(second, 200)
        self.assertNotEqual(first, second)
        with sqlite3.connect(self.path.parent / "routes.sqlite3") as c:
            self.assertEqual(
                c.execute(
                    "SELECT count(*),count(DISTINCT request_id) FROM correlations"
                ).fetchone(),
                (2, 1),
            )

    def test_bad_or_inconsistent_stage_headers_remain_unattributed(self):
        for headers in (
            {"X-Honcho-Request-ID": "SECRET", "X-Honcho-Stage": "deriver"},
            {
                "X-Honcho-Request-ID": "a" * 32,
                "X-Honcho-Stage": "deriver",
                "X-Honcho-Call-Purpose": "summary.short",
            },
            {"X-Honcho-Request-ID": "a" * 32, "X-Honcho-Stage": "embedding"},
        ):
            self.assertEqual(
                proxy.correlation_fields(headers, "chat")[1], "unattributed"
            )

    def test_legacy_receipts_never_acquire_inferred_stage_labels(self):
        with patch.dict("os.environ", {"HERMES_HOME": str(self.home)}):
            row = proxy.route_begin("caller", "/v1/chat/completions", "glm-model")
            proxy.route_finish(row, 200)
        with sqlite3.connect(self.path.parent / "routes.sqlite3") as c:
            c.execute("DROP TABLE correlations")
        result = routing_receipts(self.home)
        self.assertEqual(result["unattributed"], 1)
        self.assertEqual(set(result["stage_coverage"].values()), {"not_observed"})

    def test_bounded_retention_removes_only_corresponding_completed_children(self):
        with patch.dict("os.environ", {"HERMES_HOME": str(self.home)}):
            with proxy.receipt_db() as c:
                old = time.time() - 8 * 86400
                c.executemany(
                    "INSERT INTO routes VALUES(?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            f"old{i}",
                            old,
                            old,
                            "caller",
                            "chat",
                            "model",
                            "endpoint",
                            200,
                            None,
                        )
                        for i in range(501)
                    ],
                )
                c.executemany(
                    "INSERT INTO correlations VALUES(?,?,?,?)",
                    [(f"old{i}", None, "unattributed", None) for i in range(501)],
                )
            proxy.route_begin("caller", "/v1/chat/completions", "model")
        with sqlite3.connect(self.path.parent / "routes.sqlite3") as c:
            self.assertEqual(c.execute("SELECT count(*) FROM routes").fetchone()[0], 2)
            self.assertEqual(
                c.execute("SELECT count(*) FROM correlations").fetchone()[0], 2
            )


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
