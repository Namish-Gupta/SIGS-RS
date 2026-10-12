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
from fastapi.exceptions import RequestValidationError
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
from sigs_rs.observability.logging import configure_logging
from sigs_rs.observability.metrics import get_metrics
from sigs_rs.observability.middleware import CorrelationIdMiddleware
from sigs_rs.persistence.circuit_breaker import CircuitBreaker
from sigs_rs.persistence.database import DatabasePool
from sigs_rs.persistence.repository import TelemetryRepository

configure_logging(level="INFO", json_format=True)
logger = logging.getLogger(__name__)


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

    circuit_breaker = CircuitBreaker(
        failure_threshold_ms=settings.circuit_breaker_failure_threshold_ms,
        cooldown_seconds=settings.circuit_breaker_cooldown_seconds,
        audit_logger=audit_logger,
        on_open_callback=lambda: _cb_open_handler(app),
        on_close_callback=lambda: _cb_close_handler(app),
        probe_action=repository.probe_connection,
    )

    # ── MQTT Subscriber (created early so CB callbacks can reference it) ─────
    from sigs_rs.ingestion.mqtt_sub import MQTTSubscriber
    mqtt = MQTTSubscriber(
        settings,
        queue=None,
        audit_logger=audit_logger,
        circuit_breaker=circuit_breaker,
        fail_closed_check=lambda: getattr(app.state, "fail_closed", False),
        compliance_check=lambda: getattr(app.state, "compliance_lockout", False),
    )  # queue set below

    # ── Middleware ────────────────────────────────────────────────────────────
    window = SlidingWindow(
        window_seconds=settings.inspector_window_seconds,
        max_packets=settings.inspector_max_packets_per_window,
        redis_url=settings.redis_url,
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
        fail_closed_check=lambda: getattr(app.state, "fail_closed", False),
        compliance_check=lambda: getattr(app.state, "compliance_lockout", False),
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
    app.state.metrics = get_metrics()

    # ── BR-3 Compliance Lockout Verification ─────────────────────────────────
    from sigs_rs.common.constants import COMPLIANCE_GUIDELINE_VERSION
    persisted_compliance = None
    if repository is not None and hasattr(repository, "get_setting"):
        try:
            persisted_compliance = await repository.get_setting("compliance.state")
        except Exception as exc:
            logger.critical("DB failure loading compliance state at startup: %s. Enforcing fail-closed.", exc)
            app.state.compliance_lockout = True
            app.state.compliance_audited = False
            app.state.fail_closed = True

    if isinstance(persisted_compliance, dict):
        app.state.compliance_guideline_version = persisted_compliance.get(
            "guideline_version", settings.compliance_guideline_version
        )
        app.state.compliance_lockout = persisted_compliance.get("lockout_active", False)
        app.state.compliance_audited = persisted_compliance.get("audited", not app.state.compliance_lockout)
        if app.state.compliance_lockout:
            app.state.fail_closed = True
            logger.critical(
                "BR-3 Compliance Lockout active from persistent state (guideline v%s). Testing suspended.",
                app.state.compliance_guideline_version,
            )
    else:
        app.state.compliance_guideline_version = settings.compliance_guideline_version
        app.state.compliance_lockout = False
        app.state.compliance_audited = True

        if settings.compliance_guideline_version != COMPLIANCE_GUIDELINE_VERSION:
            logger.critical(
                "BR-3 Compliance Lockout: Guideline version '%s' differs from baseline '%s'. "
                "Suspending all simulation testing until formal compliance audit.",
                settings.compliance_guideline_version,
                COMPLIANCE_GUIDELINE_VERSION,
            )
            app.state.compliance_lockout = True
            app.state.compliance_audited = False
            app.state.fail_closed = True
            if repository is not None and hasattr(repository, "set_setting"):
                try:
                    await repository.set_setting(
                        key="compliance.state",
                        value={
                            "guideline_version": settings.compliance_guideline_version,
                            "approved_version": COMPLIANCE_GUIDELINE_VERSION,
                            "lockout_active": True,
                            "audited": False,
                        },
                        updated_by="system",
                    )
                except Exception:
                    pass
            await audit_logger.log(
                event_type=AuditEvent.COMPLIANCE_LOCKOUT,
                severity="CRITICAL",
                detail={
                    "configured_version": settings.compliance_guideline_version,
                    "approved_version": COMPLIANCE_GUIDELINE_VERSION,
                    "reason": "Guidelines revised without compliance audit approval (SRS BR-3).",
                },
            )

    # ── Start background tasks ────────────────────────────────────────────────
    import asyncio
    pipeline_task = asyncio.create_task(pipeline.run(), name="middleware-pipeline")
    app.state.mqtt_task = asyncio.create_task(mqtt.start(), name="mqtt-subscriber")

    def _pipeline_died(task: asyncio.Task) -> None:
        try:
            exc = task.exception()
            if exc:
                logger.critical(f"Middleware pipeline CRASHED: {exc}. System entering FAIL CLOSED state.")
                app.state.fail_closed = True
                if hasattr(app.state, "mqtt"):
                    app.state.mqtt._running = False
        except asyncio.CancelledError:
            pass

    pipeline_task.add_done_callback(_pipeline_died)

    # ── Sliding-window cleanup task (Remediation 3) ───────────────────────────
    cleanup_interval = settings.inspector_window_cleanup_interval

    async def _sliding_window_cleanup_loop() -> None:
        """Periodically sweep stale sensor entries from the sliding window."""
        try:
            while True:
                await asyncio.sleep(cleanup_interval)
                removed = await window.cleanup()
                if removed:
                    logger.debug("Sliding-window cleanup: removed %d stale sensor(s).", removed)
        except asyncio.CancelledError:
            logger.info("Sliding-window cleanup task cancelled.")

    cleanup_task = asyncio.create_task(
        _sliding_window_cleanup_loop(), name="sliding-window-cleanup"
    )

    # ── Compliance synchronization loop (SRS BR-3) ──────────────────────────
    async def _compliance_sync_loop() -> None:
        """Periodically sync compliance state from persistent database across instances (SRS BR-3)."""
        sync_interval = 1.0  # 1-second interval ensures sub-second/1s cross-instance freshness
        try:
            while True:
                await asyncio.sleep(sync_interval)
                if repository is not None and hasattr(repository, "get_setting"):
                    try:
                        db_state = await repository.get_setting("compliance.state")
                        if isinstance(db_state, dict):
                            app.state.compliance_guideline_version = db_state.get(
                                "guideline_version",
                                getattr(app.state, "compliance_guideline_version", settings.compliance_guideline_version),
                            )
                            app.state.compliance_lockout = db_state.get("lockout_active", False)
                            app.state.compliance_audited = db_state.get("audited", not app.state.compliance_lockout)
                            if app.state.compliance_lockout:
                                app.state.fail_closed = True
                    except Exception as exc:
                        logger.critical("DB failure during compliance sync: %s. Enforcing fail-closed.", exc)
                        app.state.compliance_lockout = True
                        app.state.compliance_audited = False
                        app.state.fail_closed = True
        except asyncio.CancelledError:
            logger.info("Compliance sync task cancelled.")

    compliance_task = asyncio.create_task(
        _compliance_sync_loop(), name="compliance-sync"
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
    if current_mqtt_task and not current_mqtt_task.done():
        try:
            await asyncio.wait_for(asyncio.shield(current_mqtt_task), timeout=0.5)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            if not current_mqtt_task.done():
                current_mqtt_task.cancel()
                try:
                    await current_mqtt_task
                except asyncio.CancelledError:
                    pass

    for task in (pipeline_task, cleanup_task, compliance_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    await audit_logger.log(AuditEvent.SYSTEM_SHUTDOWN)
    inspector = getattr(app.state, "inspector", None)
    if inspector and hasattr(inspector, "_window") and hasattr(inspector._window, "close"):
        await inspector._window.close()
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

    if getattr(app.state, "fail_closed", False):
        logger.warning("Circuit breaker CLOSED, but system is in FAIL-CLOSED state. MQTT will NOT be restarted.")
        return

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

            if getattr(app.state, "fail_closed", False):
                logger.warning("System entered FAIL-CLOSED state while waiting for MQTT restart. Restart aborted.")
                return

            if getattr(app.state, "is_shutting_down", False):
                return

            mqtt._stop_requested = False
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
    app.add_middleware(CorrelationIdMiddleware)

    class PayloadSizeLimitMiddleware:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await self.app(scope, receive, send)

            # Fast path check Content-Length
            content_length = dict(scope.get("headers", [])).get(b"content-length")
            if content_length:
                try:
                    if int(content_length) > settings.max_payload_size_bytes:
                        if scope["path"].startswith("/api/v1/telemetry"):
                            metrics = get_metrics()
                            metrics.record_packet_received(1)
                            metrics.record_packet_malformed(1)
                        await self._send_413(send)
                        return
                except ValueError:
                    pass

            # Slow path: read stream
            body_bytes = bytearray()
            more_body = True

            while more_body:
                message = await receive()
                if message["type"] == "http.disconnect":
                    # Client disconnected early, pass it down
                    async def early_disconnect_receive():
                        return {"type": "http.disconnect"}
                    return await self.app(scope, early_disconnect_receive, send)

                chunk = message.get("body", b"")
                more_body = message.get("more_body", False)
                body_bytes.extend(chunk)

                if len(body_bytes) > settings.max_payload_size_bytes:
                    if scope["path"].startswith("/api/v1/telemetry"):
                        metrics = get_metrics()
                        metrics.record_packet_received(1)
                        metrics.record_packet_malformed(1)
                    await self._send_413(send)
                    return

            chunks = [bytes(body_bytes)]

            async def new_receive():
                if chunks:
                    chunk = chunks.pop(0)
                    return {"type": "http.request", "body": chunk, "more_body": False}
                return await receive()

            await self.app(scope, new_receive, send)

        async def _send_413(self, send):
            import json
            response = json.dumps({"detail": f"Payload exceeds {settings.max_payload_size_bytes} bytes limit."}).encode()
            await send({
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(response)).encode())
                ]
            })
            await send({
                "type": "http.response.body",
                "body": response,
            })

    app.add_middleware(PayloadSizeLimitMiddleware)

    # ── Routers ───────────────────────────────────────────────────────────────
    app.include_router(health_router)
    app.include_router(telemetry_router)
    app.include_router(auth_router)
    app.include_router(config_router)
    app.include_router(audit_router)

    # ── Global exception handlers ─────────────────────────────────────────────
    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        if request.url.path.startswith("/api/v1/telemetry"):
            metrics = getattr(app.state, "metrics", None) or get_metrics()
            count = 1
            body = getattr(exc, "body", None)
            if request.url.path.endswith("/batch") and isinstance(body, dict) and "packets" in body and isinstance(body["packets"], list):
                count = len(body["packets"])
            metrics.record_packet_received(count)
            metrics.record_packet_malformed(count)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": exc.errors()},
        )

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
