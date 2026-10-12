"""
Phase 14 Security Audit & Hostile QA Regression Tests — SIGS-RS.

Validates remediation for:
- Finding 1: Case-sensitivity bypass in Privacy Anonymizer, Repository, and Audit Redaction (Attack Class 3)
- Finding 2: Missing enforcement of allowed_db_hosts environment isolation (Attack Class 9 / REQ-SF-1)
- Finding 3: Missing enforcement of database_ssl_mode='verify-full' in production (Attack Class 9 / REQ-SE-3)
- Finding 4: MQTT subscriber fail-closed and circuit-breaker open gate (Attack Classes 7, 11, 12 / REQ-SF-2, REQ-SF-3)
- Finding 5: ConfigService bypass of anonymizer field validation (Attack Class 5 / BR-1, REQ-PA-1)
- Finding 6: Whitespace-only sensor_id bypass (Attack Class 2 / REQ-TI-2, REQ-API-1)
- Finding 7: Non-finite numbers (NaN/Inf) in extended telemetry metrics (Attack Classes 1, 2 / REQ-API-3)
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any
import pytest
from pydantic import ValidationError

from sigs_rs.common.exceptions import (
    AnonymizerError,
    ConfigError,
    PersistenceError,
    ProductionDatabaseError,
    ImpossibleValueError,
)
from sigs_rs.config import Settings
from sigs_rs.middleware.anonymizer import PrivacyAnonymizer, _find_keys
from sigs_rs.middleware.payload_validator import PayloadValidator
from sigs_rs.persistence.circuit_breaker import CircuitBreaker, CBState
from sigs_rs.persistence.database import InMemoryDatabasePool
from sigs_rs.persistence.repository import TelemetryRepository
from sigs_rs.telemetry.models import (
    RawTelemetryPacket,
    SanitizedTelemetryPacket,
    EnergyExtended,
    WaterExtended,
    AirQualityExtended,
)
from sigs_rs.management.audit_service import _sanitize_audit_detail
from sigs_rs.management.config_service import ConfigService
from sigs_rs.schemas import AnonymizerConfig, UserInfo


class TestAlternativeCasingPIISecurity:
    """Finding 1 (Attack Class 3): Case-insensitive PII handling."""

    def test_alternative_casing_pii_stripped_by_anonymizer(self):
        anonymizer = PrivacyAnonymizer()
        data = {
            "metadata": {
                "Latitude": 12.3456,
                "LONGITUDE": 80.1234,
                "Mac_Address": "00:11:22:33:44:55",
                "Device_ID": "dev-999",
                "safe_field": "keep_me",
            },
            "nested_list": [
                {"LAT": 13.0, "LON": 80.0},
                {"Geo": {"lat": 1.0}},
            ],
        }
        PrivacyAnonymizer._strip_pii(data, anonymizer.pii_fields)
        # All cased variants must be stripped
        meta = data["metadata"]
        assert "Latitude" not in meta
        assert "LONGITUDE" not in meta
        assert "Mac_Address" not in meta
        assert "Device_ID" not in meta
        assert meta["safe_field"] == "keep_me"

        assert "LAT" not in data["nested_list"][0]
        assert "LON" not in data["nested_list"][0]
        assert "Geo" not in data["nested_list"][1]

    def test_find_keys_detects_alternative_casing(self):
        targets = {"latitude", "longitude", "mac_address"}
        data = {
            "Latitude": 12.0,
            "sub": {"MAC_ADDRESS": "AA:BB"},
        }
        found = _find_keys(data, targets)
        assert "Latitude" in found
        assert "MAC_ADDRESS" in found

    @pytest.mark.asyncio
    async def test_repository_scan_for_pii_detects_alternative_casing(self):
        repo = TelemetryRepository(InMemoryDatabasePool())
        data_with_pii = {
            "metadata": {"Latitude": 12.34, "MAC_Address": "AA:BB:CC"},
        }
        detected = repo._scan_for_pii(data_with_pii)
        assert len(detected) == 2
        assert "Latitude" in detected
        assert "MAC_Address" in detected

    @pytest.mark.asyncio
    async def test_repository_insert_rejects_alternative_casing_pii(self):
        pool = InMemoryDatabasePool()
        await pool.connect()
        repo = TelemetryRepository(pool)
        record = SanitizedTelemetryPacket(
            sensor_id="sensor-001",
            timestamp=datetime.now(timezone.utc),
            resource_type="water",
            value=10.0,
            unit="liters_per_min",
            metadata={"Latitude": 12.34, "Device_Id": "dev-01"},
        )
        with pytest.raises(PersistenceError, match="restricted PII fields"):
            await repo.insert(record)

    def test_audit_detail_redaction_case_insensitive(self):
        detail = {
            "Latitude": 12.34,
            "mac_address": "AA:BB",
            "DEVICE_ID": "dev-123",
            "safe": "value",
        }
        cleaned = _sanitize_audit_detail(detail)
        assert cleaned["Latitude"] == "[REDACTED]"
        assert cleaned["mac_address"] == "[REDACTED]"
        assert cleaned["DEVICE_ID"] == "[REDACTED]"
        assert cleaned["safe"] == "value"


class TestDatabaseEnvironmentIsolationSecurity:
    """Finding 2 & 3 (Attack Class 9 / REQ-SF-1, REQ-SE-3)."""

    def test_unauthorized_db_host_rejected_req_sf_1(self):
        with pytest.raises(ProductionDatabaseError):
            Settings(
                database_url="postgresql+asyncpg://user:pass@corporate-main-db.internal:5432/main",
                allowed_db_hosts="localhost,timescaledb,127.0.0.1",
            )

    def test_staging_and_test_db_hosts_accepted_req_sf_1(self):
        s1 = Settings(
            database_url="postgresql+asyncpg://user:pass@staging-db:5432/test",
            allowed_db_hosts="localhost,timescaledb,127.0.0.1",
        )
        assert "staging-db" in s1.database_url

        s2 = Settings(
            database_url="postgresql+asyncpg://user:pass@test-cluster.corp:5432/test",
            allowed_db_hosts="localhost,timescaledb,127.0.0.1",
        )
        assert "test-cluster" in s2.database_url

    def test_production_database_ssl_mode_enforced_req_se_3(self):
        # In production mode, database_ssl_mode must be 'verify-full'
        with pytest.raises(ConfigError, match="verify-full"):
            Settings(
                app_env="production",
                mqtt_use_tls=True,
                mqtt_broker_port=8883,
                database_ssl_mode="disable",
            )

        # verify-full is accepted in production
        s = Settings(
            app_env="production",
            mqtt_use_tls=True,
            mqtt_broker_port=8883,
            database_ssl_mode="verify-full",
            database_url="postgresql+asyncpg://user:pass@staging-db:5432/test",
        )
        assert s.database_ssl_mode == "verify-full"


class TestMQTTSubscriberResilience:
    """Finding 4 (Attack Classes 7, 11, 12 / REQ-SF-2, REQ-SF-3)."""

    @pytest.mark.asyncio
    async def test_mqtt_drops_when_circuit_breaker_open(self):
        from sigs_rs.ingestion.mqtt_sub import MQTTSubscriber
        from sigs_rs.ingestion.buffer import IngestionQueue

        settings = Settings()
        queue = IngestionQueue(max_size=10)
        cb = CircuitBreaker(failure_threshold_ms=2000, cooldown_seconds=30)
        cb._state = CBState.OPEN

        subscriber = MQTTSubscriber(
            settings=settings,
            queue=queue,
            circuit_breaker=cb,
        )
        subscriber._running = True

        class MockMessage:
            topic = "telemetry/water"
            payload = b'{"sensor_id": "sensor-01", "timestamp": "2026-07-17T10:00:00Z", "resource_type": "water", "value": 1.0, "unit": "liters_per_min"}'

        result = await subscriber.handle_message(MockMessage())
        assert result is False
        assert queue.depth == 0

    @pytest.mark.asyncio
    async def test_mqtt_drops_when_fail_closed_active(self):
        from sigs_rs.ingestion.mqtt_sub import MQTTSubscriber
        from sigs_rs.ingestion.buffer import IngestionQueue

        settings = Settings()
        queue = IngestionQueue(max_size=10)
        fail_closed = True

        subscriber = MQTTSubscriber(
            settings=settings,
            queue=queue,
            fail_closed_check=lambda: fail_closed,
        )
        subscriber._running = True

        class MockMessage:
            topic = "telemetry/water"
            payload = b'{"sensor_id": "sensor-01", "timestamp": "2026-07-17T10:00:00Z", "resource_type": "water", "value": 1.0, "unit": "liters_per_min"}'

        result = await subscriber.handle_message(MockMessage())
        assert result is False
        assert queue.depth == 0

    @pytest.mark.asyncio
    async def test_mqtt_drops_when_subscriber_stopped(self):
        from sigs_rs.ingestion.mqtt_sub import MQTTSubscriber
        from sigs_rs.ingestion.buffer import IngestionQueue

        settings = Settings()
        queue = IngestionQueue(max_size=10)
        subscriber = MQTTSubscriber(settings=settings, queue=queue)
        subscriber._stop_requested = True

        class MockMessage:
            topic = "telemetry/water"
            payload = b'{"sensor_id": "sensor-01", "timestamp": "2026-07-17T10:00:00Z", "resource_type": "water", "value": 1.0, "unit": "liters_per_min"}'

        result = await subscriber.handle_message(MockMessage())
        assert result is False
        assert queue.depth == 0


class TestConfigServiceValidation:
    """Finding 5 (Attack Class 5 / BR-1, REQ-PA-1)."""

    def test_anonymizer_config_schema_rejects_empty_fields(self):
        with pytest.raises(ValidationError):
            AnonymizerConfig(pii_fields=["   "])

        with pytest.raises(ValidationError):
            AnonymizerConfig(pii_fields=["valid", ""])

    @pytest.mark.asyncio
    async def test_config_service_invokes_anonymizer_update_pii_fields(self):
        class MockApp:
            class state:
                anonymizer = PrivacyAnonymizer()
                repository = None
                audit_logger = None

        class MockRequest:
            app = MockApp()

        user = UserInfo(sub="sec_eng_1", role="security_engineer")
        cfg = AnonymizerConfig(pii_fields=["latitude", "longitude", "mac_address", "custom_pii"])
        resp = await ConfigService.update_anonymizer_config(MockRequest(), cfg, user)
        assert resp.updated is True
        assert "custom_pii" in MockApp.state.anonymizer.pii_fields


class TestSensorIdAndPayloadIntegrity:
    """Finding 6 & 7 (Attack Classes 1, 2 / REQ-TI-2, REQ-API-3)."""

    def test_whitespace_only_sensor_id_rejected(self):
        with pytest.raises(ValidationError, match="sensor_id"):
            RawTelemetryPacket(
                sensor_id="   ",
                timestamp=datetime.now(timezone.utc),
                resource_type="water",
                value=10.0,
                unit="liters_per_min",
            )

    def test_payload_validator_rejects_non_finite_extended_metrics(self):
        validator = PayloadValidator()

        # Energy packet with Inf in voltage
        packet_inf_energy = RawTelemetryPacket(
            sensor_id="sensor-energy",
            timestamp=datetime.now(timezone.utc),
            resource_type="energy",
            value=10.0,
            unit="kWh",
            energy_ext=EnergyExtended(voltage_v=float("inf")),
        )
        with pytest.raises(ImpossibleValueError, match="NaN/Infinity"):
            validator.validate(packet_inf_energy)
        assert validator.is_valid(packet_inf_energy) is False

        # Energy packet with NaN in current_a
        packet_nan_energy = RawTelemetryPacket(
            sensor_id="sensor-energy",
            timestamp=datetime.now(timezone.utc),
            resource_type="energy",
            value=5.0,
            unit="kWh",
            energy_ext=EnergyExtended(current_a=float("nan")),
        )
        with pytest.raises(ImpossibleValueError, match="NaN/Infinity"):
            validator.validate(packet_nan_energy)
        assert validator.is_valid(packet_nan_energy) is False
