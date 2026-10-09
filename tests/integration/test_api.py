"""Integration tests for the HTTP API endpoints (Phase 7)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from fastapi import FastAPI

from sigs_rs.auth.jwt_handler import create_access_token
from sigs_rs.common.constants import AuditEvent, Role
from sigs_rs.common.exceptions import QueueFullError
from sigs_rs.persistence.circuit_breaker import CBState

# Ensure test env before app import
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/sigs_test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-api-tests")
os.environ.setdefault("ADMIN_USERNAME", "testadmin")
os.environ.setdefault("ADMIN_PASSWORD", "testpassword")
os.environ.setdefault("MQTT_USE_TLS", "false")
os.environ.setdefault("MQTT_BROKER_PORT", "18883")


def make_token(role: str, sub: str = "testuser") -> str:
    """Generate a valid signed JWT for a given role and subject."""
    return create_access_token(
        sub=sub,
        role=role,
        secret=os.environ.get("JWT_SECRET_KEY", "test-secret-for-api-tests"),
        algorithm="HS256",
        expires_minutes=60,
    )


@pytest.fixture
def auth_token() -> str:
    """Obtain a valid JWT for the security engineer."""
    return make_token(Role.SECURITY_ENGINEER, "testadmin")


@pytest.fixture
def qa_token() -> str:
    """Obtain a valid JWT for a QA tester."""
    return make_token(Role.QA_TESTER, "qa_tester_user")


@pytest.fixture
def dev_token() -> str:
    """Obtain a valid JWT for a developer."""
    return make_token(Role.BACKEND_DEVELOPER, "developer_user")


@pytest.fixture
def guest_token() -> str:
    """Obtain a valid JWT for an unauthorized role."""
    return make_token("guest", "guest_user")


# ─── Health & Readiness Endpoints ─────────────────────────────────────────────

class TestHealthEndpoints:
    async def test_health_ok(self, client: AsyncClient):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "degraded", "unhealthy")
        assert "version" in data
        assert "components" in data

    async def test_ready_ok(self, client: AsyncClient):
        resp = await client.get("/ready")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("ok", "degraded")

    async def test_health_fail_closed_returns_503(self, client: AsyncClient, app: FastAPI):
        """When the gateway is in fail-closed state, /health must return HTTP 503 (SRS REQ-QA-1)."""
        app.state.fail_closed = True
        try:
            resp = await client.get("/health")
            assert resp.status_code == 503
            data = resp.json()
            assert data["status"] == "unhealthy"
            assert data["components"]["pipeline"] == "fail_closed"
        finally:
            app.state.fail_closed = False

    async def test_ready_fail_closed_returns_503(self, client: AsyncClient, app: FastAPI):
        """When fail-closed, /ready probe must return 503 to signal orchestrator not to route traffic."""
        app.state.fail_closed = True
        try:
            resp = await client.get("/ready")
            assert resp.status_code == 503
            data = resp.json()
            assert data["status"] == "unhealthy"
        finally:
            app.state.fail_closed = False

    async def test_health_circuit_breaker_open_degraded(self, client: AsyncClient, app: FastAPI):
        """When circuit breaker is open, /health returns HTTP 200 with degraded status."""
        cb = getattr(app.state, "circuit_breaker", None)
        if cb is not None:
            cb._state = CBState.OPEN
            try:
                resp = await client.get("/health")
                assert resp.status_code == 200
                data = resp.json()
                assert data["status"] == "degraded"
                assert data["components"]["circuit_breaker"] == "open"
            finally:
                cb._state = CBState.CLOSED


# ─── Metrics Endpoints ────────────────────────────────────────────────────────

class TestMetricsEndpoints:
    async def test_metrics_ok(self, client: AsyncClient):
        # Prometheus format (default)
        resp = await client.get("/metrics")
        assert resp.status_code == 200
        assert "sigs_packets_received_total" in resp.text
        assert "# HELP" in resp.text

        # JSON format
        resp_json = await client.get("/metrics", headers={"Accept": "application/json"})
        assert resp_json.status_code == 200
        data = resp_json.json()
        assert "packets_ingested_total" in data
        assert "packets_dropped_total" in data
        assert "packets_sanitized_total" in data
        assert "queue_depth" in data
        assert "circuit_breaker_state" in data
        assert "uptime_seconds" in data
        assert data["uptime_seconds"] >= 0
        assert isinstance(data["packets_ingested_total"], int)


# ─── Auth Endpoints ───────────────────────────────────────────────────────────

class TestAuthEndpoints:
    async def test_issue_token_valid_credentials(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/auth/token",
            json={"username": "testadmin", "password": "testpassword"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["expires_in"] > 0

    async def test_issue_token_wrong_password(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/auth/token",
            json={"username": "testadmin", "password": "wrongpassword"},
        )
        assert resp.status_code == 401

    async def test_issue_token_unknown_user(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/auth/token",
            json={"username": "nobody", "password": "anything"},
        )
        assert resp.status_code == 401


# ─── Telemetry Ingestion Endpoints ─────────────────────────────────────────────

class TestTelemetryIngestion:
    async def test_ingest_valid_packet(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/telemetry",
            json={
                "sensor_id": "sensor-001",
                "timestamp": "2026-07-17T10:30:00Z",
                "resource_type": "water",
                "value": 42.7,
                "unit": "liters_per_min",
            },
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["accepted"] is True
        assert "accepted for processing" in data["message"].lower()

    async def test_ingest_all_resource_types(self, client: AsyncClient):
        """Test water, energy, and air_quality packets are accepted."""
        cases = [
            {
                "sensor_id": "water-node-01",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "resource_type": "water",
                "value": 15.2,
                "unit": "L/min",
            },
            {
                "sensor_id": "energy-meter-01",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "resource_type": "energy",
                "value": 3.45,
                "unit": "kWh",
            },
            {
                "sensor_id": "aq-sensor-01",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "resource_type": "air_quality",
                "value": 45.0,
                "unit": "AQI",
            },
        ]
        for payload in cases:
            resp = await client.post("/api/v1/telemetry", json=payload)
            assert resp.status_code == 202, f"Failed for payload: {payload}"

    async def test_ingest_invalid_resource_type(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/telemetry",
            json={
                "sensor_id": "sensor-001",
                "timestamp": "2026-07-17T10:30:00Z",
                "resource_type": "invalid_type",
                "value": 42.7,
                "unit": "liters_per_min",
            },
        )
        assert resp.status_code == 422

    async def test_ingest_missing_fields(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/telemetry",
            json={"sensor_id": "sensor-001"},
        )
        assert resp.status_code == 422

    async def test_ingest_payload_too_large_header(self, client: AsyncClient):
        """SRS REQ-TI-3: Payloads > 512 KB rejected with HTTP 413."""
        resp = await client.post(
            "/api/v1/telemetry",
            headers={"Content-Length": "600000"},
            content=b'{"some":"data"}',
        )
        assert resp.status_code == 413

    async def test_ingest_payload_too_large_body(self, client: AsyncClient):
        """Reject request body exceeding 512 KB."""
        large_comment = "x" * (524_288 + 100)
        oversized = {
            "sensor_id": "sensor-001",
            "timestamp": "2026-07-17T10:30:00Z",
            "resource_type": "water",
            "value": 42.7,
            "unit": "liters_per_min",
            "comment": large_comment,
        }
        resp = await client.post("/api/v1/telemetry", json=oversized)
        assert resp.status_code == 413

    async def test_ingest_batch_valid(self, client: AsyncClient):
        batch = {
            "packets": [
                {
                    "sensor_id": f"sensor-b-{i}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "resource_type": "water",
                    "value": float(i + 10),
                    "unit": "liters_per_min",
                }
                for i in range(5)
            ]
        }
        resp = await client.post("/api/v1/telemetry/batch", json=batch)
        assert resp.status_code == 202
        data = resp.json()
        assert data["accepted"] == 5
        assert data["rejected"] == 0

    async def test_ingest_batch_oversized(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/telemetry/batch",
            headers={"Content-Length": "600000"},
            content=b'{"packets":[]}',
        )
        assert resp.status_code == 413

    async def test_ingest_queue_full_returns_503(self, client: AsyncClient, app: FastAPI):
        """When queue is at full capacity, returns HTTP 503."""
        queue = app.state.queue
        with patch.object(queue, "put", side_effect=QueueFullError("Queue full")):
            resp = await client.post(
                "/api/v1/telemetry",
                json={
                    "sensor_id": "sensor-full-01",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "resource_type": "water",
                    "value": 12.0,
                    "unit": "liters_per_min",
                },
            )
            assert resp.status_code == 503

    async def test_ingest_pipeline_e2e_persistence_and_sanitization(
        self, client: AsyncClient, app: FastAPI
    ):
        """
        End-to-end processing test:
        1. Ingest telemetry containing PII (geo latitude/longitude, mac_address).
        2. Pipeline dequeues, inspects, sanitizes, and persists to DB.
        3. Verify the reading in persistence has NO PII.
        """
        sensor_id = "sensor-e2e-pii"
        payload = {
            "sensor_id": sensor_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "resource_type": "water",
            "value": 25.5,
            "unit": "liters_per_min",
            "geo": {"latitude": 37.7749, "longitude": -122.4194},
            "mac_address": "AA:BB:CC:DD:EE:FF",
        }
        resp = await client.post("/api/v1/telemetry", json=payload)
        assert resp.status_code == 202

        # Deterministically wait for background pipeline worker to process queue
        await asyncio.wait_for(app.state.queue._q.join(), timeout=2.0)

        repo = getattr(app.state, "repository", None)
        assert repo is not None
        readings = await repo.get_by_sensor(sensor_id, limit=5)
        assert len(readings) >= 1
        saved = readings[-1]

        # Verify reading persisted
        assert saved["sensor_id"] == sensor_id
        assert saved["resource_type"] == "water"
        assert saved["value"] == 25.5

        # Critical security guarantee: PII must NEVER reach persistence
        assert "latitude" not in saved
        assert "longitude" not in saved
        assert "mac_address" not in saved
        assert "geo" not in saved

    async def test_ingest_impossible_value_dropped_not_persisted(
        self, client: AsyncClient, app: FastAPI
    ):
        """
        Impossible value (negative power consumption) must be:
        1. Accepted at HTTP gateway.
        2. Dropped by the Anomalous Packet Inspector.
        3. NEVER reach persistence.
        """
        sensor_id = "sensor-impossible-01"
        payload = {
            "sensor_id": sensor_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "resource_type": "energy",
            "value": -50.0,  # Impossible: negative consumption
            "unit": "kWh",
        }
        resp = await client.post("/api/v1/telemetry", json=payload)
        assert resp.status_code == 202

        await asyncio.wait_for(app.state.queue._q.join(), timeout=2.0)

        # Verify Inspector dropped packet
        inspector = app.state.inspector
        assert inspector.stats["dropped_impossible"] >= 1

        # Verify NOT persisted
        repo = getattr(app.state, "repository", None)
        assert repo is not None
        readings = await repo.get_by_sensor(sensor_id, limit=5)
        assert len(readings) == 0


# ─── Security Configuration Endpoints ──────────────────────────────────────────

class TestConfigEndpoints:
    async def test_get_inspector_config_requires_auth(self, client: AsyncClient):
        resp = await client.get("/api/v1/config/inspector")
        assert resp.status_code == 401

    async def test_get_inspector_config_authenticated(
        self, client: AsyncClient, auth_token: str
    ):
        resp = await client.get(
            "/api/v1/config/inspector",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "window_seconds" in data
        assert "max_packets_per_window" in data
        assert "cleanup_interval" in data

    async def test_get_inspector_config_accessible_by_qa_and_dev(
        self, client: AsyncClient, qa_token: str, dev_token: str
    ):
        """QA testers and Developers have read-only access to inspector config."""
        for token in (qa_token, dev_token):
            resp = await client.get(
                "/api/v1/config/inspector",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200

    async def test_put_inspector_config_requires_auth(self, client: AsyncClient):
        resp = await client.put(
            "/api/v1/config/inspector",
            json={"window_seconds": 15, "max_packets_per_window": 60, "cleanup_interval": 60},
        )
        assert resp.status_code == 401

    async def test_put_inspector_config_forbidden_for_qa_tester(
        self, client: AsyncClient, qa_token: str
    ):
        """SRS BR-1: Only Security Engineers may modify thresholds. QA Tester gets 403."""
        resp = await client.put(
            "/api/v1/config/inspector",
            headers={"Authorization": f"Bearer {qa_token}"},
            json={"window_seconds": 15, "max_packets_per_window": 60, "cleanup_interval": 60},
        )
        assert resp.status_code == 403

    async def test_put_inspector_config_forbidden_for_developer(
        self, client: AsyncClient, dev_token: str
    ):
        """SRS BR-1: Developer gets 403 on threshold modification."""
        resp = await client.put(
            "/api/v1/config/inspector",
            headers={"Authorization": f"Bearer {dev_token}"},
            json={"window_seconds": 15, "max_packets_per_window": 60, "cleanup_interval": 60},
        )
        assert resp.status_code == 403

    async def test_put_inspector_config_success_security_engineer(
        self, client: AsyncClient, auth_token: str, app: FastAPI
    ):
        """Security Engineer can successfully update thresholds dynamically."""
        new_config = {
            "window_seconds": 15,
            "max_packets_per_window": 75,
            "cleanup_interval": 120,
        }
        resp = await client.put(
            "/api/v1/config/inspector",
            headers={"Authorization": f"Bearer {auth_token}"},
            json=new_config,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["updated"] is True
        assert data["updated_by"] == "testadmin"

        # Verify dynamic update in memory
        inspector = app.state.inspector
        assert inspector._window.window_seconds == 15
        assert inspector._window.max_packets == 75

        # Verify DB persistence
        repo = getattr(app.state, "repository", None)
        if repo and hasattr(repo, "get_setting"):
            stored = await repo.get_setting("inspector.thresholds")
            assert stored is not None
            assert stored["window_seconds"] == 15
            assert stored["max_packets_per_window"] == 75

    async def test_put_inspector_config_validation(
        self, client: AsyncClient, auth_token: str
    ):
        """Invalid threshold parameters return HTTP 422."""
        invalid_config = {
            "window_seconds": 0,  # ge=1 violated
            "max_packets_per_window": 50,
            "cleanup_interval": 60,
        }
        resp = await client.put(
            "/api/v1/config/inspector",
            headers={"Authorization": f"Bearer {auth_token}"},
            json=invalid_config,
        )
        assert resp.status_code == 422

    async def test_get_anonymizer_config_authenticated(
        self, client: AsyncClient, auth_token: str
    ):
        resp = await client.get(
            "/api/v1/config/anonymizer",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "pii_fields" in data
        assert isinstance(data["pii_fields"], list)
        assert "latitude" in data["pii_fields"]

    async def test_put_anonymizer_config_forbidden_for_qa(
        self, client: AsyncClient, qa_token: str
    ):
        resp = await client.put(
            "/api/v1/config/anonymizer",
            headers={"Authorization": f"Bearer {qa_token}"},
            json={"pii_fields": ["latitude", "longitude", "custom_pii"]},
        )
        assert resp.status_code == 403

    async def test_put_anonymizer_config_success_security_engineer(
        self, client: AsyncClient, auth_token: str, app: FastAPI
    ):
        new_fields = ["latitude", "longitude", "mac_address", "custom_sensor_sn"]
        resp = await client.put(
            "/api/v1/config/anonymizer",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={"pii_fields": new_fields},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["updated"] is True

        # Verify dynamic update in memory
        anonymizer = app.state.anonymizer
        assert "custom_sensor_sn" in anonymizer._pii_fields

    async def test_get_middleware_status_requires_auth(self, client: AsyncClient):
        resp = await client.get("/api/v1/config/status")
        assert resp.status_code == 401

    async def test_get_middleware_status_authenticated(
        self, client: AsyncClient, auth_token: str
    ):
        resp = await client.get(
            "/api/v1/config/status",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "environment" in data
        assert "inspector" in data
        assert "anonymizer" in data
        assert "circuit_breaker" in data
        assert "pipeline" in data
        assert "queue" in data


# ─── Audit Log Query Endpoints ────────────────────────────────────────────────

class TestAuditLogEndpoints:
    async def test_get_audit_logs_requires_auth(self, client: AsyncClient):
        resp = await client.get("/api/v1/audit/logs")
        assert resp.status_code == 401

    async def test_get_audit_logs_authenticated(
        self, client: AsyncClient, auth_token: str
    ):
        resp = await client.get(
            "/api/v1/audit/logs",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "entries" in data
        assert "total" in data
        assert "page" in data
        assert "page_size" in data

    async def test_get_audit_logs_forbidden_for_guest(
        self, client: AsyncClient, guest_token: str
    ):
        resp = await client.get(
            "/api/v1/audit/logs",
            headers={"Authorization": f"Bearer {guest_token}"},
        )
        assert resp.status_code == 403

    async def test_get_audit_logs_accessible_by_qa_tester(
        self, client: AsyncClient, qa_token: str
    ):
        resp = await client.get(
            "/api/v1/audit/logs",
            headers={"Authorization": f"Bearer {qa_token}"},
        )
        assert resp.status_code == 200

    async def test_get_audit_logs_pagination_and_filters(
        self, client: AsyncClient, auth_token: str, app: FastAPI
    ):
        # Create test audit records
        audit = app.state.audit_logger
        for i in range(5):
            await audit.log(
                event_type="TEST_EVENT",
                severity="INFO",
                sensor_id="sensor-filter-01",
                detail={"index": i},
            )

        resp = await client.get(
            "/api/v1/audit/logs?page=1&page_size=2&event_type=TEST_EVENT&sensor_id=sensor-filter-01",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["page"] == 1
        assert data["page_size"] == 2
        assert len(data["entries"]) <= 2

    async def test_get_audit_logs_pii_strictly_redacted(
        self, client: AsyncClient, auth_token: str, app: FastAPI
    ):
        """
        Security requirement: No raw PII should be returned in audit logs.
        Any accidentally captured PII fields in audit details must be redacted to '[REDACTED]'.
        """
        audit = app.state.audit_logger
        await audit.log(
            event_type="SECURITY_ALERT",
            severity="WARN",
            sensor_id="sensor-pii-audit",
            detail={
                "reason": "packet inspection detail",
                "latitude": 37.7749,
                "longitude": -122.4194,
                "mac_address": "00:11:22:33:44:55",
                "device_id": "dev-999",
                "nested": {
                    "geo": {"lat": 37.77, "lon": -122.41},
                    "safe_field": "ok_value",
                },
            },
        )

        resp = await client.get(
            "/api/v1/audit/logs?sensor_id=sensor-pii-audit",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["entries"]) >= 1

        entry = data["entries"][0]
        detail = entry["detail"]

        # Check top-level PII redacted
        assert detail["latitude"] == "[REDACTED]"
        assert detail["longitude"] == "[REDACTED]"
        assert detail["mac_address"] == "[REDACTED]"
        assert detail["device_id"] == "[REDACTED]"
        assert detail["reason"] == "packet inspection detail"

        # Check nested PII redacted
        assert detail["nested"]["geo"] == "[REDACTED]"
        assert detail["nested"]["safe_field"] == "ok_value"


# ─── OpenAPI Documentation ────────────────────────────────────────────────────

class TestOpenApiSpec:
    async def test_openapi_json_available(self, client: AsyncClient):
        """OpenAPI JSON spec is served and documents all Phase 7 routes."""
        resp = await client.get("/openapi.json")
        assert resp.status_code == 200
        spec = resp.json()
        assert "paths" in spec
        paths = spec["paths"]

        # Assert key Phase 7 endpoints documented
        assert "/health" in paths
        assert "/ready" in paths
        assert "/metrics" in paths
        assert "/api/v1/telemetry" in paths
        assert "/api/v1/telemetry/batch" in paths
        assert "/api/v1/config/inspector" in paths
        assert "/api/v1/config/anonymizer" in paths
        assert "/api/v1/config/status" in paths
        assert "/api/v1/audit/logs" in paths
        assert "/api/v1/auth/token" in paths
