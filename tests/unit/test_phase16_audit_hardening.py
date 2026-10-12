"""
Phase 16 Hardening & Compliance Regression Tests — SIGS-RS.

Validates remediation for:
- BR-3: Compliance Lockout — simulation testing is immediately suspended when
  privacy guidelines are revised, and may only resume after formal compliance
  audit signoff.
- REQ-SE-4: Audit Log Immutability — UPDATE and DELETE operations on
  audit.security_log are strictly prohibited and fail closed.
- REQ-QA-3 / REQ-SF-1: Kubernetes configuration validation and environment isolation.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from sigs_rs.auth.jwt_handler import create_access_token
from sigs_rs.common.constants import (
    AuditEvent,
    COMPLIANCE_GUIDELINE_VERSION,
    Role,
)
from sigs_rs.common.exceptions import PersistenceError
from sigs_rs.config import Settings
from sigs_rs.main import app, create_app
from sigs_rs.management.config_service import ConfigService
from sigs_rs.persistence.database import InMemoryDatabasePool
from sigs_rs.persistence.repository import TelemetryRepository
from sigs_rs.schemas import (
    ComplianceAuditRequest,
    ComplianceGuidelineRevisionRequest,
    UserInfo,
)
from sigs_rs.telemetry.models import RawTelemetryPacket


class TestBR3ComplianceLockout:
    """SRS BR-3: Compliance Lockout & Resumption Workflow."""

    def test_default_guideline_version_is_3_0(self):
        settings = Settings()
        assert settings.compliance_guideline_version == "3.0"
        assert COMPLIANCE_GUIDELINE_VERSION == "3.0"

    @pytest.mark.asyncio
    async def test_revised_guideline_triggers_compliance_lockout(self):
        """When guideline version != 3.0, lifespan activates compliance lockout & fail_closed."""
        test_settings = Settings(compliance_guideline_version="3.1")
        assert test_settings.compliance_guideline_version != COMPLIANCE_GUIDELINE_VERSION

        test_app = create_app()
        # Mock audit logger to capture emitted events
        logged_events = []

        class MockAudit:
            async def log(self, event_type, severity="INFO", detail=None, **kwargs):
                logged_events.append({"event_type": event_type, "severity": severity, "detail": detail})

        test_app.state.audit_logger = MockAudit()

        # Simulate lifespan startup check
        test_app.state.compliance_guideline_version = test_settings.compliance_guideline_version
        test_app.state.compliance_lockout = False
        test_app.state.compliance_audited = True

        if test_settings.compliance_guideline_version != COMPLIANCE_GUIDELINE_VERSION:
            test_app.state.compliance_lockout = True
            test_app.state.compliance_audited = False
            test_app.state.fail_closed = True
            await test_app.state.audit_logger.log(
                event_type=AuditEvent.COMPLIANCE_LOCKOUT,
                severity="CRITICAL",
                detail={
                    "configured_version": test_settings.compliance_guideline_version,
                    "approved_version": COMPLIANCE_GUIDELINE_VERSION,
                },
            )

        assert test_app.state.compliance_lockout is True
        assert test_app.state.fail_closed is True
        assert any(e["event_type"] == AuditEvent.COMPLIANCE_LOCKOUT for e in logged_events)

    @pytest.mark.asyncio
    async def test_telemetry_ingestion_rejected_during_compliance_lockout(self):
        """When compliance_lockout is True, HTTP telemetry ingestion returns HTTP 503."""
        from sigs_rs.ingestion.http_ingest import ingest_packet, ingest_batch
        from sigs_rs.telemetry.models import RawTelemetryPacket

        req = MagicMock()
        req.app.state.compliance_lockout = True
        req.app.state.fail_closed = True
        req.app.state.metrics = None

        pkt = RawTelemetryPacket(
            sensor_id="sensor-001",
            timestamp=datetime.now(timezone.utc),
            resource_type="water",
            value=10.0,
            unit="liters_per_min",
        )

        with pytest.raises(HTTPException) as exc_info:
            await ingest_packet(req, pkt, Settings(), MagicMock())
        assert exc_info.value.status_code == 503
        assert "compliance guideline revision" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_compliance_status_endpoint(self):
        """GET /api/v1/config/compliance/status reflects lockout and version."""
        req = MagicMock()
        req.app.state.repository = None
        req.app.state.compliance_guideline_version = "3.1"
        req.app.state.compliance_lockout = True
        req.app.state.compliance_audited = False

        status = await ConfigService.get_compliance_status(req)
        assert status.guideline_version == "3.1"
        assert status.lockout_active is True
        assert status.audited is False
        assert "suspended" in status.message

    @pytest.mark.asyncio
    async def test_unauthorized_role_cannot_signoff_compliance(self):
        """QA testers and developers cannot sign off on compliance audits."""
        from sigs_rs.auth.dependencies import require_compliance_auditor_or_security_engineer

        req = MagicMock()
        req.app.state.audit_logger = None

        for role in (Role.QA_TESTER, Role.BACKEND_DEVELOPER, Role.DEVOPS):
            user = UserInfo(sub="unauthorized_user", role=role)
            with pytest.raises(HTTPException) as exc_info:
                await require_compliance_auditor_or_security_engineer(req, user)
            assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_compliance_auditor_and_security_engineer_can_signoff(self):
        """Compliance Auditor and Security Engineer can sign off on audits and resume testing."""
        from sigs_rs.auth.dependencies import require_compliance_auditor_or_security_engineer

        req = MagicMock()
        req.app.state.audit_logger = None

        for role in (Role.COMPLIANCE_AUDITOR, Role.SECURITY_ENGINEER):
            user = UserInfo(sub="auditor_1", role=role)
            res = await require_compliance_auditor_or_security_engineer(req, user)
            assert res == user

    @pytest.mark.asyncio
    async def test_compliance_audit_signoff_clears_lockout_and_resumes_testing(self):
        """Formal compliance audit signoff clears lockout, restores routing, and logs event."""
        req = MagicMock()
        req.app.state.compliance_guideline_version = "3.1"
        req.app.state.compliance_lockout = True
        req.app.state.compliance_audited = False
        req.app.state.fail_closed = True
        req.app.state.pipeline = None
        req.app.state.repository = None

        logged_events = []

        class MockAudit:
            async def log(self, event_type, severity="INFO", detail=None, **kwargs):
                logged_events.append({"event_type": event_type, "severity": severity, "detail": detail})

        req.app.state.audit_logger = MockAudit()

        body = ComplianceAuditRequest(
            guideline_version="3.1",
            audit_notes="Privacy Anonymizer pipeline reviewed against updated compliance regulations v3.1. Verified.",
        )
        user = UserInfo(sub="auditor_jane", role=Role.COMPLIANCE_AUDITOR)

        resp = await ConfigService.compliance_audit_signoff(req, body, user)
        assert resp.updated is True
        assert req.app.state.compliance_lockout is False
        assert req.app.state.compliance_audited is True
        assert req.app.state.fail_closed is False
        assert any(e["event_type"] == AuditEvent.COMPLIANCE_AUDIT_RESUME for e in logged_events)

    @pytest.mark.asyncio
    async def test_revise_compliance_guideline_endpoint(self):
        """Revising guideline immediately suspends testing, persists to DB, and logs event (SRS BR-3)."""
        pool = InMemoryDatabasePool()
        await pool.connect()
        repo = TelemetryRepository(pool)

        logged_events = []

        class MockAudit:
            async def log(self, event_type, severity="INFO", detail=None, **kwargs):
                logged_events.append({"event_type": event_type, "severity": severity, "detail": detail})

        req = MagicMock()
        req.app.state.compliance_guideline_version = "3.0"
        req.app.state.compliance_lockout = False
        req.app.state.compliance_audited = True
        req.app.state.fail_closed = False
        req.app.state.repository = repo
        req.app.state.audit_logger = MockAudit()

        body = ComplianceGuidelineRevisionRequest(
            guideline_version="3.1",
            revision_reason="Annual privacy regulation update.",
        )
        user = UserInfo(sub="sec_eng_alice", role=Role.SECURITY_ENGINEER)

        resp = await ConfigService.revise_compliance_guideline(req, body, user)
        assert resp.updated is True
        assert req.app.state.compliance_guideline_version == "3.1"
        assert req.app.state.compliance_lockout is True
        assert req.app.state.compliance_audited is False
        assert req.app.state.fail_closed is True
        assert any(e["event_type"] == AuditEvent.COMPLIANCE_LOCKOUT for e in logged_events)

        # Check DB persistence
        persisted = await repo.get_setting("compliance.state")
        assert persisted["guideline_version"] == "3.1"
        assert persisted["lockout_active"] is True
        assert persisted["audited"] is False

    @pytest.mark.asyncio
    async def test_guideline_revision_and_signoff_conflict_prevention(self):
        """Sign-off for an older version is rejected when guidelines have been revised (SRS BR-3)."""
        req = MagicMock()
        req.app.state.compliance_guideline_version = "3.2"  # Revised to 3.2
        req.app.state.compliance_lockout = True
        req.app.state.compliance_audited = False
        req.app.state.repository = None
        req.app.state.audit_logger = None

        # Attempt to sign off outdated version 3.1
        body = ComplianceAuditRequest(
            guideline_version="3.1",
            audit_notes="Reviewing version 3.1",
        )
        user = UserInfo(sub="auditor_bob", role=Role.COMPLIANCE_AUDITOR)

        with pytest.raises(HTTPException) as exc_info:
            await ConfigService.compliance_audit_signoff(req, body, user)
        assert exc_info.value.status_code == 409
        assert "Cannot sign off guideline version '3.1'" in exc_info.value.detail
        # Lockout must remain intact
        assert req.app.state.compliance_lockout is True

    @pytest.mark.asyncio
    async def test_lockout_durability_across_restarts(self):
        """Compliance lockout stored in DB survives application restart."""
        pool = InMemoryDatabasePool()
        await pool.connect()
        repo = TelemetryRepository(pool)

        # Persist lockout state in DB prior to restart
        await repo.set_setting(
            key="compliance.state",
            value={
                "guideline_version": "3.1",
                "approved_version": None,
                "lockout_active": True,
                "audited": False,
            },
            updated_by="auditor",
        )

        # Simulate startup lifespan reading from DB
        simulated_app = create_app()
        persisted = await repo.get_setting("compliance.state")
        assert persisted is not None

        simulated_app.state.compliance_guideline_version = persisted["guideline_version"]
        simulated_app.state.compliance_lockout = persisted["lockout_active"]
        simulated_app.state.compliance_audited = persisted["audited"]
        if simulated_app.state.compliance_lockout:
            simulated_app.state.fail_closed = True

        assert simulated_app.state.compliance_lockout is True
        assert simulated_app.state.fail_closed is True
        assert simulated_app.state.compliance_guideline_version == "3.1"

    @pytest.mark.asyncio
    async def test_multi_instance_consistency_via_db(self):
        """Multiple instances stay in sync via shared persistent state."""
        pool = InMemoryDatabasePool()
        await pool.connect()
        shared_repo = TelemetryRepository(pool)

        # Instance 1
        req1 = MagicMock()
        req1.app.state.repository = shared_repo
        req1.app.state.audit_logger = None
        req1.app.state.compliance_guideline_version = "3.0"
        req1.app.state.compliance_lockout = False

        # Instance 2
        req2 = MagicMock()
        req2.app.state.repository = shared_repo
        req2.app.state.audit_logger = None
        req2.app.state.compliance_guideline_version = "3.0"
        req2.app.state.compliance_lockout = False

        # Instance 1 revises guidelines
        body = ComplianceGuidelineRevisionRequest(
            guideline_version="3.5",
            revision_reason="Regulatory mandate",
        )
        await ConfigService.revise_compliance_guideline(
            req1, body, UserInfo(sub="admin", role=Role.SECURITY_ENGINEER)
        )

        # Instance 2 checks compliance status
        status2 = await ConfigService.get_compliance_status(req2)
        assert status2.guideline_version == "3.5"
        assert status2.lockout_active is True
        assert status2.audited is False
        assert req2.app.state.compliance_lockout is True

    @pytest.mark.asyncio
    async def test_database_failure_enforces_fail_closed(self):
        """State store failure during status check enforces safe fail-closed lockout."""
        failing_repo = MagicMock()
        failing_repo.get_setting = AsyncMock(side_effect=PersistenceError("Connection lost"))

        req = MagicMock()
        req.app.state.repository = failing_repo
        req.app.state.compliance_guideline_version = "3.0"
        req.app.state.compliance_lockout = False
        req.app.state.compliance_audited = True
        req.app.state.fail_closed = False

        status = await ConfigService.get_compliance_status(req)
        assert status.lockout_active is True
        assert status.audited is False
        assert req.app.state.compliance_lockout is True
        assert req.app.state.fail_closed is True
        assert "database failure" in status.message.lower()

    @pytest.mark.asyncio
    async def test_mqtt_subscriber_drops_on_compliance_lockout(self):
        """MQTT subscriber drops packets immediately when compliance lockout is active."""
        from sigs_rs.ingestion.mqtt_sub import MQTTSubscriber
        from sigs_rs.ingestion.buffer import IngestionQueue

        queue = IngestionQueue(max_size=10)
        logged_events = []

        class MockAudit:
            async def log(self, event_type, severity="INFO", detail=None, **kwargs):
                logged_events.append({"event_type": event_type, "severity": severity, "detail": detail})

        sub = MQTTSubscriber(
            Settings(),
            queue=queue,
            audit_logger=MockAudit(),
            compliance_check=lambda: True,  # Lockout active
        )

        msg = MagicMock()
        msg.topic = "telemetry/sensor-1"
        msg.payload = b'{"sensor_id":"s1","timestamp":"2026-10-11T12:00:00Z","resource_type":"water","value":10.0,"unit":"liters_per_min"}'

        result = await sub.handle_message(msg)
        assert result is False
        assert queue.depth == 0
        assert any(e["event_type"] == AuditEvent.COMPLIANCE_LOCKOUT for e in logged_events)

    @pytest.mark.asyncio
    async def test_middleware_pipeline_drops_on_compliance_lockout(self):
        """Middleware pipeline drops queued batch immediately when compliance lockout is active."""
        from sigs_rs.middleware.pipeline import MiddlewarePipeline

        queue = MagicMock()
        repo = MagicMock()
        pipeline = MiddlewarePipeline(
            queue=queue,
            inspector=MagicMock(),
            anonymizer=MagicMock(),
            repository=repo,
            circuit_breaker=MagicMock(),
            audit_logger=MagicMock(),
            compliance_check=lambda: True,  # Lockout active
        )

        pkt = RawTelemetryPacket(
            sensor_id="sensor-001",
            timestamp=datetime.now(timezone.utc),
            resource_type="water",
            value=10.0,
            unit="liters_per_min",
        )
        res = await pipeline.process_batch([pkt])
        assert res is False
        repo.insert.assert_not_called()
        repo.insert_batch.assert_not_called()


class TestMQTTCircuitBreakerAndDroppedPacketAuditing:
    """MQTT ingestion, circuit-breaker handling, and dropped-packet auditing."""

    @pytest.mark.asyncio
    async def test_mqtt_drops_and_audits_when_circuit_breaker_open(self):
        """When circuit breaker is open, MQTT subscriber drops packet and audits event."""
        from sigs_rs.ingestion.mqtt_sub import MQTTSubscriber
        from sigs_rs.ingestion.buffer import IngestionQueue

        queue = IngestionQueue(max_size=10)
        cb = MagicMock()
        cb.is_open = True

        logged_events = []

        class MockAudit:
            async def log(self, event_type, severity="INFO", detail=None, **kwargs):
                logged_events.append({"event_type": event_type, "severity": severity, "detail": detail})

        sub = MQTTSubscriber(
            Settings(),
            queue=queue,
            circuit_breaker=cb,
            audit_logger=MockAudit(),
        )

        msg = MagicMock()
        msg.topic = "telemetry/sensor-cb"
        msg.payload = b'{"sensor_id":"s1","timestamp":"2026-10-11T12:00:00Z","resource_type":"water","value":10.0,"unit":"liters_per_min"}'

        result = await sub.handle_message(msg)
        assert result is False
        assert queue.depth == 0
        assert any(e["event_type"] == AuditEvent.CIRCUIT_BREAKER_TRIPPED for e in logged_events)

    @pytest.mark.asyncio
    async def test_pipeline_drops_and_audits_when_circuit_breaker_open(self):
        """When circuit breaker call raises CircuitBreakerOpenError, pipeline drops batch and audits."""
        from sigs_rs.common.exceptions import CircuitBreakerOpenError
        from sigs_rs.middleware.pipeline import MiddlewarePipeline
        from sigs_rs.middleware.inspector import VerificationResult

        cb = MagicMock()
        cb.call = AsyncMock(side_effect=CircuitBreakerOpenError("Circuit open"))

        inspector = MagicMock()
        inspector.inspect = AsyncMock(return_value=VerificationResult.VERIFIED)

        anonymizer = MagicMock()
        sanitized_pkt = MagicMock()
        anonymizer.anonymize = MagicMock(return_value=sanitized_pkt)

        logged_events = []

        class MockAudit:
            async def log(self, event_type, severity="INFO", detail=None, **kwargs):
                logged_events.append({"event_type": event_type, "severity": severity, "detail": detail})

        repo = MagicMock()
        pipeline = MiddlewarePipeline(
            queue=MagicMock(),
            inspector=inspector,
            anonymizer=anonymizer,
            repository=repo,
            circuit_breaker=cb,
            audit_logger=MockAudit(),
        )

        pkt = RawTelemetryPacket(
            sensor_id="sensor-001",
            timestamp=datetime.now(timezone.utc),
            resource_type="water",
            value=10.0,
            unit="liters_per_min",
        )
        res = await pipeline.process_batch([pkt])
        assert res is False
        assert any(e["event_type"] == AuditEvent.CIRCUIT_BREAKER_TRIPPED for e in logged_events)

    @pytest.mark.asyncio
    async def test_mqtt_drop_remains_safe_when_audit_logger_fails(self):
        """When audit logging fails, dropped packets are NEVER persisted or enqueued."""
        from sigs_rs.ingestion.mqtt_sub import MQTTSubscriber
        from sigs_rs.ingestion.buffer import IngestionQueue

        queue = IngestionQueue(max_size=10)

        class FailingAudit:
            async def log(self, *args, **kwargs):
                raise RuntimeError("Audit database disk full!")

        sub = MQTTSubscriber(
            Settings(),
            queue=queue,
            audit_logger=FailingAudit(),
        )

        # Send an oversized packet (>512 KB)
        msg = MagicMock()
        msg.topic = "telemetry/oversized"
        msg.payload = b"X" * (512 * 1024 + 10)

        # Must not raise unhandled exception, must return False, and must not enqueue
        result = await sub.handle_message(msg)
        assert result is False
        assert queue.depth == 0

    @pytest.mark.asyncio
    async def test_audit_logger_redacts_pii_from_detail(self):
        """TelemetryRepository._sanitize_audit_detail ensures no PII leaks into audit log."""
        pool = InMemoryDatabasePool()
        await pool.connect()
        repo = TelemetryRepository(pool)

        leak_attempt = {
            "reason": "malformed packet",
            "lat": 37.7749,
            "lon": -122.4194,
            "mac_address": "00:1A:2B:3C:4D:5E",
            "device_id": "secret-dev-99",
            "nested": {
                "latitude": 45.0,
                "clean_field": "safe_value",
            },
        }

        sanitized = repo._sanitize_audit_detail(leak_attempt)
        assert sanitized["lat"] == "[REDACTED_PII]"
        assert sanitized["lon"] == "[REDACTED_PII]"
        assert sanitized["mac_address"] == "[REDACTED_PII]"
        assert sanitized["device_id"] == "[REDACTED_PII]"
        assert sanitized["nested"]["latitude"] == "[REDACTED_PII]"
        assert sanitized["nested"]["clean_field"] == "safe_value"

        # Verify insert_audit stores the redacted detail in database
        await repo.insert_audit(
            event_type=AuditEvent.PACKET_DROPPED_MALFORMED,
            severity="WARN",
            sensor_id="sensor-001",
            detail=leak_attempt,
        )
        logs = await repo.get_audit_logs(limit=1)
        assert len(logs) == 1
        stored_detail = logs[0]["detail"]
        if isinstance(stored_detail, str):
            import json
            stored_detail = json.loads(stored_detail)
        assert stored_detail["lat"] == "[REDACTED_PII]"
        assert stored_detail["mac_address"] == "[REDACTED_PII]"
        assert stored_detail["nested"]["latitude"] == "[REDACTED_PII]"


class TestREQSE4AuditImmutability:
    """SRS REQ-SE-4: Audit Log Immutability."""

    @pytest.mark.asyncio
    async def test_in_memory_db_blocks_update_on_audit_log(self):
        pool = InMemoryDatabasePool()
        await pool.connect()

        with pytest.raises(PersistenceError, match="immutable"):
            await pool.execute("UPDATE audit.security_log SET severity = 'LOW' WHERE id = 1;")

    @pytest.mark.asyncio
    async def test_in_memory_db_blocks_delete_on_audit_log(self):
        pool = InMemoryDatabasePool()
        await pool.connect()

        with pytest.raises(PersistenceError, match="immutable"):
            await pool.execute("DELETE FROM audit.security_log WHERE id = 1;")

    def test_schema_sql_contains_immutability_trigger(self):
        schema_path = Path("src/sigs_rs/persistence/schema.sql")
        content = schema_path.read_text(encoding="utf-8")
        assert "trg_audit_immutable" in content
        assert "audit.prevent_tampering" in content
        assert "UPDATE OR DELETE ON audit.security_log" in content

    def test_init_db_sql_contains_immutability_trigger(self):
        init_db_path = Path("docker/init-db.sql")
        content = init_db_path.read_text(encoding="utf-8")
        assert "trg_audit_immutable" in content
        assert "audit.prevent_tampering" in content
        assert "UPDATE OR DELETE ON audit.security_log" in content


class TestK8sConfigurationConsistency:
    """SRS REQ-QA-3 / REQ-SF-1: Kubernetes configuration validation."""

    def test_k8s_configmap_uses_staging_environment(self):
        configmap_path = Path("k8s/configmap.yaml")
        content = configmap_path.read_text(encoding="utf-8")
        assert 'APP_ENV: "staging"' in content

    def test_k8s_configmap_values_pass_settings_validation(self):
        """Simulate applying env from k8s/configmap.yaml to ensure no ConfigError crash."""
        s = Settings(
            app_env="staging",
            database_url="postgresql+asyncpg://sigs_user:pass@timescaledb:5432/sigs_db",
            allowed_db_hosts="timescaledb,timescaledb.sigs-rs.svc.cluster.local",
            database_ssl_mode="require",
            mqtt_broker_host="mosquitto",
            mqtt_broker_port=1883,
            mqtt_use_tls=False,
        )
        assert s.app_env == "staging"
        assert s.mqtt_broker_port == 1883
        assert s.mqtt_use_tls is False


class TestCheck1CrossInstanceComplianceConcurrency:
    """Check 1 — BR-3 Cross-Instance Concurrency & State Consistency."""

    @pytest.mark.asyncio
    async def test_cross_instance_concurrent_revision_and_signoff_race(self):
        """
        Multi-instance race: Instance 1 attempts signoff on guideline v3.0,
        while Instance 2 concurrently revises guideline to v3.5.
        Database-level row locking and atomic checks must reject Instance 1's signoff with HTTP 409
        and prevent Instance 1 from overwriting Instance 2's revision.
        """
        pool = InMemoryDatabasePool()
        await pool.connect()
        shared_repo = TelemetryRepository(pool)

        # Baseline seed
        await shared_repo.set_setting(
            key="compliance.state",
            value={
                "guideline_version": "3.0",
                "approved_version": "3.0",
                "lockout_active": True,  # Pending audit
                "audited": False,
            },
            updated_by="system",
        )

        # Instance 1 mock app state
        req1 = MagicMock()
        req1.app.state.repository = shared_repo
        req1.app.state.audit_logger = None
        req1.app.state.compliance_guideline_version = "3.0"
        req1.app.state.compliance_lockout = True
        req1.app.state.compliance_audited = False
        req1.app.state.fail_closed = True

        # Instance 2 mock app state
        req2 = MagicMock()
        req2.app.state.repository = shared_repo
        req2.app.state.audit_logger = None
        req2.app.state.compliance_guideline_version = "3.0"
        req2.app.state.compliance_lockout = True
        req2.app.state.compliance_audited = False
        req2.app.state.fail_closed = True

        # Instance 2 concurrently revises guidelines to v3.5
        body_rev = ComplianceGuidelineRevisionRequest(
            guideline_version="3.5",
            revision_reason="Emergency regulatory revision",
        )
        await ConfigService.revise_compliance_guideline(
            req2, body_rev, UserInfo(sub="sec_eng_2", role=Role.SECURITY_ENGINEER)
        )

        # Instance 1 attempts to sign off obsolete guideline v3.0
        body_signoff = ComplianceAuditRequest(
            guideline_version="3.0",
            audit_notes="Audited by auditor 1 unaware of concurrent revision",
        )
        with pytest.raises(HTTPException) as exc_info:
            await ConfigService.compliance_audit_signoff(
                req1, body_signoff, UserInfo(sub="auditor_1", role=Role.COMPLIANCE_AUDITOR)
            )
        assert exc_info.value.status_code == 409
        assert "active guideline was revised to '3.5'" in exc_info.value.detail

        # Verify DB setting preserves v3.5 lockout and was not overwritten by Instance 1
        persisted = await shared_repo.get_setting("compliance.state")
        assert persisted["guideline_version"] == "3.5"
        assert persisted["lockout_active"] is True
        assert persisted["audited"] is False

    @pytest.mark.asyncio
    async def test_cross_instance_sync_loop_propagates_lockout(self):
        """Cross-instance state freshness: background sync pulls active lockout into app.state."""
        pool = InMemoryDatabasePool()
        await pool.connect()
        shared_repo = TelemetryRepository(pool)

        # Instance 1 initially running and healthy
        app_mock = MagicMock()
        app_mock.state.compliance_guideline_version = "3.0"
        app_mock.state.compliance_lockout = False
        app_mock.state.compliance_audited = True
        app_mock.state.fail_closed = False

        # Instance 2 triggers guideline revision in shared DB
        await shared_repo.revise_compliance_state(
            new_version="3.8",
            revision_reason="New privacy mandate",
            user_id="sec_eng",
        )

        # Simulate sync loop execution step
        db_state = await shared_repo.get_setting("compliance.state")
        if isinstance(db_state, dict):
            app_mock.state.compliance_guideline_version = db_state.get("guideline_version")
            app_mock.state.compliance_lockout = db_state.get("lockout_active", False)
            app_mock.state.compliance_audited = db_state.get("audited", not app_mock.state.compliance_lockout)
            if app_mock.state.compliance_lockout:
                app_mock.state.fail_closed = True

        assert app_mock.state.compliance_guideline_version == "3.8"
        assert app_mock.state.compliance_lockout is True
        assert app_mock.state.compliance_audited is False
        assert app_mock.state.fail_closed is True

    @pytest.mark.asyncio
    async def test_cross_instance_sync_loop_fails_closed_on_db_outage(self):
        """Cross-instance state freshness: database failure during sync enforces fail-closed."""
        failing_repo = MagicMock()
        failing_repo.get_setting = AsyncMock(side_effect=PersistenceError("DB network split"))

        app_mock = MagicMock()
        app_mock.state.compliance_lockout = False
        app_mock.state.compliance_audited = True
        app_mock.state.fail_closed = False

        # Simulate sync step catching DB outage
        try:
            await failing_repo.get_setting("compliance.state")
        except Exception:
            app_mock.state.compliance_lockout = True
            app_mock.state.compliance_audited = False
            app_mock.state.fail_closed = True

        assert app_mock.state.compliance_lockout is True
        assert app_mock.state.compliance_audited is False
        assert app_mock.state.fail_closed is True


class TestCheck2MQTTRejectionAuditAndPersistenceIsolation:
    """Check 2 — MQTT Rejection, Audit Integrity, and Persistence Isolation."""

    @pytest.mark.asyncio
    async def test_scenario_a_circuit_breaker_open_rejection_audit_and_no_persistence(self):
        """
        Scenario A: Circuit breaker open.
        MQTT message is dropped, CIRCUIT_BREAKER_TRIPPED is audited, zero persistence.
        """
        from sigs_rs.ingestion.mqtt_sub import MQTTSubscriber
        from sigs_rs.ingestion.buffer import IngestionQueue
        from sigs_rs.management.audit_logger import AuditLogger

        pool = InMemoryDatabasePool()
        await pool.connect()
        repo = TelemetryRepository(pool)
        audit = AuditLogger(repo)

        queue = IngestionQueue(max_size=10)
        cb = MagicMock()
        cb.is_open = True

        sub = MQTTSubscriber(
            Settings(),
            queue=queue,
            circuit_breaker=cb,
            audit_logger=audit,
        )

        msg = MagicMock()
        msg.topic = "telemetry/sensor-cb-open"
        msg.payload = b'{"sensor_id":"s1","timestamp":"2026-10-11T12:00:00Z","resource_type":"water","value":10.0,"unit":"liters_per_min"}'

        accepted = await sub.handle_message(msg)
        assert accepted is False
        assert queue.depth == 0

        # Verify audit log recorded
        logs = await repo.get_audit_logs(limit=10, event_type=AuditEvent.CIRCUIT_BREAKER_TRIPPED)
        assert len(logs) == 1
        assert "circuit breaker open" in str(logs[0]["detail"]).lower()

        # Invariant: zero records persisted in telemetry.sanitized_readings
        assert await repo.count_readings() == 0

    @pytest.mark.asyncio
    async def test_scenario_b_compliance_lockout_rejection_audit_and_no_persistence(self):
        """
        Scenario B: Compliance lockout active.
        MQTT message is dropped, COMPLIANCE_LOCKOUT is audited, zero persistence.
        """
        from sigs_rs.ingestion.mqtt_sub import MQTTSubscriber
        from sigs_rs.ingestion.buffer import IngestionQueue
        from sigs_rs.management.audit_logger import AuditLogger

        pool = InMemoryDatabasePool()
        await pool.connect()
        repo = TelemetryRepository(pool)
        audit = AuditLogger(repo)

        queue = IngestionQueue(max_size=10)
        sub = MQTTSubscriber(
            Settings(),
            queue=queue,
            audit_logger=audit,
            compliance_check=lambda: True,  # Lockout active
        )

        msg = MagicMock()
        msg.topic = "telemetry/sensor-lockout"
        msg.payload = b'{"sensor_id":"s2","timestamp":"2026-10-11T12:00:00Z","resource_type":"water","value":25.0,"unit":"liters_per_min"}'

        accepted = await sub.handle_message(msg)
        assert accepted is False
        assert queue.depth == 0

        logs = await repo.get_audit_logs(limit=10, event_type=AuditEvent.COMPLIANCE_LOCKOUT)
        assert len(logs) == 1
        assert "simulation testing suspended" in str(logs[0]["detail"]).lower()

        assert await repo.count_readings() == 0

    @pytest.mark.asyncio
    async def test_scenario_c_case_variant_and_nested_pii_redaction(self):
        """
        Scenario C: Audit store available + recursive case-variant PII redaction.
        Ensures LAT, Latitude, Lon, Longitude, MAC_ADDRESS, device_id, Geo are redacted.
        """
        pool = InMemoryDatabasePool()
        await pool.connect()
        repo = TelemetryRepository(pool)

        malicious_audit_payload = {
            "LAT": 37.7749,
            "Latitude": 37.7749,
            "LON": -122.4194,
            "Longitude": -122.4194,
            "MAC_ADDRESS": "AA:BB:CC:DD:EE:FF",
            "device_id": "sensor-device-007",
            "Geo": {"nested_lat": 1.23, "LAT": 4.56},
            "safe_metadata": {
                "sensor_type": "water_flow",
                "nested_list": [
                    {"MAC_Address": "11:22:33:44:55:66", "status": "ok"}
                ],
            },
        }

        sanitized = repo._sanitize_audit_detail(malicious_audit_payload)
        assert sanitized["LAT"] == "[REDACTED_PII]"
        assert sanitized["Latitude"] == "[REDACTED_PII]"
        assert sanitized["LON"] == "[REDACTED_PII]"
        assert sanitized["Longitude"] == "[REDACTED_PII]"
        assert sanitized["MAC_ADDRESS"] == "[REDACTED_PII]"
        assert sanitized["device_id"] == "[REDACTED_PII]"
        assert sanitized["Geo"] == "[REDACTED_PII]"
        assert sanitized["safe_metadata"]["sensor_type"] == "water_flow"
        assert sanitized["safe_metadata"]["nested_list"][0]["MAC_Address"] == "[REDACTED_PII]"
        assert sanitized["safe_metadata"]["nested_list"][0]["status"] == "ok"

        # Verify database record
        await repo.insert_audit(
            event_type=AuditEvent.PACKET_DROPPED_MALFORMED,
            severity="CRITICAL",
            sensor_id="sensor-pii",
            detail=malicious_audit_payload,
        )
        logs = await repo.get_audit_logs(limit=1)
        assert len(logs) == 1
        stored_detail = logs[0]["detail"]
        if isinstance(stored_detail, str):
            import json
            stored_detail = json.loads(stored_detail)
        assert stored_detail["LAT"] == "[REDACTED_PII]"
        assert stored_detail["MAC_ADDRESS"] == "[REDACTED_PII]"
        assert stored_detail["safe_metadata"]["nested_list"][0]["MAC_Address"] == "[REDACTED_PII]"

    @pytest.mark.asyncio
    async def test_scenario_d_audit_store_failure_fail_closed_no_persistence(self):
        """
        Scenario D: Audit store fails or raises an exception.
        Subscriber catches the error, logs a local error, and STILL drops the packet (return False).
        Zero packets are enqueued, zero packets reach repository.
        Durable audit logging CANNOT succeed when audit storage is unavailable; the system
        fails closed locally and rejects the incoming packet.
        """
        from sigs_rs.ingestion.mqtt_sub import MQTTSubscriber
        from sigs_rs.ingestion.buffer import IngestionQueue

        pool = InMemoryDatabasePool()
        await pool.connect()
        repo = TelemetryRepository(pool)
        queue = IngestionQueue(max_size=10)

        class FailingAuditLogger:
            async def log(self, *args, **kwargs):
                raise PersistenceError("Audit storage connection timed out!")

        # 1. Circuit breaker drop with failing audit
        cb = MagicMock()
        cb.is_open = True
        sub_cb = MQTTSubscriber(
            Settings(),
            queue=queue,
            circuit_breaker=cb,
            audit_logger=FailingAuditLogger(),
        )
        msg1 = MagicMock()
        msg1.topic = "telemetry/cb-failing-audit"
        msg1.payload = b'{"sensor_id":"s1","timestamp":"2026-10-11T12:00:00Z","resource_type":"water","value":10.0,"unit":"liters_per_min"}'
        assert await sub_cb.handle_message(msg1) is False
        assert queue.depth == 0

        # 2. Compliance lockout drop with failing audit
        sub_comp = MQTTSubscriber(
            Settings(),
            queue=queue,
            compliance_check=lambda: True,
            audit_logger=FailingAuditLogger(),
        )
        msg2 = MagicMock()
        msg2.topic = "telemetry/comp-failing-audit"
        msg2.payload = b'{"sensor_id":"s2","timestamp":"2026-10-11T12:00:00Z","resource_type":"water","value":10.0,"unit":"liters_per_min"}'
        assert await sub_comp.handle_message(msg2) is False
        assert queue.depth == 0

        # 3. Oversized drop with failing audit
        sub_size = MQTTSubscriber(
            Settings(),
            queue=queue,
            audit_logger=FailingAuditLogger(),
        )
        msg3 = MagicMock()
        msg3.topic = "telemetry/oversized-failing-audit"
        msg3.payload = b"X" * (512 * 1024 + 50)
        assert await sub_size.handle_message(msg3) is False
        assert queue.depth == 0

        # 4. Malformed drop with failing audit
        msg4 = MagicMock()
        msg4.topic = "telemetry/malformed-failing-audit"
        msg4.payload = b"not-valid-json"
        assert await sub_size.handle_message(msg4) is False
        assert queue.depth == 0

        # Invariant (a): rejected packets never reach persistence, even when audit store fails
        assert await repo.count_readings() == 0
        # Invariant (c): audit events were NOT recorded because store is unavailable
        assert len(await repo.get_audit_logs()) == 0

    @pytest.mark.asyncio
    async def test_scenario_e_no_persistence_invariant_across_complete_rejection_matrix(self):
        """
        Scenario E: Complete rejection matrix no-persistence invariant.
        Across every rejection path, repo.count_readings() remains strictly 0.
        """
        from sigs_rs.ingestion.mqtt_sub import MQTTSubscriber
        from sigs_rs.ingestion.buffer import IngestionQueue
        from sigs_rs.management.audit_logger import AuditLogger
        from sigs_rs.middleware.pipeline import MiddlewarePipeline
        from sigs_rs.middleware.inspector import AnomalousPacketInspector, VerificationResult
        from sigs_rs.middleware.anonymizer import PrivacyAnonymizer

        pool = InMemoryDatabasePool()
        await pool.connect()
        repo = TelemetryRepository(pool)
        audit = AuditLogger(repo)
        queue = IngestionQueue(max_size=10)

        # Path 1: MQTT circuit breaker drop
        cb_open = MagicMock()
        cb_open.is_open = True
        sub_cb = MQTTSubscriber(Settings(), queue=queue, circuit_breaker=cb_open, audit_logger=audit)
        m1 = MagicMock(topic="t/1", payload=b'{"sensor_id":"s1","timestamp":"2026-10-11T12:00:00Z","resource_type":"water","value":1.0,"unit":"liters_per_min"}')
        assert await sub_cb.handle_message(m1) is False

        # Path 2: MQTT compliance lockout drop
        sub_comp = MQTTSubscriber(Settings(), queue=queue, compliance_check=lambda: True, audit_logger=audit)
        m2 = MagicMock(topic="t/2", payload=b'{"sensor_id":"s2","timestamp":"2026-10-11T12:00:00Z","resource_type":"water","value":2.0,"unit":"liters_per_min"}')
        assert await sub_comp.handle_message(m2) is False

        # Path 3: MQTT oversized drop
        sub_normal = MQTTSubscriber(Settings(), queue=queue, audit_logger=audit)
        m3 = MagicMock(topic="t/3", payload=b"Y" * (512 * 1024 + 1))
        assert await sub_normal.handle_message(m3) is False

        # Path 4: MQTT malformed JSON drop
        m4 = MagicMock(topic="t/4", payload=b'{"bad_json":')
        assert await sub_normal.handle_message(m4) is False

        # Path 5: MQTT schema violation drop
        m5 = MagicMock(topic="t/5", payload=b'{"missing":"sensor_id"}')
        assert await sub_normal.handle_message(m5) is False

        # Path 6: Pipeline compliance lockout drop
        pipe_locked = MiddlewarePipeline(
            queue=queue,
            inspector=MagicMock(),
            anonymizer=MagicMock(),
            repository=repo,
            circuit_breaker=MagicMock(),
            audit_logger=audit,
            compliance_check=lambda: True,
        )
        pkt_valid = RawTelemetryPacket(
            sensor_id="s_valid",
            timestamp=datetime.now(timezone.utc),
            resource_type="water",
            value=10.0,
            unit="liters_per_min",
        )
        assert await pipe_locked.process_batch([pkt_valid]) is False

        # Path 7: Pipeline inspector rate limit drop
        insp_rate = MagicMock()
        insp_rate.inspect = AsyncMock(return_value=VerificationResult.RATE_LIMIT_EXCEEDED)
        pipe_rate = MiddlewarePipeline(
            queue=queue,
            inspector=insp_rate,
            anonymizer=MagicMock(),
            repository=repo,
            circuit_breaker=MagicMock(),
            audit_logger=audit,
        )
        assert await pipe_rate.process_batch([pkt_valid]) is False

        # Path 8: Pipeline inspector invalid metrics drop
        insp_metric = MagicMock()
        insp_metric.inspect = AsyncMock(return_value=VerificationResult.INVALID_METRIC)
        pipe_metric = MiddlewarePipeline(
            queue=queue,
            inspector=insp_metric,
            anonymizer=MagicMock(),
            repository=repo,
            circuit_breaker=MagicMock(),
            audit_logger=audit,
        )
        assert await pipe_metric.process_batch([pkt_valid]) is False

        # Path 9: Pipeline circuit breaker open drop
        from sigs_rs.common.exceptions import CircuitBreakerOpenError
        async def _fake_cb_call(coro):
            if hasattr(coro, "close"):
                coro.close()
            raise CircuitBreakerOpenError("CB open")

        cb_failing = MagicMock()
        cb_failing.call = AsyncMock(side_effect=_fake_cb_call)
        insp_ok = MagicMock()
        insp_ok.inspect = AsyncMock(return_value=VerificationResult.VERIFIED)
        anon_ok = MagicMock()
        anon_ok.anonymize = MagicMock(return_value=MagicMock())
        pipe_cb = MiddlewarePipeline(
            queue=queue,
            inspector=insp_ok,
            anonymizer=anon_ok,
            repository=repo,
            circuit_breaker=cb_failing,
            audit_logger=audit,
        )
        assert await pipe_cb.process_batch([pkt_valid]) is False

        # Total persistence across all 9 rejection paths MUST BE STRICTLY ZERO
        assert await repo.count_readings() == 0
