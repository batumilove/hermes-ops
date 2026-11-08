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
            parts = {
                r["id"]: [
                    dict(p)
                    for p in c.execute(
                        "SELECT ordinal,role,remote_id FROM parts WHERE turn_id=? ORDER BY ordinal",
                        (r["id"],),
                    )
                ]
                for r in rows
            }
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


def routing_receipts(home, *, now=None):
    now = time.time() if now is None else now
    result = {
        "status": "unverified",
        "scope": "proxy-bound HTTP attempts; requested model, not per-stage coverage",
    }
    try:
        with _read(Path(home) / "cache/honcho-receipts/routes.sqlite3") as c:
            c.row_factory = sqlite3.Row
            rows = [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM routes WHERE started>=? ORDER BY started DESC LIMIT 1001",
                    (now - 900,),
                )
            ]
    except (sqlite3.Error, OSError, ValueError):
        return {**result, "reason": "route_store_unavailable"}
    if len(rows) > 1000:
        return {**result, "reason": "route_window_truncated"}
    if not rows:
        return {**result, "status": "idle", "reason": "no_recent_proxy_attempts"}
    failures = sum(
        r["finished"] is not None
        and (bool(r["error_class"]) or not r["status"] or r["status"] >= 400)
        for r in rows
    )
    delayed = sum(r["finished"] is None and now - r["started"] > 150 for r in rows)
    pending = sum(r["finished"] is None for r in rows)
    completed = [
        r
        for r in rows
        if r["finished"] is not None
        and r["status"]
        and 200 <= r["status"] < 400
        and not r["error_class"]
    ]
    return {
        **result,
        "status": "failed"
        if failures
        else "delayed"
        if delayed
        else "observed"
        if completed
        else "pending",
        "successful": len(completed),
        "failed": failures,
        "delayed": delayed,
        "pending": pending,
        "routes": [
            dict(operation=r[0], model=r[1], endpoint=r[2])
            for r in sorted(
                {(r["operation"], r["model"], r["endpoint"]) for r in completed}
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
