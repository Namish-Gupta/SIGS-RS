"""
Anomalous Packet Inspector — primary security middleware subsystem.

SRS §4.2: evaluates packet rate (sliding window) and payload integrity
           (impossible values) and drops offending packets silently.

SRS REQ-API-4: complete validation in < 15 ms per packet.
SRS REQ-API-5: no rejection ACK sent to client.

Inspection order:
  1. Rate-limit check (SlidingWindow)      — cheapest, O(1) amortized
  2. Impossible-value check (PayloadValidator) — stateless comparison

If step 1 fails the packet is dropped immediately without running step 2.
This ordering minimises work for the most common attack vector (flooding).

The module has ZERO I/O dependencies.  The optional AuditLogger is used
fire-and-forget (asyncio.create_task) and its failure never blocks the
pipeline.  This satisfies SRS REQ-QA-4: independently testable without
live MQTT, FastAPI, or PostgreSQL.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import time
from typing import TYPE_CHECKING

from sigs_rs.common.constants import AuditEvent
from sigs_rs.common.exceptions import ImpossibleValueError, RateExceededError
from sigs_rs.middleware.payload_validator import PayloadValidator
from sigs_rs.middleware.sliding_window import SlidingWindow
from sigs_rs.telemetry.models import RawTelemetryPacket

if TYPE_CHECKING:
    from sigs_rs.management.audit_logger import AuditLogger

logger = logging.getLogger(__name__)


# ─── Verification result enum ─────────────────────────────────────────────────

class VerificationResult(enum.Enum):
    """
    Outcome of a single packet inspection.

    Used by inspect() so callers get a typed, descriptive result rather
    than a bare boolean, while still being truthy/falsy for convenience.
    """
    VERIFIED           = "VERIFIED"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    INVALID_METRIC     = "INVALID_METRIC"

    def __bool__(self) -> bool:
        """VERIFIED is truthy; everything else is falsy."""
        return self is VerificationResult.VERIFIED


# ─── Inspector ─────────────────────────────────────────────────────────────────

class AnomalousPacketInspector:
    """
    Primary security middleware.

    Combines:
    - SlidingWindow: per-sensor rate limiting (SRS REQ-API-1/2)
    - PayloadValidator: impossible-value detection (SRS REQ-API-3)

    Packets that pass both checks are forwarded to the Privacy Anonymizer.
    Dropped packets are silently discarded (SRS REQ-API-5) and audit-logged.
    """

    __slots__ = (
        "_window", "_validator", "_audit",
        "_dropped_rate", "_dropped_impossible", "_passed",
    )

    def __init__(
        self,
        window: SlidingWindow,
        validator: PayloadValidator,
        audit_logger: "AuditLogger | None" = None,
    ) -> None:
        self._window = window
        self._validator = validator
        self._audit = audit_logger
        self._dropped_rate: int = 0
        self._dropped_impossible: int = 0
        self._passed: int = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def inspect(self, packet: RawTelemetryPacket) -> VerificationResult:
        """
        Run the full inspection pipeline on a single packet.

        Returns:
            VerificationResult.VERIFIED            — forward to Anonymizer.
            VerificationResult.RATE_LIMIT_EXCEEDED  — silently dropped.
            VerificationResult.INVALID_METRIC       — silently dropped.

        Does NOT raise exceptions — all errors are handled internally
        so the ingestion loop cannot crash on a bad packet.
        """
        sensor_id = packet.sensor_id

        # ── Step 1: Rate-limit check (cheapest first) ─────────────────────
        if not self._window.check(sensor_id):
            self._dropped_rate += 1
            self._audit_drop(
                AuditEvent.PACKET_DROPPED_RATE,
                sensor_id,
                f"Rate limit exceeded ({self._window.max_packets} packets / "
                f"{self._window.window_seconds}s window)",
            )
            return VerificationResult.RATE_LIMIT_EXCEEDED

        # ── Step 2: Impossible-value check ────────────────────────────────
        try:
            self._validator.validate(packet)
        except ImpossibleValueError as exc:
            self._dropped_impossible += 1
            self._audit_drop(
                AuditEvent.PACKET_DROPPED_IMPOSSIBLE,
                sensor_id,
                str(exc),
            )
            return VerificationResult.INVALID_METRIC

        # ── All checks passed ─────────────────────────────────────────────
        self._passed += 1
        return VerificationResult.VERIFIED

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def stats(self) -> dict:
        """Return current inspection statistics for the /metrics endpoint."""
        return {
            "passed": self._passed,
            "dropped_rate": self._dropped_rate,
            "dropped_impossible": self._dropped_impossible,
            "total_inspected": self._passed + self._dropped_rate + self._dropped_impossible,
        }

    @property
    def window(self) -> SlidingWindow:
        """Expose the sliding window for external introspection (metrics, config)."""
        return self._window

    @property
    def validator(self) -> PayloadValidator:
        """Expose the validator for external introspection (metrics, config)."""
        return self._validator

    # ── Internals ─────────────────────────────────────────────────────────────

    def _audit_drop(
        self,
        event_type: str,
        sensor_id: str,
        reason: str,
    ) -> None:
        """
        Log a packet-drop audit event.

        Uses fire-and-forget asyncio.create_task when inside a running
        event loop, otherwise logs to the Python logger only.  The audit
        logger's failure NEVER blocks the inspection pipeline.
        """
        logger.warning(
            "Packet dropped: event=%s sensor=%s reason=%s",
            event_type, sensor_id, reason,
        )

        if self._audit is None:
            return

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                self._audit.log(
                    event_type=event_type,
                    severity="WARN",
                    sensor_id=sensor_id,
                    detail={"reason": reason},
                )
            )
        except RuntimeError:
            # No running event loop — log only (happens in sync tests).
            pass
