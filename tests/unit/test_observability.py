"""
Unit and integration tests for Phase 10: Observability.

Verifies:
  - Metrics tracking (all 15 required metrics):
      * total packets received
      * valid packets
      * malformed packets
      * packets dropped by rate threshold
      * packets dropped by invalid metrics
      * packets dropped by anonymization failure
      * packets persisted
      * current queue size
      * processing latency
      * inspector latency
      * anonymizer latency
      * database latency
      * MQTT connection state
      * active sensors
      * circuit breaker state
  - Prometheus exposition format (v0.0.4) on GET /metrics
  - Content negotiation (text/plain vs application/json)
  - Structured JSON logging & PII/sensitive data redaction
  - Correlation ID context tracking and X-Correlation-ID HTTP header propagation
  - Health & Readiness endpoint behaviors
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient, ASGITransport
from fastapi import FastAPI

from sigs_rs.common.constants import AuditEvent
from sigs_rs.common.exceptions import AnonymizerError
from sigs_rs.ingestion.buffer import IngestionQueue
from sigs_rs.main import create_app
from sigs_rs.middleware.inspector import VerificationResult
from sigs_rs.middleware.pipeline import MiddlewarePipeline
from sigs_rs.observability.logging import (
    JsonLogFormatter,
    get_correlation_id,
    get_packet_id,
    redact_sensitive_data,
    set_correlation_id,
    set_packet_id,
)
from sigs_rs.observability.metrics import LatencyTracker, MetricsCollector, get_metrics
from sigs_rs.persistence.circuit_breaker import CBState
from sigs_rs.telemetry.models import RawTelemetryPacket


# ─── 1. MetricsCollector Unit Tests ──────────────────────────────────────────

class TestMetricsCollectorUnit:
    def test_latency_tracker_calculations(self):
        tracker = LatencyTracker()
        assert tracker.count == 0
        assert tracker.sum == 0.0
        assert tracker.average == 0.0
        assert tracker.last == 0.0

        # N=1, duration=2 -> sum=2, count=1, avg=2
        tracker.record(2.0, count=1)
        assert tracker.sum == 2.0
        assert tracker.count == 1
        assert tracker.average == 2.0

        tracker = LatencyTracker()
        # N=10, duration=2 -> sum=20, count=10, avg=2
        tracker.record(2.0, count=10)
        assert tracker.sum == 20.0
        assert tracker.count == 10
        assert tracker.average == 2.0

        tracker = LatencyTracker()
        # N=100, duration=10 -> sum=1000, count=100, avg=10
        tracker.record(10.0, count=100)
        assert tracker.sum == 1000.0
        assert tracker.count == 100
        assert tracker.average == 10.0

        d = tracker.to_dict()
        assert d["count"] == 100
        assert d["avg_seconds"] == 10.0

    def test_metrics_collector_counters_and_gauges(self):
        col = MetricsCollector()
        col.reset()

        col.record_packet_received(5)
        col.record_packet_valid(4)
        col.record_packet_malformed(1)
        col.record_packet_dropped_rate(2)
        col.record_packet_dropped_invalid_metrics(1)
        col.record_packet_dropped_anonymization(1)
        col.record_packet_persisted(3)

        col.record_processing_latency(0.005)
        col.record_inspector_latency(0.001)
        col.record_anonymizer_latency(0.001)
        col.record_database_latency(0.003)

        col.set_queue_size(15)
        col.set_active_sensors(8)
        col.set_mqtt_connected(True)
        col.set_circuit_breaker_state("CLOSED")

        data = col.to_dict()

        # Verify all 15 required metrics
        assert data["packets_received_total"] == 5
        assert data["packets_valid_total"] == 4
        assert data["packets_malformed_total"] == 1
        assert data["packets_dropped_rate_total"] == 2
        assert data["packets_dropped_invalid_metrics_total"] == 1
        assert data["packets_dropped_anonymization_total"] == 1
        assert data["packets_persisted_total"] == 3
        assert data["current_queue_size"] == 15
        assert data["active_sensors"] == 8
        assert data["mqtt_connection_state"] == 1
        assert data["circuit_breaker_state"] == "CLOSED"

        assert data["processing_latency"]["count"] == 1
        assert data["inspector_latency"]["count"] == 1
        assert data["anonymizer_latency"]["count"] == 1
        assert data["database_latency"]["count"] == 1

    def test_to_prometheus_text_format(self):
        col = MetricsCollector()
        col.reset()

        col.record_packet_received(100)
        col.record_packet_valid(95)
        col.record_packet_malformed(5)
        col.record_packet_dropped_rate(10)
        col.record_packet_dropped_invalid_metrics(2)
        col.record_packet_dropped_anonymization(1)
        col.record_packet_persisted(82)
        col.set_queue_size(4)
        col.set_active_sensors(25)
        col.set_mqtt_connected(True)
        col.set_circuit_breaker_state("HALF_OPEN")

        col.record_processing_latency(0.015)
        col.record_inspector_latency(0.002)
        col.record_anonymizer_latency(0.003)
        col.record_database_latency(0.010)

        prom_text = col.to_prometheus_text()

        # Check all required Prometheus HELP/TYPE declarations
        expected_metrics = [
            "sigs_packets_received_total",
            "sigs_packets_valid_total",
            "sigs_packets_malformed_total",
            "sigs_packets_dropped_rate_total",
            "sigs_packets_dropped_invalid_metrics_total",
            "sigs_packets_dropped_anonymization_total",
            "sigs_packets_persisted_total",
            "sigs_queue_size_current",
            "sigs_processing_batch_latency_seconds",
            "sigs_inspector_latency_seconds",
            "sigs_anonymizer_latency_seconds",
            "sigs_database_batch_latency_seconds",
            "sigs_mqtt_connected",
            "sigs_active_sensors",
            "sigs_circuit_breaker_state",
            "sigs_uptime_seconds",
        ]

        for metric in expected_metrics:
            assert f"# HELP {metric}" in prom_text, f"Missing HELP for {metric}"
            assert f"# TYPE {metric}" in prom_text, f"Missing TYPE for {metric}"

        # Assert specific exported values
        assert "sigs_packets_received_total 100" in prom_text
        assert "sigs_packets_valid_total 95" in prom_text
        assert "sigs_packets_malformed_total 5" in prom_text
        assert "sigs_packets_dropped_rate_total 10" in prom_text
        assert "sigs_packets_dropped_invalid_metrics_total 2" in prom_text
        assert "sigs_packets_dropped_anonymization_total 1" in prom_text
        assert "sigs_packets_persisted_total 82" in prom_text
        assert "sigs_queue_size_current 4" in prom_text
        assert "sigs_mqtt_connected 1" in prom_text
        assert "sigs_active_sensors 25" in prom_text
        assert "sigs_circuit_breaker_state 1" in prom_text  # HALF_OPEN code is 1
        assert 'sigs_circuit_breaker_state_info{state="HALF_OPEN"} 1' in prom_text

    def test_live_app_state_integration(self):
        col = MetricsCollector()
        col.reset()

        # Mock app.state with live objects
        mock_state = MagicMock()
        mock_state.queue.depth = 42
        mock_state.inspector.window.active_sensor_count.return_value = 17
        mock_state.circuit_breaker.state.name = "OPEN"
        mock_state.mqtt.is_connected = True

        data = col.to_dict(app_state=mock_state)
        assert data["current_queue_size"] == 42
        assert data["active_sensors"] == 17
        assert data["circuit_breaker_state"] == "OPEN"
        assert data["mqtt_connection_state"] == 1

        prom = col.to_prometheus_text(app_state=mock_state)
        assert "sigs_queue_size_current 42" in prom
        assert "sigs_active_sensors 17" in prom
        assert "sigs_circuit_breaker_state 2" in prom  # OPEN code is 2


# ─── 2. Structured JSON Logging & PII Redaction Tests ────────────────────────

class TestStructuredLoggingAndRedaction:
    def test_pii_redaction_removes_sensitive_data(self):
        sensitive_payload = {
            "sensor_id": "sensor-123",
            "resource_type": "water",
            "value": 45.2,
            "latitude": 12.9716,
            "longitude": 80.2209,
            "mac_address": "AA:BB:CC:DD:EE:FF",
            "password": "super-secret-password",
            "token": "jwt.bearer.secret",
            "geo": {"lat": 12.9, "lon": 80.2},
            "water_ext": {"pressure": 2.5},
            "nested": {
                "safe_field": "ok",
                "auth": "Basic 12345",
            },
            "list_items": [
                {"latitude": 10.0, "clean": "yes"},
            ],
        }

        cleaned = redact_sensitive_data(sensitive_payload)

        # Verified safe fields remain intact
        assert cleaned["sensor_id"] == "sensor-123"
        assert cleaned["resource_type"] == "water"
        assert cleaned["value"] == 45.2
        assert cleaned["nested"]["safe_field"] == "ok"
        assert cleaned["list_items"][0]["clean"] == "yes"

        # Sensitive fields strictly redacted
        assert cleaned["latitude"] == "[REDACTED]"
        assert cleaned["longitude"] == "[REDACTED]"
        assert cleaned["mac_address"] == "[REDACTED]"
        assert cleaned["password"] == "[REDACTED]"
        assert cleaned["token"] == "[REDACTED]"
        assert cleaned["geo"] == "[REDACTED]"
        assert cleaned["water_ext"] == "[REDACTED]"
        assert cleaned["nested"]["auth"] == "[REDACTED]"
        assert cleaned["list_items"][0]["latitude"] == "[REDACTED]"

    def test_json_log_formatter_enriches_and_serializes(self):
        formatter = JsonLogFormatter()
        set_correlation_id("test-corr-abc-123")
        set_packet_id("test-pkt-xyz-789")

        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=42,
            msg="Testing log output with sensor=%s",
            args=("sensor-01",),
            exc_info=None,
        )
        record.latitude = 37.7749  # Extra sensitive attribute

        output = formatter.format(record)
        parsed = json.loads(output)

        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "test.logger"
        assert parsed["message"] == "Testing log output with sensor=sensor-01"
        assert parsed["correlation_id"] == "test-corr-abc-123"
        assert parsed["packet_id"] == "test-pkt-xyz-789"
        assert "timestamp" in parsed
        assert parsed["extra"]["latitude"] == "[REDACTED]"

    def test_correlation_id_contextvars(self):
        set_correlation_id()
        cid = get_correlation_id()
        assert cid.startswith("corr-")
        assert get_correlation_id() == cid

        set_correlation_id("custom-cid-456")
        assert get_correlation_id() == "custom-cid-456"

        pid = set_packet_id()
        assert pid.startswith("pkt-")
        assert get_packet_id() == pid


# ─── 3. HTTP API Observability Endpoints Tests ───────────────────────────────

class TestEndpointsObservability:
    @pytest.mark.asyncio
    async def test_metrics_prometheus_default_and_headers(self):
        
        class DummyState:
            name = "CLOSED"
        class DummyCB:
            state = DummyState()
            is_open = False
        app = create_app()
        from unittest.mock import MagicMock
        class DummyQueue:
            def qsize(self): return 0
        app.state.queue = DummyQueue()
        app.state.circuit_breaker = DummyCB()

        class DummyQueue:
            def qsize(self): return 0
        app.state.queue = DummyQueue()
        app.state.circuit_breaker = MagicMock()
        app.state.circuit_breaker.state.name = "CLOSED"
        initial_cid = get_correlation_id()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. Default GET /metrics returns Prometheus exposition format
            resp_default = await client.get("/metrics")
            assert resp_default.status_code == 200
            assert "text/plain" in resp_default.headers["content-type"]
            assert "# HELP sigs_packets_received_total" in resp_default.text
            assert "sigs_packets_received_total" in resp_default.text

            # 2. Accept: text/plain returns Prometheus format
            resp_txt = await client.get("/metrics", headers={"Accept": "text/plain"})
            assert resp_txt.status_code == 200
            assert "text/plain" in resp_txt.headers["content-type"]
            assert "# HELP sigs_queue_size_current" in resp_txt.text

            # 3. Accept: application/openmetrics-text returns Prometheus format
            resp_om = await client.get("/metrics", headers={"Accept": "application/openmetrics-text"})
            assert resp_om.status_code == 200
            assert "# HELP sigs_processing_batch_latency_seconds" in resp_om.text

            # 4. format=prometheus query parameter
            resp_qp = await client.get("/metrics?format=prometheus")
            assert resp_qp.status_code == 200
            assert "# HELP sigs_active_sensors" in resp_qp.text

    @pytest.mark.asyncio
    async def test_metrics_json_content_negotiation(self):
        
        class DummyState:
            name = "CLOSED"
        class DummyCB:
            state = DummyState()
            is_open = False
        app = create_app()
        from unittest.mock import MagicMock
        class DummyQueue:
            def qsize(self): return 0
        app.state.queue = DummyQueue()
        app.state.circuit_breaker = DummyCB()

        class DummyQueue:
            def qsize(self): return 0
        app.state.queue = DummyQueue()
        app.state.circuit_breaker = MagicMock()
        app.state.circuit_breaker.state.name = "CLOSED"
        initial_cid = get_correlation_id()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. Accept: application/json
            resp_json = await client.get("/metrics", headers={"Accept": "application/json"})
            assert resp_json.status_code == 200
            assert "application/json" in resp_json.headers["content-type"]
            data = resp_json.json()

            # Verify presence of all required metrics
            assert "packets_received_total" in data
            assert "packets_valid_total" in data
            assert "packets_malformed_total" in data
            assert "packets_dropped_rate_total" in data
            assert "packets_dropped_invalid_metrics_total" in data
            assert "packets_dropped_anonymization_total" in data
            assert "packets_persisted_total" in data
            assert "current_queue_size" in data
            assert "processing_latency" in data
            assert "inspector_latency" in data
            assert "anonymizer_latency" in data
            assert "database_latency" in data
            assert "mqtt_connection_state" in data
            assert "active_sensors" in data
            assert "circuit_breaker_state" in data

            # Verify backward compatibility fields
            assert "packets_ingested_total" in data
            assert "packets_dropped_total" in data
            assert "packets_sanitized_total" in data
            assert "queue_depth" in data
            assert "uptime_seconds" in data

            # 2. format=json query parameter
            resp_qp = await client.get("/metrics?format=json")
            assert resp_qp.status_code == 200
            data_qp = resp_qp.json()
            assert "packets_received_total" in data_qp

    @pytest.mark.asyncio
    async def test_correlation_id_middleware_header_propagation(self):
        
        class DummyState:
            name = "CLOSED"
        class DummyCB:
            state = DummyState()
            is_open = False
        app = create_app()
        from unittest.mock import MagicMock
        class DummyQueue:
            def qsize(self): return 0
        app.state.queue = DummyQueue()
        app.state.circuit_breaker = DummyCB()

        class DummyQueue:
            def qsize(self): return 0
        app.state.queue = DummyQueue()
        app.state.circuit_breaker = MagicMock()
        app.state.circuit_breaker.state.name = "CLOSED"
        initial_cid = get_correlation_id()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # When caller sends valid X-Correlation-ID, server preserves and reflects it
            custom_id = "test-corr-custom-header-99"
            resp = await client.get("/health", headers={"X-Correlation-ID": custom_id})
            assert resp.status_code == 200
            assert resp.headers["X-Correlation-ID"] == custom_id

            # When caller doesn't send header, server generates and attaches one
            resp_gen = await client.get("/health")
            assert resp_gen.status_code == 200
            assert "X-Correlation-ID" in resp_gen.headers
            assert resp_gen.headers["X-Correlation-ID"].startswith("req-")
            
            # When caller sends invalid ID, server rejects and generates
            resp_invalid = await client.get("/health", headers={"X-Correlation-ID": "invalid!@#"})
            assert resp_invalid.headers["X-Correlation-ID"].startswith("req-")
            
            # When caller sends overly long ID, server rejects and generates
            resp_long = await client.get("/health", headers={"X-Correlation-ID": "a" * 65})
            assert resp_long.headers["X-Correlation-ID"].startswith("req-")

    @pytest.mark.asyncio
    async def test_health_and_readiness_probe_matrix(self):
        
        class DummyState:
            name = "CLOSED"
        class DummyCB:
            state = DummyState()
            is_open = False
        app = create_app()
        from unittest.mock import MagicMock
        class DummyQueue:
            def qsize(self): return 0
        app.state.queue = DummyQueue()
        app.state.circuit_breaker = DummyCB()

        class DummyQueue:
            def qsize(self): return 0
        app.state.queue = DummyQueue()
        app.state.circuit_breaker = MagicMock()
        app.state.circuit_breaker.state.name = "CLOSED"
        initial_cid = get_correlation_id()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. Healthy
            h_resp = await client.get("/health")
            assert h_resp.status_code == 200
            assert h_resp.json()["status"] in ("ok", "degraded")

            r_resp = await client.get("/ready")
            assert r_resp.status_code == 200
            assert r_resp.json()["status"] in ("ok", "degraded")

            # 2. Circuit Breaker OPEN -> Degraded
            cb = getattr(app.state, "circuit_breaker", None)
            if cb:
                cb._state = CBState.OPEN
                try:
                    h_deg = await client.get("/health")
                    assert h_deg.status_code == 200
                    assert h_deg.json()["status"] == "degraded"
                    assert h_deg.json()["components"]["circuit_breaker"] == "open"
                finally:
                    cb._state = CBState.CLOSED

            # 3. Fail-Closed state -> 503 Unhealthy (SRS REQ-QA-1)
            app.state.fail_closed = True
            try:
                h_unh = await client.get("/health")
                assert h_unh.status_code == 503
                assert h_unh.json()["status"] == "unhealthy"

                r_unh = await client.get("/ready")
                assert r_unh.status_code == 503
                assert r_unh.json()["status"] == "unhealthy"
            finally:
                app.state.fail_closed = False


# ─── 4. Pipeline & Ingestion Metrics Integration ─────────────────────────────

class TestPipelineMetricsIntegration:
    @pytest.mark.asyncio
    async def test_pipeline_tracks_latencies_and_drops(self):
        metrics = MetricsCollector()
        metrics.reset()

        queue = IngestionQueue(max_size=100)
        inspector = MagicMock()
        anonymizer = MagicMock()
        
        async def fake_save(batch): pass
        repo = MagicMock()
        repo.save_batch = fake_save
        async def fake_insert(*args, **kwargs): pass
        repo.insert = fake_insert
        
        async def fake_cb_call(coro): return await coro
        cb = MagicMock()
        cb.call = fake_cb_call
        
        async def fake_audit(*args, **kwargs): pass
        audit = MagicMock()
        audit.log = fake_audit

        pipeline = MiddlewarePipeline(
            queue=queue,
            inspector=inspector,
            anonymizer=anonymizer,
            repository=repo,
            circuit_breaker=cb,
            audit_logger=audit,
            metrics=metrics,
        )

        pkt1 = RawTelemetryPacket(
            sensor_id="s1",
            timestamp="2026-07-17T10:30:00Z",
            resource_type="water",
            value=25.0,
            unit="liters_per_min",
        )
        pkt2 = RawTelemetryPacket(
            sensor_id="s2",
            timestamp="2026-07-17T10:30:00Z",
            resource_type="energy",
            value=-10.0,
            unit="kWh",
        )
        pkt3 = RawTelemetryPacket(
            sensor_id="s3",
            timestamp="2026-07-17T10:30:00Z",
            resource_type="water",
            value=30.0,
            unit="liters_per_min",
        )

        # Mock behaviors:
        # pkt1 passes all stages
        # pkt2 fails inspector with INVALID_METRIC
        # pkt3 fails anonymizer with AnonymizerError
        def inspect_mock(packet):
            if packet.sensor_id == "s2":
                return VerificationResult.INVALID_METRIC
            return VerificationResult.VERIFIED

        inspector.inspect.side_effect = inspect_mock

        def anonymize_mock(packet):
            if packet.sensor_id == "s3":
                raise AnonymizerError("anonymization masking failed")
            return packet

        anonymizer.anonymize.side_effect = anonymize_mock

        # Process the batch
        batch = [pkt1, pkt2, pkt3]
        import pytest
        try:
            await pipeline.process_batch(batch)
        except RuntimeError:
            pass

        # Assert metrics recorded
        assert metrics._packets_dropped_invalid_metrics == 1
        assert metrics._packets_dropped_anonymization == 1
        assert metrics._packets_persisted == 0
        assert metrics.inspector_latency.count == 3
        assert metrics.anonymizer_latency.count == 2
        assert metrics.database_latency.count == 0
        assert metrics.processing_latency.count == 0

# ─── 5. Added Phase 10 Audit Tests ──────────────────────────────────────────

class TestAuditFindingsMetrics:

    @pytest.mark.asyncio
    async def test_oversized_payload_metrics_with_content_length(self):
        app = create_app()
        metrics = get_metrics()
        metrics.reset()
        initial_cid = get_correlation_id()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            huge_payload = b"a" * (512 * 1024 + 10)
            resp = await client.post("/api/v1/telemetry/batch", content=huge_payload, headers={"Content-Type": "application/json"})
            assert resp.status_code == 413
            data = metrics.to_dict(app_state=app.state)
            assert data["packets_received_total"] == 1
            assert data["packets_malformed_total"] == 1

    @pytest.mark.asyncio
    async def test_oversized_payload_metrics_without_content_length(self):
        app = create_app()
        metrics = get_metrics()
        metrics.reset()
        initial_cid = get_correlation_id()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # We use an async generator to force chunked encoding, bypassing Content-Length
            async def chunked_body():
                yield b"a" * 256 * 1024
                yield b"a" * 256 * 1024
                yield b"a" * 10
            resp = await client.post("/api/v1/telemetry/batch", content=chunked_body(), headers={"Content-Type": "application/json"})
            assert resp.status_code == 413
            data = metrics.to_dict(app_state=app.state)
            assert data["packets_received_total"] == 1
            assert data["packets_malformed_total"] == 1

    @pytest.mark.asyncio
    async def test_request_validation_error_metrics(self):
        
        class DummyState:
            name = "CLOSED"
        class DummyCB:
            state = DummyState()
            is_open = False
        app = create_app()
        from unittest.mock import MagicMock
        class DummyQueue:
            def qsize(self): return 0
        app.state.queue = DummyQueue()
        app.state.circuit_breaker = DummyCB()

        class DummyQueue:
            def qsize(self): return 0
        app.state.queue = DummyQueue()
        app.state.circuit_breaker = MagicMock()
        app.state.circuit_breaker.state.name = "CLOSED"
        metrics = get_metrics()
        metrics.reset()
        initial_cid = get_correlation_id()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Send invalid JSON schema
            resp = await client.post("/api/v1/telemetry", json={"missing": "everything"})
            assert resp.status_code == 422
            data = metrics.to_dict(app_state=app.state)
            assert data["packets_received_total"] == 1
            assert data["packets_malformed_total"] == 1

    @pytest.mark.asyncio
    async def test_oversized_payload_metrics(self):
        
        class DummyState:
            name = "CLOSED"
        class DummyCB:
            state = DummyState()
            is_open = False
        app = create_app()
        from unittest.mock import MagicMock
        class DummyQueue:
            def qsize(self): return 0
        app.state.queue = DummyQueue()
        app.state.circuit_breaker = DummyCB()

        class DummyQueue:
            def qsize(self): return 0
        app.state.queue = DummyQueue()
        app.state.circuit_breaker = MagicMock()
        app.state.circuit_breaker.state.name = "CLOSED"
        metrics = get_metrics()
        metrics.reset()
        initial_cid = get_correlation_id()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            huge_payload = "a" * (512 * 1024 + 10)
            resp = await client.post("/api/v1/telemetry", content=huge_payload, headers={"Content-Type": "application/json"})
            assert resp.status_code == 413
            data = metrics.to_dict(app_state=app.state)
            assert data["packets_received_total"] == 1
            assert data["packets_malformed_total"] == 1

class TestAuditFindingsLogging:
    def test_pii_redaction_in_strings(self):
        # Testing string scrubbing
        from sigs_rs.observability.logging import redact_sensitive_data
        
        # Test 1: Exception string containing raw payload
        exc_str = "MalformedPayloadError: input_value={'latitude': 123.45, 'mac': '00:11:22:33:44:55'}"
        scrubbed_exc = redact_sensitive_data(exc_str)
        assert "123.45" not in scrubbed_exc
        assert "00:11:22:33:44:55" not in scrubbed_exc
        assert "[REDACTED]" in scrubbed_exc
        
        # Test 2: JWT in string
        jwt_str = "Found Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c here"
        scrubbed_jwt = redact_sensitive_data(jwt_str)
        assert "eyJhbGci" not in scrubbed_jwt
        assert "[REDACTED]" in scrubbed_jwt

        # Test 3: Immutability
        orig = {"password": "secret_password"}
        orig_copy = orig.copy()
        res = redact_sensitive_data(orig)
        assert res["password"] == "[REDACTED]"
        assert orig == orig_copy

class TestAuditFindingsMiddleware:

    @pytest.mark.asyncio
    async def test_correlation_id_reset_after_request(self):
        app = create_app()
        from sigs_rs.observability.logging import get_correlation_id
        
        @app.get("/test_cid")
        def test_cid():
            return {"cid": get_correlation_id()}
            
        initial_cid = get_correlation_id()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/test_cid", headers={"X-Correlation-ID": "test-cid-123"})
            assert resp.json()["cid"] == "test-cid-123"
            assert get_correlation_id() == initial_cid

    @pytest.mark.asyncio
    async def test_correlation_id_validation(self):
        
        class DummyState:
            name = "CLOSED"
        class DummyCB:
            state = DummyState()
            is_open = False
        app = create_app()
        from unittest.mock import MagicMock
        class DummyQueue:
            def qsize(self): return 0
        app.state.queue = DummyQueue()
        app.state.circuit_breaker = DummyCB()

        class DummyQueue:
            def qsize(self): return 0
        app.state.queue = DummyQueue()
        app.state.circuit_breaker = MagicMock()
        app.state.circuit_breaker.state.name = "CLOSED"
        initial_cid = get_correlation_id()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Valid
            r1 = await client.get("/health", headers={"X-Correlation-ID": "valid-id-123"})
            assert r1.headers["X-Correlation-ID"] == "valid-id-123"
            
            # Invalid chars
            r2 = await client.get("/health", headers={"X-Correlation-ID": "invalid<script>"})
            assert r2.headers["X-Correlation-ID"] != "invalid<script>"
            assert r2.headers["X-Correlation-ID"].startswith("req-")
            
            # Too long
            long_id = "a" * 70
            r3 = await client.get("/health", headers={"X-Correlation-ID": long_id})
            assert r3.headers["X-Correlation-ID"] != long_id
            assert r3.headers["X-Correlation-ID"].startswith("req-")

def test_json_nested_redaction():
    from sigs_rs.observability.logging import scrub_string
    import json
    
    # 1. Arrays
    assert scrub_string('{"latitude":[12.34]}') == '{"latitude": "[REDACTED]"}'
    
    # 2. Objects
    assert scrub_string('{"longitude":{"value":80.12}}') == '{"longitude": "[REDACTED]"}'
    
    # 3. device_id array
    assert scrub_string('{"device_id":["SECRET"]}') == '{"device_id": "[REDACTED]"}'
    
    # 4. Multiple mixed
    input_str = '{"payload": {"coords": {"latitude": 1.2, "longitude": 2.3}}, "password": "123"}'
    out = scrub_string(input_str)
    parsed = json.loads(out)
    assert parsed["payload"] == "[REDACTED]"
    assert parsed["password"] == "[REDACTED]"
    
    # 5. Deeply nested array of objects
    assert scrub_string('{"payload":[{"location":{"latitude":12.34}}]}') == '{"payload": "[REDACTED]"}'
    
    # 6. Malformed JSON
    assert scrub_string('{"latitude": 1.2, malformed}') == '{"latitude": [REDACTED], malformed}'
    
    # 7. Ordinary non-sensitive strings
    assert scrub_string('{"message": "ordinary non-sensitive string"}') == '{"message": "ordinary non-sensitive string"}'
    
    # 8. Exception messages with JSON
    assert scrub_string('Error: input_value={"latitude": 1.2}') == 'Error: input_value=[REDACTED]'
    
    # 9. Pydantic ValidationError containing raw telemetry
    assert scrub_string('type=type_error.dict; input_value={"coords": {"lat": 1.2}}') == 'type=type_error.dict; input_value=[REDACTED]'
    
    # 10. Authorization/Bearer token
    assert scrub_string('Headers: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c') == 'Headers: [REDACTED]'

def test_json_multiline_redaction():
    from sigs_rs.observability.logging import scrub_string
    import json
    
    # multiline device_id arrays
    multiline_device = '{\n  "device_id": [\n    "SECRET1",\n    "SECRET2"\n  ]\n}'
    assert scrub_string(multiline_device) == '{"device_id": "[REDACTED]"}'
    
    # multiline latitude/longitude arrays
    multiline_lat = '{\n  "payload": {\n    "latitude": [\n      1.2,\n      2.3\n    ]\n  }\n}'
    assert scrub_string(multiline_lat) == '{"payload": "[REDACTED]"}'
    
    # nested multiline payloads
    nested_multiline = '{\n  "payload": {\n    "nested": {\n      "device_id": [\n        "A",\n        "B"\n      ]\n    }\n  }\n}'
    assert scrub_string(nested_multiline) == '{"payload": "[REDACTED]"}'
    
    # multiline password/secret
    multiline_pwd = '{\n  "password": "this\\nis\\nsecret"\n}'
    assert scrub_string(multiline_pwd) == '{"password": "[REDACTED]"}'
    
    # malformed/truncated multiline JSON
    malformed_multiline = 'Exception: {\n  "device_id": [\n    "SECRET1"'
    assert scrub_string(malformed_multiline) == 'Exception: {\n  "device_id": [REDACTED]'
    
    # Pydantic multiline exception text
    pydantic_err = '1 validation error for Model\ndevice_id\n  input_value=[\n  "SECRET"\n]\n  type=value_error'
    assert scrub_string(pydantic_err) == '1 validation error for Model\ndevice_id\n  input_value=[REDACTED]\n  type=value_error'
    
    # ordinary multiline non-sensitive messages
    ordinary_msg = 'Exception: {\n  "status": "ok",\n  "count": 5\n}'
    assert scrub_string(ordinary_msg) == 'Exception: {\n  "status": "ok",\n  "count": 5\n}'
