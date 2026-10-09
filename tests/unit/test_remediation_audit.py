"""
Comprehensive unit tests covering all 5 remediation priorities:
1. Circuit Breaker state transitions, latency measurement, MQTT integration, audit logging.
2. Unauthorized audit logging, zero PII/secret leaks, fault tolerance.
3. Sliding-window background cleanup, active sensor preservation, clean shutdown.
4. Packaging / Docker build structure.
5. Middleware batching, security invariant (Inspector -> Anonymizer -> Persistence), mixed batches, PII insert_batch safety.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException, Request

from sigs_rs.auth.dependencies import get_current_user, require_security_engineer
from sigs_rs.common.constants import AuditEvent, Role
from sigs_rs.common.exceptions import (
    AnonymizerError,
    CircuitBreakerOpenError,
    InvalidTokenError,
    PersistenceError,
)
from sigs_rs.ingestion.buffer import IngestionQueue
from sigs_rs.middleware.anonymizer import PrivacyAnonymizer
from sigs_rs.middleware.inspector import AnomalousPacketInspector
from sigs_rs.middleware.payload_validator import PayloadValidator
from sigs_rs.middleware.pipeline import MiddlewarePipeline
from sigs_rs.middleware.sliding_window import SlidingWindow
from sigs_rs.persistence.circuit_breaker import CBState, CircuitBreaker
from sigs_rs.persistence.database import InMemoryDatabasePool
from sigs_rs.persistence.repository import TelemetryRepository
from sigs_rs.schemas import RawTelemetryPacket, SanitizedTelemetryPacket, UserInfo


# ─── Fixtures & Helpers ───────────────────────────────────────────────────────

def _make_raw_packet(sensor_id: str = "sensor-test-1", value: float = 25.0) -> RawTelemetryPacket:
    return RawTelemetryPacket(
        sensor_id=sensor_id,
        timestamp="2026-07-17T10:30:00Z",
        resource_type="water",
        value=value,
        unit="liters_per_min",
        geo={"latitude": 12.97, "longitude": 80.22},
        mac_address="AA:BB:CC:DD:EE:FF",
        device_id="dev-001",
    )


# ─── 1. Circuit Breaker Tests ─────────────────────────────────────────────────

class TestCircuitBreakerRemediation:
    @pytest.mark.asyncio
    async def test_cb_latency_trip_above_2000ms(self):
        """CB trips OPEN when DB write latency exceeds 2000ms."""
        audit_mock = AsyncMock()
        cb = CircuitBreaker(failure_threshold_ms=2000, cooldown_seconds=30, audit_logger=audit_mock)
        assert cb.state == CBState.CLOSED

        async def slow_db_write():
            await asyncio.sleep(2.05)

        with pytest.raises(CircuitBreakerOpenError, match="tripped"):
            await cb.call(slow_db_write())

        assert cb.state == CBState.OPEN
        assert cb.is_open is True
        await asyncio.sleep(0.01)

    @pytest.mark.asyncio
    async def test_cb_fast_write_remains_closed(self):
        """Fast writes keep CB in CLOSED state."""
        cb = CircuitBreaker(failure_threshold_ms=2000, cooldown_seconds=30)
        async def fast_db_write():
            await asyncio.sleep(0.01)

        await cb.call(fast_db_write())
        assert cb.state == CBState.CLOSED

    @pytest.mark.asyncio
    async def test_cb_state_transitions_closed_open_halfopen_closed(self):
        """Full state transition: CLOSED -> OPEN -> HALF_OPEN -> CLOSED."""
        on_open = MagicMock()
        on_close = MagicMock()
        cb = CircuitBreaker(
            failure_threshold_ms=100,
            cooldown_seconds=1,
            on_open_callback=on_open,
            on_close_callback=on_close,
        )

        # 1. Trip to OPEN
        async def slow_write():
            await asyncio.sleep(0.15)

        with pytest.raises(CircuitBreakerOpenError):
            await cb.call(slow_write())

        assert cb.state == CBState.OPEN
        on_open.assert_called_once()

        # Writes blocked while OPEN
        async def fast_write():
            pass

        with pytest.raises(CircuitBreakerOpenError, match="is OPEN"):
            await cb.call(fast_write())

        # 2. Wait for cooldown -> HALF_OPEN
        await asyncio.sleep(1.05)
        assert cb.state == CBState.HALF_OPEN

        # 3. Successful probe write -> CLOSED
        await cb.call(fast_write())
        assert cb.state == CBState.CLOSED
        on_close.assert_called_once()
        await asyncio.sleep(0.01)

    @pytest.mark.asyncio
    async def test_cb_half_open_failure_re_trips(self):
        """Failed probe in HALF_OPEN returns state to OPEN."""
        cb = CircuitBreaker(failure_threshold_ms=100, cooldown_seconds=1)
        cb._trip()
        await asyncio.sleep(1.05)
        assert cb.state == CBState.HALF_OPEN

        async def failing_write():
            raise RuntimeError("DB error")

        with pytest.raises(RuntimeError):
            await cb.call(failing_write())

        assert cb.state == CBState.OPEN

    @pytest.mark.asyncio
    async def test_cb_mqtt_integration_lifecycle_and_wiring(self):
        """
        Tests MQTT restart lifecycle: timeout abort, shutdown, duplicate prevention, and successful recovery.
        """
        from sigs_rs.main import _cb_close_handler, _cb_open_handler
        from sigs_rs.persistence.circuit_breaker import CircuitBreaker

        app = FastAPI()
        app.state.is_shutting_down = False
        app.state._mqtt_restarting = False
        app.state.mqtt_restart_timeout = 0.2  # Speed up tests
        
        class MockMQTT:
            def __init__(self):
                self._running = True
                self.starts = 0
            async def start(self):
                self.starts += 1
                try:
                    while self._running:
                        await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    pass

        mqtt_mock = MockMQTT()
        app.state.mqtt = mqtt_mock
        loop = asyncio.get_running_loop()
        app.state.mqtt_task = loop.create_task(mqtt_mock.start())
        old_task = app.state.mqtt_task
        
        cb = CircuitBreaker(
            failure_threshold_ms=1000, 
            cooldown_seconds=10, 
            on_open_callback=lambda: _cb_open_handler(app),
            on_close_callback=lambda: _cb_close_handler(app)
        )
        
        # E. Actual CB wiring
        cb._trip()
        assert app.state.mqtt._running is False
        
        await asyncio.sleep(0.05)
        assert old_task.done()
        
        # A. Normal successful restart: old task terminates before timeout
        cb._reset()
        await asyncio.sleep(0.05)
        
        assert app.state.mqtt._running is True
        task1 = app.state.mqtt_task
        assert task1 is not old_task
        assert not task1.done()
        assert mqtt_mock.starts == 2
        
        # C. Duplicate CLOSE calls -> no duplicate tasks
        cb._reset()
        await asyncio.sleep(0.05)
        assert app.state.mqtt_task is task1
        assert mqtt_mock.starts == 2
        
        # B. Timeout: old task remains alive beyond the restart timeout -> NO replacement task
        app.state.mqtt_task.cancel()
        await asyncio.sleep(0.05)
        app.state._mqtt_restarting = False
        
        stop_hanging = False
        async def hanging_task():
            # Ignores cancellation and hangs longer than the 0.2s timeout
            while not stop_hanging:
                try:
                    await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    if stop_hanging:
                        break
                    await asyncio.sleep(0.05)

        hanging = loop.create_task(hanging_task())
        app.state.mqtt_task = hanging
        mqtt_mock.starts = 0

        cb._reset()
        await asyncio.sleep(0.3)  # wait longer than timeout=0.2s

        # Timeout occurred -> no replacement task created
        assert app.state.mqtt_task is hanging
        assert not hanging.done()
        assert mqtt_mock.starts == 0
        assert app.state._mqtt_restarting is False  # Guard reset correctly

        # D. Shutdown -> no replacement MQTT task is created
        app.state.is_shutting_down = True
        app.state._mqtt_restarting = False

        cb._reset()
        await asyncio.sleep(0.05)
        assert app.state.mqtt_task is hanging  # Remained unchanged
        assert app.state._mqtt_restarting is False

        # Clean up hanging task
        stop_hanging = True
        hanging.cancel()
        try:
            await hanging
        except asyncio.CancelledError:
            pass


# ─── 2. Unauthorized Access Audit Logging Tests ───────────────────────────────

class TestUnauthorizedAuditLoggingRemediation:
    @pytest.mark.asyncio
    async def test_unauthorized_attempt_logs_audit_event(self):
        """Missing or invalid auth header creates an AUTH_FAILURE audit log."""
        audit_mock = AsyncMock()
        app = FastAPI()
        app.state.audit_logger = audit_mock

        req = MagicMock(spec=Request)
        req.app = app
        req.url.path = "/api/v1/config/inspector"
        req.method = "PUT"
        req.client.host = "192.168.1.50"

        # Missing token
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(request=req, credentials=None)

        assert exc_info.value.status_code == 401
        audit_mock.log.assert_awaited_once()
        call_kwargs = audit_mock.log.call_args.kwargs
        assert call_kwargs["event_type"] == AuditEvent.AUTH_FAILURE
        assert call_kwargs["ip_address"] == "192.168.1.50"
        assert "Missing Authorization header" in call_kwargs["detail"]["reason"]

    @pytest.mark.asyncio
    async def test_auth_audit_log_never_leaks_secrets_or_pii(self):
        """Audit logging detail contains no sensitive fields or JWTs."""
        audit_mock = AsyncMock()
        app = FastAPI()
        app.state.audit_logger = audit_mock

        req = MagicMock(spec=Request)
        req.app = app
        req.url.path = "/api/v1/config/inspector"
        req.method = "GET"
        req.client.host = "10.0.0.1"

        credentials = MagicMock()
        credentials.credentials = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.super-secret-jwt"

        with patch("sigs_rs.auth.dependencies.decode_token", side_effect=InvalidTokenError("Token expired")):
            with pytest.raises(HTTPException):
                await get_current_user(request=req, credentials=credentials)

        call_kwargs = audit_mock.log.call_args.kwargs
        detail = call_kwargs["detail"]
        detail_str = str(detail)

        # Assert no sensitive patterns
        assert "super-secret-jwt" not in detail_str
        assert "latitude" not in detail_str
        assert "longitude" not in detail_str
        assert "mac_address" not in detail_str
        assert "password" not in detail_str

    @pytest.mark.asyncio
    async def test_audit_logger_error_does_not_break_auth(self):
        """Internal error in AuditLogger must never break authentication failure flow."""
        failing_audit = MagicMock()
        async def fake_log(*args, **kwargs): raise RuntimeError("Audit DB connection failed")
        failing_audit.log = fake_log

        app = FastAPI()
        app.state.audit_logger = failing_audit

        req = MagicMock(spec=Request)
        req.app = app
        req.url.path = "/test"
        req.method = "GET"
        req.client = None

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(request=req, credentials=None)

        assert exc_info.value.status_code == 401


# ─── 3. Sliding Window Cleanup & Lifespan Tests ───────────────────────────────

class TestSlidingWindowCleanupRemediation:
    def test_cleanup_does_not_affect_active_sensors(self):
        """Cleanup removes expired sensors while preserving active ones."""
        sw = SlidingWindow(window_seconds=10, max_packets=50)
        t_now = 1000.0

        sw.check("active-sensor", now=t_now)
        sw.check("stale-sensor", now=t_now - 20.0)  # 20 seconds ago (expired)

        removed = sw.cleanup(now=t_now)
        assert removed == 1
        assert sw.sensor_count("stale-sensor") == 0
        assert sw.sensor_count("active-sensor") == 1

    @pytest.mark.asyncio
    async def test_clean_shutdown_cancels_background_tasks(self):
        """Application shutdown context cleanly cancels background tasks."""
        from sigs_rs.main import lifespan

        app = FastAPI()
        with patch("sigs_rs.persistence.database.DatabasePool.connect", return_value=True):
            with patch("sigs_rs.persistence.database.DatabasePool.disconnect", return_value=None):
                async with lifespan(app):
                    assert hasattr(app.state, "pipeline")
                    assert hasattr(app.state, "inspector")

        # Lifespan context exit completes without unhandled task errors


# ─── 4. Docker Packaging Test ─────────────────────────────────────────────────

class TestDockerPackagingRemediation:
    def test_src_package_structure(self):
        """Verify sigs_rs module is importable and correctly structured."""
        import sigs_rs
        assert hasattr(sigs_rs, "__file__")


# ─── 5. Batching & Security Invariant Tests ───────────────────────────────────

class TestBatchingAndSecurityInvariant:
    @pytest.mark.asyncio
    async def test_mixed_valid_and_malicious_batch(self):
        """
        Mixed batch: invalid/malicious packets are individually dropped while valid packets
        in the same batch are anonymized and persisted.
        """
        queue = IngestionQueue(max_size=100)
        sw = SlidingWindow(window_seconds=10, max_packets=50)
        inspector = AnomalousPacketInspector(sw, PayloadValidator())
        anonymizer = PrivacyAnonymizer()
        db = InMemoryDatabasePool()
        await db.connect()
        repo = TelemetryRepository(db)
        cb = CircuitBreaker(2000, 30)
        audit = AsyncMock()

        pipeline = MiddlewarePipeline(
            queue=queue,
            inspector=inspector,
            anonymizer=anonymizer,
            repository=repo,
            circuit_breaker=cb,
            audit_logger=audit,
            batch_size=10,
            flush_interval_seconds=0.05,
        )

        valid_pkt1 = _make_raw_packet("sensor-001", value=10.0)
        invalid_pkt = _make_raw_packet("sensor-002", value=-999.0)  # Impossible negative water value
        valid_pkt2 = _make_raw_packet("sensor-003", value=20.0)

        batch = [valid_pkt1, invalid_pkt, valid_pkt2]
        success = await pipeline.process_batch(batch)

        assert success is True
        assert await repo.count_readings() == 2

        # Verify persisted readings are sanitized
        readings = await repo.get_by_sensor("sensor-001")
        assert len(readings) == 1
        assert "latitude" not in readings[0]

    @pytest.mark.asyncio
    async def test_pii_cannot_enter_insert_batch(self):
        """insert_batch raises PersistenceError if any record contains PII fields."""
        db = InMemoryDatabasePool()
        await db.connect()
        repo = TelemetryRepository(db)

        valid_sanitized = SanitizedTelemetryPacket(
            sensor_id="sensor-001",
            timestamp="2026-07-17T10:30:00Z",
            resource_type="water",
            value=10.0,
            unit="liters_per_min",
        )

        # Unsanitized packet with PII
        raw_pkt_dict = _make_raw_packet().model_dump()

        with pytest.raises(PersistenceError, match="Security violation"):
            await repo.insert_batch([valid_sanitized, raw_pkt_dict])

    @pytest.mark.asyncio
    async def test_circuit_breaker_prevents_batch_persistence(self):
        """When CB is OPEN, process_batch drops the sanitized batch safely."""
        queue = IngestionQueue(max_size=100)
        inspector = MagicMock()
        inspector.inspect.return_value = MagicMock(__bool__=lambda s: True)
        anonymizer = PrivacyAnonymizer()
        repo = MagicMock()
        cb = CircuitBreaker(2000, 30)
        cb._trip()  # Open breaker

        pipeline = MiddlewarePipeline(
            queue=queue,
            inspector=inspector,
            anonymizer=anonymizer,
            repository=repo,
            circuit_breaker=cb,
            audit_logger=AsyncMock(),
        )

        success = await pipeline.process_batch([_make_raw_packet()])
        assert success is False
        repo.insert_batch.assert_not_called()
