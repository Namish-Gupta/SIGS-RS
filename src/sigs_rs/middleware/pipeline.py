"""
Pipeline orchestrator — wires Inspector → Anonymizer → Persistence.

Consumes packets from the IngestionQueue, passes each through the
security middleware, and forwards sanitized packets to the DB.

SRS BR-2: under no circumstances shall telemetry bypass the middleware layers.
SRS REQ-SF-3: if this pipeline crashes, the system enters fail-closed state.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from sigs_rs.common.constants import AuditEvent
from sigs_rs.common.exceptions import AnonymizerError, CircuitBreakerOpenError
from sigs_rs.middleware.inspector import VerificationResult
from sigs_rs.observability.metrics import MetricsCollector, get_metrics

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
    Uses bounded batching with a flush interval for high throughput
    while preserving the strict Inspector → Anonymizer → Persistence invariant.
    """

    def __init__(
        self,
        queue: "IngestionQueue",
        inspector: "AnomalousPacketInspector",
        anonymizer: "PrivacyAnonymizer",
        repository: "TelemetryRepository",
        circuit_breaker: "CircuitBreaker",
        audit_logger: "AuditLogger",
        batch_size: int = 100,
        flush_interval_seconds: float = 0.05,
        metrics: "MetricsCollector | None" = None,
    ) -> None:
        self._queue = queue
        self._inspector = inspector
        self._anonymizer = anonymizer
        self._repo = repository
        self._cb = circuit_breaker
        self._audit = audit_logger
        self._batch_size = max(1, batch_size)
        self._flush_interval = max(0.001, flush_interval_seconds)
        self._metrics = metrics or get_metrics()
        self._running = False
        self._processed = 0
        self._sanitized = 0

    async def run(self) -> None:
        """
        Consume packets from the queue in batches and process them.
        """
        logger.info(
            "Middleware pipeline started (batch_size=%d, flush_interval=%.3fs).",
            self._batch_size,
            self._flush_interval,
        )
        self._running = True
        loop = asyncio.get_running_loop()

        while self._running:
            try:
                packet = await self._queue.get()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error retrieving packet from queue: %s", exc)
                continue

            raw_batch = [packet]
            deadline = loop.time() + self._flush_interval

            while len(raw_batch) < self._batch_size:
                timeout = deadline - loop.time()
                if timeout <= 0:
                    break
                try:
                    next_pkt = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                    raw_batch.append(next_pkt)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    break

            try:
                await self.process_batch(raw_batch)
            finally:
                for _ in raw_batch:
                    self._queue.task_done()

    async def process_batch(self, raw_batch: list) -> bool:
        """
        Process a batch of queued packets through the security boundary.

        Each packet individually and independently undergoes:
        1. Inspector verification (rate limits & payload integrity).
        2. Anonymization (stripping PII).

        Malicious or invalid packets are dropped individually without causing
        valid packets in the same batch to be dropped.
        """
        batch_t0 = time.monotonic()
        sanitized_batch = []
        for packet in raw_batch:
            self._processed += 1

            # 1. Inspector
            insp_t0 = time.monotonic()
            insp_res = self._inspector.inspect(packet)
            self._metrics.record_inspector_latency(time.monotonic() - insp_t0)

            if insp_res is not VerificationResult.VERIFIED:
                if insp_res == VerificationResult.RATE_LIMIT_EXCEEDED:
                    self._metrics.record_packet_dropped_rate()
                elif insp_res == VerificationResult.INVALID_METRIC:
                    self._metrics.record_packet_dropped_invalid_metrics()
                continue

            # 2. Privacy Anonymizer
            anon_t0 = time.monotonic()
            try:
                sanitized = self._anonymizer.anonymize(packet)
            except AnonymizerError as exc:
                self._metrics.record_packet_dropped_anonymization()
                if self._audit:
                    await self._audit.log(
                        AuditEvent.ANONYMIZER_FAIL_SECURE,
                        severity="CRITICAL",
                        sensor_id=getattr(packet, "sensor_id", "<unknown>"),
                        detail={"reason": str(exc)},
                    )
                logger.critical(
                    "Catastrophic anonymization failure for sensor %s; FAILING CLOSED.",
                    getattr(packet, "sensor_id", "<unknown>"),
                )
                # Fail closed: do not continue. Stop the routing path.
                self._running = False

                # Signal the rest of the application
                import fastapi
                for obj in [self._repo, self._inspector]: # Try to find app state
                    pass # Not easy to get app from pipeline, let's just crash the pipeline.

                raise RuntimeError(f"Fail-closed: Privacy Anonymizer failure: {exc}") from exc
            finally:
                self._metrics.record_anonymizer_latency(time.monotonic() - anon_t0)

            sanitized_batch.append(sanitized)

        if not sanitized_batch:
            self._metrics.record_processing_latency(time.monotonic() - batch_t0, count=1)
            return False

        # 3. Persistence (via Circuit Breaker)
        db_t0 = time.monotonic()
        try:
            if len(sanitized_batch) == 1:
                await self._cb.call(self._repo.insert(sanitized_batch[0]))
            else:
                await self._cb.call(self._repo.insert_batch(sanitized_batch))
            self._sanitized += len(sanitized_batch)
            self._metrics.record_packet_persisted(len(sanitized_batch))
            return True
        except CircuitBreakerOpenError:
            logger.warning(
                "Circuit breaker open; %d sanitized packet(s) dropped.",
                len(sanitized_batch),
            )
            return False
        except Exception as exc:
            logger.error("Database persistence failed: %s", exc)
            return False
        finally:
            self._metrics.record_database_latency(time.monotonic() - db_t0)
            self._metrics.record_processing_latency(time.monotonic() - batch_t0, count=1)

    async def process_packet(self, packet) -> bool:
        """Process one packet (convenience wrapper over process_batch)."""
        return await self.process_batch([packet])

    async def stop(self) -> None:
        self._running = False

    @property
    def stats(self) -> dict:
        return {
            "processed": self._processed,
            "sanitized": self._sanitized,
        }
