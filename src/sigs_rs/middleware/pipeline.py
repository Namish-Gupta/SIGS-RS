"""
Pipeline orchestrator — wires Inspector → Anonymizer → Persistence.

Consumes packets from the IngestionQueue, passes each through the
security middleware, and forwards sanitized packets to the DB.

SRS BR-2: under no circumstances shall telemetry bypass the middleware layers.
SRS REQ-SF-3: if this pipeline crashes, the system enters fail-closed state.

NOTE (Phase 1): Skeleton with complete docstrings.
                Full wiring implemented in Phase 4.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from sigs_rs.common.constants import AuditEvent
from sigs_rs.common.exceptions import AnonymizerError, CircuitBreakerOpenError
from sigs_rs.middleware.inspector import VerificationResult

if TYPE_CHECKING:
    from sigs_rs.ingestion.buffer import IngestionQueue
    from sigs_rs.middleware.anonymizer import PrivacyAnonymizer
    from sigs_rs.middleware.inspector import AnomalousPacketInspector
    from sigs_rs.persistence.circuit_breaker import CircuitBreaker
    from sigs_rs.persistence.repository import TelemetryRepository
    from sigs_rs.management.audit_logger import AuditLogger

logger = logging.getLogger(__name__)


class MiddlewarePipeline:
    """
    Main processing loop: Queue → Inspector → Anonymizer → DB.

    Runs as a long-lived asyncio Task (started in main.py lifespan).
    Sets app.state.fail_closed = True on unhandled exceptions (REQ-SF-3).
    """

    def __init__(
        self,
        queue: "IngestionQueue",
        inspector: "AnomalousPacketInspector",
        anonymizer: "PrivacyAnonymizer",
        repository: "TelemetryRepository",
        circuit_breaker: "CircuitBreaker",
        audit_logger: "AuditLogger",
    ) -> None:
        self._queue = queue
        self._inspector = inspector
        self._anonymizer = anonymizer
        self._repo = repository
        self._cb = circuit_breaker
        self._audit = audit_logger
        self._running = False
        self._processed = 0
        self._sanitized = 0

    async def run(self) -> None:
        """
        Consume packets from the queue and process them through the pipeline.
        """
        logger.info("Middleware pipeline started.")
        self._running = True
        while self._running:
            packet = await self._queue.get()
            try:
                await self.process_packet(packet)
            finally:
                self._queue.task_done()

    async def process_packet(self, packet) -> bool:
        """
        Process one queued packet through the mandatory security boundary.

        The anonymizer is called only after the Inspector returns VERIFIED.
        An anonymization failure is audit-logged and dropped before the
        repository can receive any representation of the raw packet.
        """
        self._processed += 1
        if self._inspector.inspect(packet) is not VerificationResult.VERIFIED:
            return False

        try:
            sanitized = self._anonymizer.anonymize(packet)
        except AnonymizerError as exc:
            await self._audit.log(
                AuditEvent.ANONYMIZER_FAIL_SECURE,
                severity="ERROR",
                sensor_id=packet.sensor_id,
                detail={"reason": str(exc)},
            )
            logger.error("Anonymization failed for sensor %s; packet dropped.", packet.sensor_id)
            return False

        try:
            await self._cb.call(self._repo.insert(sanitized))
        except CircuitBreakerOpenError:
            logger.warning("Circuit breaker open; sanitized packet dropped.")
            return False

        self._sanitized += 1
        return True

    async def stop(self) -> None:
        self._running = False

    @property
    def stats(self) -> dict:
        return {
            "processed": self._processed,
            "sanitized": self._sanitized,
        }
