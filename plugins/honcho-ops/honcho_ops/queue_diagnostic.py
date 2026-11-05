"""Hermes-side Honcho queue detail diagnostic client.

Reports per-work-unit or best-available detail from the Honcho database and/or
deriver logs. This is intentionally a low-risk, read-only diagnostic that does not
modify running Honcho containers. It is designed to run from the Hermes VM against
a remote Honcho host via SSH or from the same host for local inspection.

Data sources:
- PostgreSQL queue / sessions / messages / documents tables
- Active queue sessions (lock table)
- Deriver logs (PERFORMANCE blocks, recent errors)
- v3 API queue/status (when available)

Redacted by default: no secrets, no message content, no raw API keys.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any

DEFAULT_HONCHO_HOST = "ubuntu@100.67.206.76"
DEFAULT_WORKSPACE = "hermes"

# Safe destination for OpenSSH: user@host/IP/Tailscale-style hostname.
# user and host segments may contain alphanumerics, dots, hyphens, and underscores
# (Tailscale machine names often include dots); IPv4 literal addresses are allowed.
_SSH_HOST_RE = re.compile(
    r"^(?P<user>[A-Za-z0-9_][\w.-]*@)?(?P<host>[A-Za-z0-9][A-Za-z0-9_.-]*|\d{1,3}(\.\d{1,3}){3})$"
)
# Docker Compose service/container names: alnum, dot, underscore, hyphen only,
# must not start with a hyphen.
_CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

# Column order must match the SELECT in collect_queue_detail().
# Live schema (verified on VM306) has no updated_at column.
QUEUE_COLUMNS = [
    "id",
    "session_id",
    "message_id",
    "task_type",
    "processed",
    "error",
    "created_at",
    "work_unit_key",
]


class HonchoDiagnosticError(Exception):
    """Raised when a diagnostic query cannot be satisfied."""


@dataclass(frozen=True)
class QueueWorkUnit:
    work_unit_id: int
    work_unit_key: str | None
    task_type: str
    session_id: str | None
    message_id: int | None
    processed: bool
    error: str | None
    created_at: datetime | None

    @property
    def phase(self) -> str:
        if self.error is not None:
            return "errored"
        if self.processed:
            return "done"
        return "pending"

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_unit_id": self.work_unit_id,
            "work_unit_key": self.work_unit_key,
            "task_type": self.task_type,
            "session_id": self.session_id,
            "message_id": self.message_id,
            "processed": self.processed,
            "error": self.error,
            "phase": self.phase,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


@dataclass(frozen=True)
class WorkUnitDiagnostics:
    work_unit: QueueWorkUnit
    peer_refs: list[str]
    message_count: int | None
    document_count: int | None
    llm_duration_ms: int | None
    observation_count: int | None
    log_excerpt: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_unit": self.work_unit.to_dict(),
            "peer_refs": self.peer_refs,
            "message_count": self.message_count,
            "document_count": self.document_count,
            "llm_duration_ms": self.llm_duration_ms,
            "observation_count": self.observation_count,
            "log_excerpt": self.log_excerpt,
        }


class HonchoQueueDiagnostic:
    """Read-only diagnostic client for the Honcho queue."""

    def __init__(
        self,
        host: str | None = None,
        workspace: str | None = None,
        database_container: str = "honcho-database-1",
        deriver_container: str = "honcho-deriver-1",
    ):
        self.host = _validate_ssh_host(
            host if host is not None else os.environ.get("HONCHO_DIAGNOSTIC_HOST", DEFAULT_HONCHO_HOST)
        )
        self.workspace = workspace if workspace is not None else os.environ.get("HONCHO_DIAGNOSTIC_WORKSPACE", DEFAULT_WORKSPACE)
        self.database_container = _validate_container_name(database_container)
        self.deriver_container = _validate_container_name(deriver_container)

    def _ssh(self, command: str, timeout: int = 30) -> str:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "ConnectTimeout=5",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                self.host,
                command,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip() if result.stderr else "(no stderr)"
            raise HonchoDiagnosticError(
                f"SSH command failed on {self.host} (rc={result.returncode}): {stderr}"
            )
        return result.stdout.strip()

    def _psql(self, query: str, timeout: int = 30) -> str:
        command = (
            f"docker exec {shlex.quote(self.database_container)} "
            f"psql -U postgres -t -A -F '|' -c {shlex.quote(query)}"
        )
        output = self._ssh(command, timeout=timeout)
        if "ERROR:" in output or "FATAL:" in output:
            raise HonchoDiagnosticError(f"psql returned an error: {output[:400]}")
        return output

    def _container_log(self, since: str = "15m") -> str:
        """Return recent deriver log text, redirected via file to avoid Rich hangs."""
        try:
            return self._ssh(
                "diagnostic_log=$(mktemp -t hermes-honcho-diagnostic.XXXXXX) || exit 1; "
                "trap 'rm -f \"$diagnostic_log\"' EXIT; "
                f"docker logs {shlex.quote(self.deriver_container)} --since {shlex.quote(since)} "
                "> \"$diagnostic_log\" 2>&1; cat \"$diagnostic_log\"",
                timeout=40,
            )
        except HonchoDiagnosticError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise HonchoDiagnosticError(f"Failed to fetch deriver logs: {exc}") from exc

    def collect_queue_summary(self) -> dict[str, Any]:
        """Aggregate queue counts by status and task type."""
        raw = self._psql(
            "SELECT task_type, processed::text, count(*)::text, "
            "count(*) FILTER (WHERE error IS NOT NULL)::text "
            "FROM queue GROUP BY task_type, processed ORDER BY task_type, processed;"
        )
        summary: dict[str, Any] = {"total": 0, "pending": 0, "done": 0, "errored": 0, "by_type": {}}
        for row in raw.splitlines():
            parts = row.split("|")
            if len(parts) != 4:
                continue
            task_type, processed_str, count_str, error_count_str = parts
            count = int(count_str) if count_str.isdigit() else 0
            error_count = int(error_count_str) if error_count_str.isdigit() else 0
            processed = processed_str.lower() in ("t", "true", "1")
            summary["total"] += count
            if processed:
                summary["done"] += count
                summary["errored"] += error_count
            else:
                summary["pending"] += count
            by_type = summary["by_type"].setdefault(task_type, {"total": 0, "pending": 0, "done": 0, "errored": 0})
            by_type["total"] += count
            if processed:
                by_type["done"] += count
                by_type["errored"] += error_count
            else:
                by_type["pending"] += count
        return summary

    def collect_queue_detail(self, limit: int = 100) -> list[QueueWorkUnit]:
        """Return per-work-unit details from the queue table."""
        raw = self._psql(
            "SELECT id, session_id, message_id, task_type, processed::text, "
            "COALESCE(error, ''), created_at::text, work_unit_key "
            "FROM queue ORDER BY created_at DESC NULLS LAST, id DESC LIMIT " + str(int(limit))
        )
        units: list[QueueWorkUnit] = []
        for row in raw.splitlines():
            parts = row.split("|")
            if len(parts) < 7:
                continue
            parts = (parts + [""] * 8)[:8]
            work_unit_id = int(parts[0]) if parts[0].isdigit() else 0
            session_id = parts[1] or None
            message_id = int(parts[2]) if parts[2].isdigit() else None
            task_type = parts[3]
            processed = parts[4].lower() in ("t", "true", "1")
            error = parts[5] or None
            created_at = _parse_iso(parts[6])
            work_unit_key = parts[7] or None
            units.append(
                QueueWorkUnit(
                    work_unit_id=work_unit_id,
                    work_unit_key=work_unit_key,
                    task_type=task_type,
                    session_id=session_id,
                    message_id=message_id,
                    processed=processed,
                    error=error,
                    created_at=created_at,
                )
            )
        return units

    def collect_peer_refs(self, session_id: str) -> list[str]:
        """Return peer names referenced in a session (redacted, no content).

        queue.session_id stores sessions.id, while messages.session_name stores
        sessions.name, so we join sessions to resolve the correct peer names.
        """
        escaped = _escape_sql(session_id)
        raw = self._psql(
            "SELECT DISTINCT m.peer_name "
            "FROM messages m "
            "JOIN sessions s ON m.session_name = s.name "
            f"WHERE s.id = '{escaped}';"
        )
        return [line for line in raw.splitlines() if line]

    def collect_session_counts(self, session_id: str) -> dict[str, int]:
        """Return message and document counts for a session.

        queue.session_id stores sessions.id, while messages.session_name and
        documents.session_name store sessions.name, so we join sessions to count
        both correctly.
        """
        escaped = _escape_sql(session_id)
        raw = self._psql(
            "SELECT "
            "(SELECT count(*)::text FROM messages m "
            " JOIN sessions s ON m.session_name = s.name "
            f" WHERE s.id = '{escaped}'), "
            "(SELECT count(*)::text FROM documents d "
            " JOIN sessions s ON d.session_name = s.name "
            f" WHERE s.id = '{escaped}' AND d.deleted_at IS NULL);"
        )
        parts = raw.split("|")
        return {
            "messages": int(parts[0]) if parts[0].isdigit() else 0,
            "documents": int(parts[1]) if parts[1].isdigit() else 0,
        }

    def collect_active_sessions(self) -> list[dict[str, Any]]:
        """Return active queue session locks."""
        raw = self._psql(
            "SELECT id, work_unit_key, last_updated::text, session_id "
            "FROM active_queue_sessions ORDER BY last_updated;"
        )
        rows: list[dict[str, Any]] = []
        for row in raw.splitlines():
            parts = row.split("|")
            if len(parts) < 3:
                continue
            rows.append(
                {
                    "id": parts[0],
                    "work_unit_key": parts[1] or None,
                    "last_updated": parts[2] or None,
                    "session_id": parts[3] if len(parts) > 3 else None,
                }
            )
        return rows

    def _parse_performance_for_work_unit(self, work_unit_key: str | None, logs: str) -> dict[str, Any]:
        """Scrape the most recent PERFORMANCE block matching a work-unit key."""
        if not work_unit_key:
            return {}
        # PERFORMANCE blocks are multi-line; look for the block header containing the key.
        pattern = re.compile(
            rf"PERFORMANCE - .*{re.escape(work_unit_key)}.*?\n"
            r"(?:  .*\n)*",
            re.IGNORECASE,
        )
        match = pattern.search(logs)
        if not match:
            return {}
        block = match.group(0)
        result: dict[str, Any] = {"log_excerpt": block[:500]}
        duration_match = re.search(r"Llm Call Duration:\s*([\d,]+)\s*ms", block)
        if duration_match:
            result["llm_duration_ms"] = int(duration_match.group(1).replace(",", ""))
        count_match = re.search(r"Observation Count:\s*(\d+)", block)
        if count_match:
            result["observation_count"] = int(count_match.group(1))
        return result

    def enrich_work_unit(self, unit: QueueWorkUnit) -> WorkUnitDiagnostics:
        """Add session/message/peer and log-derived details to a work unit."""
        counts: dict[str, int] = {}
        peer_refs: list[str] = []
        if unit.session_id:
            counts = self.collect_session_counts(unit.session_id)
            peer_refs = self.collect_peer_refs(unit.session_id)
        logs = self._container_log(since="15m") if unit.created_at else ""
        perf = self._parse_performance_for_work_unit(unit.work_unit_key, logs)
        return WorkUnitDiagnostics(
            work_unit=unit,
            peer_refs=peer_refs,
            message_count=counts.get("messages"),
            document_count=counts.get("documents"),
            llm_duration_ms=perf.get("llm_duration_ms"),
            observation_count=perf.get("observation_count"),
            log_excerpt=perf.get("log_excerpt"),
        )

    def collect_enriched_queue(self, limit: int = 100) -> list[WorkUnitDiagnostics]:
        """Return enriched queue detail diagnostics."""
        units = self.collect_queue_detail(limit=limit)
        return [self.enrich_work_unit(unit) for unit in units]

    def format_report(self, enriched: list[WorkUnitDiagnostics], summary: dict[str, Any] | None = None) -> str:
        lines = [
            f"🩺 Honcho queue diagnostics — {datetime.now(UTC).strftime('%H:%M UTC')}",
            f"  host={self.host} workspace={self.workspace}",
        ]
        if summary:
            lines.append(
                f"📊 Summary: {summary['total']} total · {summary['pending']} pending · "
                f"{summary['done']} done · {summary['errored']} errored"
            )
        if not enriched:
            lines.append("No work units returned.")
            return "\n".join(lines)
        lines.append(f"📋 Showing {len(enriched)} work units (most recently created first):")
        for diag in enriched:
            unit = diag.work_unit
            age = "?"
            if unit.created_at:
                delta = datetime.now(UTC) - unit.created_at
                age = f"{delta.total_seconds() / 60:.1f}m"
            status_emoji = {"done": "✅", "pending": "⏳", "errored": "❌"}.get(unit.phase, "❓")
            lines.append(
                f"{status_emoji} {unit.work_unit_id} ({unit.task_type}) phase={unit.phase} age={age}"
            )
            if unit.work_unit_key:
                lines.append(f"   key={unit.work_unit_key}")
            if unit.session_id:
                lines.append(f"   session={unit.session_id} peers={len(diag.peer_refs)} "
                             f"messages={diag.message_count} docs={diag.document_count}")
            if unit.message_id:
                lines.append(f"   message_id={unit.message_id}")
            if diag.llm_duration_ms is not None:
                lines.append(f"   llm_duration={diag.llm_duration_ms}ms observations={diag.observation_count}")
            if unit.error:
                lines.append(f"   error={unit.error[:200]}")
        return "\n".join(lines)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # PostgreSQL text timestamps often look like "2026-05-27 19:40:00+00".
        return datetime.fromisoformat(value.replace(" ", "T").replace("+00", "+00:00"))
    except (TypeError, ValueError):
        return None


def _escape_sql(value: str | None) -> str:
    if value is None:
        return ""
    return value.replace("'", "''")


def _validate_ssh_host(host: str) -> str:
    """Return host if it is a safe ssh destination; raise otherwise.

    OpenSSH parses the first positional argument after options as either a
    destination or, if it begins with '-', an additional option. Allowlisting
    the destination shape prevents config-derived argv injection.
    """
    if not host or "|" in host or "\n" in host or "\x00" in host:
        raise HonchoDiagnosticError(f"Invalid SSH host: {host!r}")
    if not _SSH_HOST_RE.fullmatch(host):
        raise HonchoDiagnosticError(f"Invalid SSH host: {host!r}")
    return host


def _validate_container_name(name: str) -> str:
    """Return name if it looks like a Docker container/Compose service name."""
    if not name or not _CONTAINER_NAME_RE.fullmatch(name):
        raise HonchoDiagnosticError(f"Invalid container name: {name!r}")
    return name


def main() -> int:
    diagnostic = HonchoQueueDiagnostic()
    summary = diagnostic.collect_queue_summary()
    enriched = diagnostic.collect_enriched_queue(limit=50)
    print(diagnostic.format_report(enriched, summary=summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
