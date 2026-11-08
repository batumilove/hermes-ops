"""Run on Honcho host: bounded read-only caller metadata, never conversation data."""

import json
import sqlite3
import time
from pathlib import Path


def collect(root=Path("/var/lib/honcho-receipts"), now=None, lookup_ids=()):
    now = time.time() if now is None else now
    stores = {}
    budget = 100
    for name in ("api", "deriver"):
        c = None
        try:
            c = sqlite3.connect(
                (root / (name + ".sqlite3")).resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=1,
            )
            c.row_factory = sqlite3.Row
            rows = [
                dict(r)
                for r in c.execute(
                    "SELECT id,started,finished,stage,purpose,model,endpoint,outcome,error_class,http_status FROM requests WHERE started>=? OR finished IS NULL ORDER BY started DESC LIMIT 101",
                    (now - 900,),
                )
            ]
            overdue = bool(
                c.execute(
                    "SELECT EXISTS(SELECT 1 FROM requests WHERE finished IS NULL AND started<?)",
                    (now - 150,),
                ).fetchone()[0]
            )
            # Exact ID lookup is independent of the recent activity window.
            # Never scan or export unbounded caller history.
            ids = tuple(lookup_ids)
            if len(ids) > 100 or any(not isinstance(i, str) or len(i) > 128 for i in ids):
                raise ValueError()
            ids = tuple(dict.fromkeys(ids))
            collisions = []
            for rid in ids:
                matches = [dict(r) for r in c.execute(
                    "SELECT id,started,finished,stage,purpose,model,endpoint,outcome,error_class,http_status FROM requests WHERE id=? LIMIT 2", (rid,))]
                if len(matches) > 1:
                    collisions.append(rid)
                # Replace sampled rows, never confuse a sample with uniqueness.
                rows = [r for r in rows if r['id'] != rid] + matches
            truncated = len(rows) > budget
            exported = rows[:budget]
            budget -= len(exported)
            stores[name] = {
                "rows": exported,
                "collisions": collisions,
                "truncated": truncated,
                "overdue": overdue,
            }
        except (sqlite3.Error, OSError, ValueError):
            stores[name] = {"unavailable": True}
        finally:
            if c is not None:
                c.close()
    return stores


if __name__ == "__main__":
    print(json.dumps(collect(), separators=(",", ":")))
