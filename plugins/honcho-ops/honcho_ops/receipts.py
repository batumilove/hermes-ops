"""Bounded read-only verification of content-free writer/proxy receipts."""

from __future__ import annotations

from contextlib import contextmanager
import base64
import json
import sqlite3
import time
from pathlib import Path


@contextmanager
def _read(path):
    c = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        yield c
    finally:
        c.close()


def ingestion_receipts(home, ssh_fn, *, now=None, limit=120):
    now = time.time() if now is None else now
    result = {
        "status": "unverified",
        "scope": "receipt-emitting completed-turn writer boundary",
        "verified": 0,
    }
    try:
        with _read(Path(home) / "cache/honcho-receipts/receipts.sqlite3") as c:
            c.row_factory = sqlite3.Row
            rows = [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM turns WHERE human=1 AND (created>=? OR status!='acknowledged') ORDER BY created DESC LIMIT ?",
                    (now - 3600, limit + 1),
                )
            ]
            if len(rows) > limit:
                return {**result, "reason": "receipt_window_truncated"}
            parts = {}
            remaining = 2400
            for row in rows:
                ps = [
                    dict(p)
                    for p in c.execute(
                        "SELECT ordinal,role,remote_id FROM parts WHERE turn_id=? ORDER BY ordinal LIMIT ?",
                        (row["id"], remaining + 1),
                    )
                ]
                if len(ps) > remaining:
                    return {**result, "reason": "receipt_part_window_truncated"}
                parts[row["id"]] = ps
                remaining -= len(ps)
    except (sqlite3.Error, OSError, ValueError):
        return {**result, "reason": "receipt_store_unavailable"}
    if not rows:
        return {**result, "status": "idle", "reason": "no_recent_admitted_turns"}
    if any(not r["source_session"] or not r["session_key"] for r in rows):
        return {**result, "reason": "source_identity_unavailable"}
    if sum(len(ps) for ps in parts.values()) > 2400:
        return {**result, "reason": "receipt_part_window_truncated"}
    uncertain = sum(r["status"] == "unknown" for r in rows)
    pending = sum(r["status"] == "pending" for r in rows)
    overdue = sum(r["status"] == "pending" and now - r["created"] > 300 for r in rows)
    ack = [r for r in rows if r["status"] == "acknowledged"]
    result.update(
        pending=pending, overdue=overdue, uncertain=uncertain, admitted=len(rows)
    )
    expected = []
    for row in ack:
        ps = parts[row["id"]]
        if len(ps) != row["expected"] or not ps or any(not p["remote_id"] for p in ps):
            return {**result, "reason": "receipt_part_incomplete"}
        expected += [
            (
                p["remote_id"],
                row["workspace"],
                row["remote_session"],
                row["id"],
                str(p["ordinal"]),
            )
            for p in ps
        ]
    if expected:
        # SQL literals are quoted before base64 transport; no receipt identifiers
        # are interpolated into shell syntax. Query emits identities only.
        def quote(s):
            return "'" + str(s).replace("'", "''") + "'"

        query = (
            "SELECT COALESCE(json_agg(json_build_array(public_id,workspace_name,session_name,metadata->'hermes_receipt'->>'id',metadata->'hermes_receipt'->>'part')), '[]'::json) FROM messages WHERE public_id IN ("
            + ",".join(quote(e[0]) for e in expected)
            + ");"
        )
        encoded = base64.b64encode(query.encode()).decode()
        command = (
            "printf %s "
            + encoded
            + " | base64 -d | docker exec -i honcho-database-1 psql -U postgres -At -v ON_ERROR_STOP=1"
        )
        try:
            raw = ssh_fn(command, timeout=30)
            remote = json.loads(raw)
            if not isinstance(remote, list):
                raise ValueError()
            matched = {tuple(r) for r in remote if isinstance(r, list) and len(r) == 5}
        except (TypeError, ValueError):
            return {**result, "reason": "remote_receipt_query_unavailable"}
        missing = sum(e not in matched for e in expected)
        result["missing_remote_parts"] = missing
        if missing:
            return {
                **result,
                "status": "failed",
                "reason": "remote_receipt_identity_mismatch",
            }
        result["verified"] = len(ack)
    status = (
        "unknown"
        if uncertain
        else "delayed"
        if overdue
        else "pending"
        if pending
        else "verified"
    )
    return {**result, "status": status, "reason": "scoped_receipts_only"}


def routing_receipts(home, *, now=None, path=None, caller=None):
    now = time.time() if now is None else now
    result = {
        "status": "unverified",
        "scope": "proxy-bound attempts; stage from validated caller headers; requested model",
    }
    try:
        store = (
            Path(path) if path else Path(home) / "cache/honcho-receipts/routes.sqlite3"
        )
        with _read(store) as c:
            c.row_factory = sqlite3.Row
            correlated = (
                c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='correlations'"
                ).fetchone()
                is not None
            )
            relation = (
                "routes AS r LEFT JOIN correlations AS c ON c.route_id=r.id"
                if correlated
                else "routes AS r"
            )
            fields = (
                "r.*,COALESCE(c.stage,'unattributed') AS stage,c.request_id"
                if correlated
                else "r.*,'unattributed' AS stage,NULL AS request_id"
            )
            condition = " AND r.caller=?" if caller else ""
            params = (now - 900, caller) if caller else (now - 900,)
            rows = [
                dict(r)
                for r in c.execute(
                    "SELECT "
                    + fields
                    + " FROM "
                    + relation
                    + " WHERE (r.started>=? OR r.finished IS NULL)"
                    + condition
                    + " ORDER BY r.started DESC LIMIT 1001",
                    params,
                )
            ]
            old_params = (now - 150, caller) if caller else (now - 150,)
            overdue = c.execute(
                "SELECT EXISTS(SELECT 1 FROM routes AS r WHERE r.finished IS NULL AND r.started<?"
                + condition
                + ")",
                old_params,
            ).fetchone()[0]
    except (sqlite3.Error, OSError, ValueError):
        return {**result, "reason": "route_store_unavailable"}
    if len(rows) > 1000:
        return {
            **result,
            "status": "delayed" if overdue else "unverified",
            "reason": "route_window_truncated",
        }
    if not rows:
        return {**result, "status": "idle", "reason": "no_recent_proxy_attempts"}

    def failed(r):
        return r["finished"] is not None and (
            bool(r["error_class"]) or not r["status"] or r["status"] >= 400
        )

    def delayed(r):
        return r["finished"] is None and now - r["started"] > 150

    completed = [
        r
        for r in rows
        if r["finished"] is not None
        and r["status"]
        and 200 <= r["status"] < 400
        and not r["error_class"]
    ]
    completed_ids = {r["id"] for r in completed}
    coverage = {}
    for stage in ("embedding", "deriver", "dialectic", "summary", "dream"):
        selected = [r for r in rows if r["stage"] == stage]
        coverage[stage] = (
            "failed"
            if any(failed(r) for r in selected)
            else "delayed"
            if any(delayed(r) for r in selected)
            else "observed"
            if any(r["id"] in completed_ids for r in selected)
            else "pending"
            if selected
            else "not_observed"
        )
    failures = sum(failed(r) for r in rows)
    late = sum(delayed(r) for r in rows)
    return {
        **result,
        "status": "failed"
        if failures
        else "delayed"
        if late
        else "observed"
        if completed
        else "pending",
        "request_ids": sorted({r["request_id"] for r in rows if r["request_id"]}),
        "successful": len(completed),
        "failed": failures,
        "delayed": late,
        "pending": sum(r["finished"] is None for r in rows),
        "unattributed": sum(r["stage"] == "unattributed" for r in rows),
        "stage_coverage": coverage,
        "routes": [
            dict(operation=r[0], model=r[1], endpoint=r[2], stage=r[3])
            for r in sorted(
                {
                    (r["operation"], r["model"], r["endpoint"], r["stage"])
                    for r in completed
                }
            )
        ],
    }


def config_receipts(ssh_fn):
    """Compare isolated parser reconstructions for both actual images."""
    import shlex

    code = Path(__file__).with_name("container_config_probe.py").read_text()
    receipts = []
    for name in ("honcho-api-1", "honcho-deriver-1"):
        command = (
            "docker exec " + name + " /app/.venv/bin/python -c " + shlex.quote(code)
        )
        try:
            value = json.loads(ssh_fn(command, timeout=25))
            if value.get("status") != "reconstructed":
                return {"status": "unknown", "reason": "parser_probe_unavailable"}
            receipts.append(value)
        except (TypeError, ValueError):
            return {"status": "unknown", "reason": "parser_probe_unavailable"}
    if receipts[0]["routes"] != receipts[1]["routes"]:
        return {"status": "mismatch", "reason": "container_route_mismatch"}
    return {
        "status": "reconstructed",
        "scope": "deployed parser; selected environment fixture; file sources absent",
        "containers": receipts,
    }


def caller_receipts(ssh_fn, proxy, *, now=None):
    """Caller errors remain visible even when the proxy received no request."""
    import shlex

    now = time.time() if now is None else now
    result = {
        "status": "unverified",
        "scope": "instrumented OpenAI caller boundary; sampled proxy correlation",
    }
    try:
        code = (
            "import os; os.environ.clear()\n"
            + Path(__file__).with_name("caller_receipt_probe.py").read_text()
        )
        stores = json.loads(
            ssh_fn(
                "docker exec honcho-api-1 /app/.venv/bin/python -I -B -c "
                + shlex.quote(code),
                timeout=15,
            )
        )
        if not isinstance(stores, dict) or set(stores) != {"api", "deriver"}:
            raise ValueError()
        rows = []
        unavailable = []
        truncated = False
        overdue = False
        for name, store in stores.items():
            if store.get("unavailable"):
                unavailable.append(name)
                continue
            items = store["rows"]
            if not isinstance(items, list) or len(items) > 1000:
                raise ValueError()
            rows.extend(items)
            truncated |= bool(store.get("truncated"))
            overdue |= bool(store.get("overdue"))
        failures = sum(r["outcome"] in {"error", "cancelled"} for r in rows)
        pending = sum(r["finished"] is None for r in rows)
        late = sum(r["finished"] is None and now - r["started"] > 150 for r in rows)
        success = sum(r["outcome"] == "success" for r in rows)
        ids = set(proxy.get("request_ids", []))
        # A successful caller may bypass this proxy; lack of a join is evidence
        # uncertainty, not an invented provider failure. SDK retries may join many.
        matched = sum(r["id"] in ids for r in rows)
        status = (
            "failed"
            if failures
            else "delayed"
            if late or overdue
            else "unverified"
            if unavailable or truncated
            else "pending"
            if pending
            else "observed"
            if rows
            else "idle"
        )
        return {
            **result,
            "status": status,
            "failed": failures,
            "successful": success,
            "pending": pending,
            "delayed": late,
            "proxy_matched": matched,
            "proxy_unmatched": len(rows) - matched,
            "unavailable_stores": unavailable,
            "truncated": truncated,
        }
    except (TypeError, ValueError, KeyError, OSError):
        return {**result, "reason": "caller_probe_unavailable"}
