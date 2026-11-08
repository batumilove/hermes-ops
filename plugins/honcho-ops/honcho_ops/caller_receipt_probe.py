"""Run on Honcho host: bounded read-only caller metadata, never conversation data."""

import json
import sqlite3
import time
from pathlib import Path


def collect(root=Path("/var/lib/honcho-receipts"), now=None):
    now = time.time() if now is None else now
    stores = {}
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
                    "SELECT id,started,finished,stage,purpose,model,endpoint,outcome,error_class,http_status FROM requests WHERE started>=? OR finished IS NULL ORDER BY started DESC LIMIT 1001",
                    (now - 900,),
                )
            ]
            overdue = bool(
                c.execute(
                    "SELECT EXISTS(SELECT 1 FROM requests WHERE finished IS NULL AND started<?)",
                    (now - 150,),
                ).fetchone()[0]
            )
            stores[name] = {
                "rows": rows[:1000],
                "truncated": len(rows) > 1000,
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
