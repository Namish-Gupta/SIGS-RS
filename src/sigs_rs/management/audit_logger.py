"""
Audit Logger — dual-output security event recorder.

SRS REQ-SE-4: immutable, time-stamped audit log recording:
  - Unauthorized API access attempts
  - Dropped packet events (Inspector)
  - Configuration changes (Security Engineers)

Writes to:
  1. Database (audit.security_log) — for API querying
  2. Structured stdout (JSON lines) — for container log aggregation

Immutability: the DB user has INSERT-only grants on audit.security_log.
              Application code never issues UPDATE or DELETE on this table.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sigs_rs.persistence.repository import TelemetryRepository

logger = logging.getLogger(__name__)


class AuditLogger:
    """
    Records security events to both the database and structured stdout.
    Thread-safe: can be called from any asyncio task.
    """

    def __init__(self, repository: "TelemetryRepository | None" = None) -> None:
        self._repo = repository

    async def log(
        self,
        event_type: str,
        severity: str = "INFO",
        sensor_id: str | None = None,
        user_id: str | None = None,
        role: str | None = None,
        detail: dict[str, Any] | None = None,
        ip_address: str | None = None,
    ) -> None:
        """
        Record a security audit event.

        Args:
            event_type: One of the AuditEvent constants.
            severity:   INFO | WARN | ERROR | CRITICAL
            sensor_id:  Originating sensor (for packet-drop events).
            user_id:    Enterprise identity (for config-change events, BR-4).
            detail:     Arbitrary JSON-serialisable context dict.
            ip_address: Source IP (for auth-failure events).
        """
        detail = detail or {}
        event: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "severity": severity,
            "sensor_id": sensor_id,
            "user_id": user_id,
            "detail": detail,
            "ip_address": ip_address,
        }

        # 1. Structured stdout (always available, even without DB)
        self._emit_stdout(event)

        # 2. Database (Phase 2: repo.insert_audit implemented)
        if self._repo:
            try:
                await self._repo.insert_audit(
                    event_type=event_type,
                    severity=severity,
                    sensor_id=sensor_id,
                    user_id=user_id,
                    detail=detail,
                    ip_address=ip_address,
                )
            except Exception as exc:
                logger.error("Failed to persist audit event to DB: %s", exc)

    @staticmethod
    def _emit_stdout(event: dict[str, Any]) -> None:
        """Write JSON-line audit event to stdout."""
        try:
            print(json.dumps({"audit": event}), file=sys.stdout, flush=True)
        except Exception:
            pass  # Never crash on audit logging
