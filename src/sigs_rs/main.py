"""
SIGS-RS — Application entrypoint.

Wires all components together:
  - FastAPI application with lifespan context
  - All routers (telemetry ingestion, auth, config, audit, health)
  - Shared state (queue, inspector, anonymizer, pipeline, db, circuit breaker)
  - Exception handlers

Run with:
    uvicorn sigs_rs.main:app --reload
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from sigs_rs.auth.auth_router import router as auth_router
from sigs_rs.common.constants import AuditEvent
from sigs_rs.common.exceptions import (
    AnonymizerError,
    CircuitBreakerOpenError,
    IngestionError,
    QueueFullError,
    SIGSError,
)
from sigs_rs.config import get_settings
from sigs_rs.ingestion.buffer import IngestionQueue
from sigs_rs.ingestion.http_ingest import router as telemetry_router
from sigs_rs.management.audit_api import router as audit_router
from sigs_rs.management.audit_logger import AuditLogger
from sigs_rs.management.config_api import router as config_router
from sigs_rs.management.health import router as health_router
from sigs_rs.middleware.anonymizer import PrivacyAnonymizer
from sigs_rs.middleware.inspector import AnomalousPacketInspector
from sigs_rs.middleware.payload_validator import PayloadValidator
from sigs_rs.middleware.pipeline import MiddlewarePipeline
from sigs_rs.middleware.sliding_window import SlidingWindow
from sigs_rs.persistence.circuit_breaker import CircuitBreaker
from sigs_rs.persistence.database import DatabasePool
from sigs_rs.persistence.repository import TelemetryRepository

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Application lifespan: initialize all shared state on startup,
    clean up on shutdown.

    Phase 1: creates all component objects and attaches to app.state.
    Phase 2-4: adds real DB connection, MQTT subscriber, pipeline task.
    """
    settings = get_settings()
    logger.info("SIGS-RS starting up (env=%s).", settings.app_env)

    # ── Persistence ──────────────────────────────────────────────────────────
    db = DatabasePool(settings.database_url, settings.database_ssl_mode)
    connected = await db.connect()
    if not connected and settings.app_env.lower() in ("development", "test", "testing"):
        from sigs_rs.persistence.database import InMemoryDatabasePool
        logger.info(
            "Database pool unavailable in %s mode; falling back to InMemoryDatabasePool.",
            settings.app_env,
        )
        db = InMemoryDatabasePool()
        await db.connect()

    repository = TelemetryRepository(db)
    audit_logger = AuditLogger(repository)

    # ── MQTT Subscriber (created early so CB callbacks can reference it) ─────
    from sigs_rs.ingestion.mqtt_sub import MQTTSubscriber
    mqtt = MQTTSubscriber(settings, queue=None, audit_logger=audit_logger)  # queue set below

    circuit_breaker = CircuitBreaker(
        failure_threshold_ms=settings.circuit_breaker_failure_threshold_ms,
        cooldown_seconds=settings.circuit_breaker_cooldown_seconds,
        audit_logger=audit_logger,
        on_open_callback=lambda: _cb_open_handler(app),
        on_close_callback=lambda: _cb_close_handler(app),
    )

    # ── Middleware ────────────────────────────────────────────────────────────
    window = SlidingWindow(
        window_seconds=settings.inspector_window_seconds,
        max_packets=settings.inspector_max_packets_per_window,
    )
    validator = PayloadValidator()
    inspector = AnomalousPacketInspector(window, validator, audit_logger)
    anonymizer = PrivacyAnonymizer(pii_fields=settings.pii_fields)

    # ── Ingestion ─────────────────────────────────────────────────────────────
    queue = IngestionQueue(max_size=settings.ingestion_queue_max_size)

    # Wire queue into the MQTT subscriber now that it exists
    mqtt._queue = queue

    # ── Pipeline ──────────────────────────────────────────────────────────────
    pipeline = MiddlewarePipeline(
        queue=queue,
        inspector=inspector,
        anonymizer=anonymizer,
        repository=repository,
        circuit_breaker=circuit_breaker,
        audit_logger=audit_logger,
    )

    # ── Attach to app state ───────────────────────────────────────────────────
    app.state.db = db
    app.state.repository = repository
    app.state.queue = queue
    app.state.inspector = inspector
    app.state.anonymizer = anonymizer
    app.state.pipeline = pipeline
    app.state.circuit_breaker = circuit_breaker
    app.state.audit_logger = audit_logger
    app.state.mqtt = mqtt
    app.state.fail_closed = False
    app.state.environment = settings.app_env

    # ── Start background tasks ────────────────────────────────────────────────
    import asyncio
    pipeline_task = asyncio.create_task(pipeline.run(), name="middleware-pipeline")
    app.state.mqtt_task = asyncio.create_task(mqtt.start(), name="mqtt-subscriber")

    # ── Sliding-window cleanup task (Remediation 3) ───────────────────────────
    cleanup_interval = settings.inspector_window_cleanup_interval

    async def _sliding_window_cleanup_loop() -> None:
        """Periodically sweep stale sensor entries from the sliding window."""
        try:
            while True:
                await asyncio.sleep(cleanup_interval)
                removed = window.cleanup()
                if removed:
                    logger.debug("Sliding-window cleanup: removed %d stale sensor(s).", removed)
        except asyncio.CancelledError:
            logger.info("Sliding-window cleanup task cancelled.")

    cleanup_task = asyncio.create_task(
        _sliding_window_cleanup_loop(), name="sliding-window-cleanup"
    )

    await audit_logger.log(AuditEvent.SYSTEM_STARTUP, detail={"env": settings.app_env})
    logger.info("SIGS-RS ready.")

    yield  # ── Application runs ──────────────────────────────────────────────

    # ── Shutdown ──────────────────────────────────────────────────────────────
    logger.info("SIGS-RS shutting down.")
    app.state.is_shutting_down = True
    await pipeline.stop()
    await mqtt.stop()

    # Cancel background tasks
    current_mqtt_task = getattr(app.state, "mqtt_task", None)
    for task in (pipeline_task, current_mqtt_task, cleanup_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    await audit_logger.log(AuditEvent.SYSTEM_SHUTDOWN)
    await db.disconnect()
    logger.info("SIGS-RS shutdown complete.")


def _cb_open_handler(app: FastAPI) -> None:
    """
    Called synchronously when the circuit breaker trips.
    SRS REQ-SF-2: drop all incoming MQTT connections when CB is OPEN.
    """
    logger.critical("Circuit breaker OPEN — signalling MQTT subscriber to stop accepting traffic.")
    if hasattr(app.state, "mqtt"):
        app.state.mqtt._running = False


def _cb_close_handler(app: FastAPI) -> None:
    """
    Called synchronously when the circuit breaker resets.
    Re-enable MQTT traffic acceptance via an asynchronous restart task to handle shutdown races.
    """
    logger.info("Circuit breaker CLOSED — re-enabling MQTT subscriber.")
    if getattr(app.state, "is_shutting_down", False):
        return

    mqtt = getattr(app.state, "mqtt", None)
    if not mqtt:
        return

    import asyncio

    if getattr(app.state, "_mqtt_restarting", False):
        return
    app.state._mqtt_restarting = True

    async def restart_mqtt() -> None:
        try:
            old_task = getattr(app.state, "mqtt_task", None)
            if old_task and not old_task.done():
                timeout_val = getattr(app.state, "mqtt_restart_timeout", 5.0)
                try:
                    await asyncio.wait_for(asyncio.shield(old_task), timeout=timeout_val)
                except asyncio.TimeoutError:
                    logger.error("MQTT restart aborted: previous MQTT task failed to terminate within the %.1fs timeout.", timeout_val)
                    return
                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    logger.error("MQTT restart aborted: unexpected error waiting for old task: %s", exc)
                    return

            if getattr(app.state, "is_shutting_down", False):
                return

            mqtt._running = True
            loop = asyncio.get_running_loop()
            app.state.mqtt_task = loop.create_task(mqtt.start(), name="mqtt-subscriber")
        finally:
            app.state._mqtt_restarting = False

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(restart_mqtt(), name="mqtt-restarter")
    except RuntimeError:
        pass


# ─── Application factory ──────────────────────────────────────────────────────

def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="SIGS-RS — Secure IoT Gateway Simulation",
        description=(
            "Backend simulation engine for a community sensor grid. "
            "Processes telemetry through the Anomalous Packet Inspector "
            "and Privacy Anonymizer middleware before PostgreSQL/TimescaleDB persistence."
        ),
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # ── Middleware ────────────────────────────────────────────────────────────
    @app.middleware("http")
    async def enforce_payload_size_limit(request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > settings.max_payload_size_bytes:
                    return JSONResponse(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        content={"detail": f"Payload exceeds {settings.max_payload_size_bytes} bytes limit."},
                    )
            except ValueError:
                pass
        return await call_next(request)

    # ── Routers ───────────────────────────────────────────────────────────────
    app.include_router(health_router)
    app.include_router(telemetry_router)
    app.include_router(auth_router)
    app.include_router(config_router)
    app.include_router(audit_router)

    # ── Global exception handlers ─────────────────────────────────────────────
    @app.exception_handler(QueueFullError)
    async def _queue_full(_: Request, exc: QueueFullError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": str(exc)},
        )

    @app.exception_handler(CircuitBreakerOpenError)
    async def _cb_open(_: Request, exc: CircuitBreakerOpenError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "Service temporarily unavailable — circuit breaker open."},
        )

    @app.exception_handler(SIGSError)
    async def _sigs_error(_: Request, exc: SIGSError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": f"Internal gateway error: {type(exc).__name__}"},
        )

    return app


app = create_app()
