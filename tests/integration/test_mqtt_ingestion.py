"""
Integration tests for Phase 6: Secure MQTT Telemetry Ingestion.

Verifies:
1. MQTT client/broker integration and lifecycle.
2. Full pipeline execution:
   MQTT -> Payload Size Gate -> Schema Validator -> IngestionQueue -> Inspector -> Anonymizer -> Persistence.
3. Payload size enforcement (SRS REQ-TI-3: 512 KB limit).
4. JSON / Schema validation enforcement (SRS REQ-TI-2).
5. Complete security gate: cannot bypass Inspector or Anonymizer.
6. Silent drop on malicious/invalid packets (SRS REQ-API-5: no rejection ACK).
7. TLS 1.2+ enforcement and mTLS client certificate configuration.
8. Local development mode vs production mode configuration validation.
9. Graceful reconnect and connection error handling.
10. Backpressure / queue-full handling.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import ssl
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sigs_rs.common.constants import AuditEvent
from sigs_rs.common.exceptions import (
    AnonymizerError,
    ConfigError,
    MalformedPayloadError,
    PayloadTooLargeError,
)
from sigs_rs.config import Settings
from sigs_rs.ingestion.buffer import IngestionQueue
from sigs_rs.ingestion.mqtt_sub import MQTTSubscriber, create_mqtt_tls_context
from sigs_rs.management.audit_logger import AuditLogger
from sigs_rs.middleware.anonymizer import PrivacyAnonymizer, _find_keys
from sigs_rs.middleware.inspector import AnomalousPacketInspector, VerificationResult
from sigs_rs.middleware.payload_validator import PayloadValidator
from sigs_rs.middleware.pipeline import MiddlewarePipeline
from tests.mock_sliding_window import MockAsyncSlidingWindow as SlidingWindow
from sigs_rs.persistence.circuit_breaker import CircuitBreaker
from sigs_rs.persistence.database import InMemoryDatabasePool
from sigs_rs.persistence.repository import TelemetryRepository
from sigs_rs.telemetry.validator import TelemetryValidator


# ─── Fixtures ─────────────────────────────────────────────────────────────────

class MockMqttMessage:
    def __init__(self, topic: str, payload: bytes | str) -> None:
        self.topic = topic
        self.payload = payload


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        app_env="development",
        mqtt_broker_host="localhost",
        mqtt_broker_port=1883,
        mqtt_use_tls=False,
        mqtt_topic_prefix="telemetry/#",
        mqtt_client_id="test-gateway-client",
        max_payload_size_bytes=524_288,  # 512 KB
    )


@pytest.fixture
def queue() -> IngestionQueue:
    return IngestionQueue(max_size=50)


@pytest.fixture
async def db_pool() -> InMemoryDatabasePool:
    pool = InMemoryDatabasePool()
    await pool.connect()
    yield pool
    await pool.disconnect()


@pytest.fixture
def repository(db_pool) -> TelemetryRepository:
    return TelemetryRepository(db_pool)


@pytest.fixture
def audit_logger(repository) -> AuditLogger:
    return AuditLogger(repository=repository)


@pytest.fixture
def pipeline(queue, repository, audit_logger) -> MiddlewarePipeline:
    window = SlidingWindow(window_seconds=10, max_packets=50)
    validator = PayloadValidator()
    inspector = AnomalousPacketInspector(window, validator, audit_logger=audit_logger)
    anonymizer = PrivacyAnonymizer()
    circuit_breaker = CircuitBreaker(failure_threshold_ms=2000, cooldown_seconds=30)

    return MiddlewarePipeline(
        queue=queue,
        inspector=inspector,
        anonymizer=anonymizer,
        repository=repository,
        circuit_breaker=circuit_breaker,
        audit_logger=audit_logger,
    )


@pytest.fixture
def mqtt_sub(test_settings, queue, audit_logger) -> MQTTSubscriber:
    validator = TelemetryValidator(max_bytes=test_settings.max_payload_size_bytes)
    return MQTTSubscriber(
        settings=test_settings,
        queue=queue,
        validator=validator,
        audit_logger=audit_logger,
    )


# ─── 1. End-to-End Pipeline Ingestion Tests ───────────────────────────────────

class TestMQTTPipelineIngestion:
    """Verifies MQTT messages flowing through the complete pipeline."""

    @pytest.mark.asyncio
    async def test_valid_mqtt_telemetry_processed_and_persisted(
        self, mqtt_sub, queue, pipeline, repository
    ):
        """
        Valid MQTT packet:
        MQTT -> Size Gate -> Schema Validator -> Queue -> Inspector -> Anonymizer -> DB.
        """
        raw_payload = {
            "sensor_id": "sensor-mqtt-001",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "resource_type": "water",
            "value": 31.4,
            "unit": "liters_per_min",
            "metadata": {"firmware_version": "2.0.1", "battery_level": 95},
            "water_ext": {"pressure_bar": 2.8, "ph_level": 7.4},
            "geo": {"latitude": 12.9716, "longitude": 80.2209},
            "mac_address": "AA:BB:CC:11:22:33",
            "device_id": "meter-mqtt-001",
        }
        msg = MockMqttMessage(
            topic="telemetry/water/sensor-mqtt-001",
            payload=json.dumps(raw_payload).encode("utf-8"),
        )

        # 1. Message handled by MQTT subscriber
        enqueued = await mqtt_sub.handle_message(msg)
        assert enqueued is True
        assert queue.depth == 1
        assert mqtt_sub.stats["enqueued"] == 1

        # 2. Pipeline consumes packet from queue
        packet = await queue.get()
        try:
            persisted = await pipeline.process_packet(packet)
        finally:
            queue.task_done()

        assert persisted is True

        # 3. Verify packet is in the database
        readings = await repository.get_by_sensor("sensor-mqtt-001")
        assert len(readings) == 1
        record = readings[0]
        assert record["sensor_id"] == "sensor-mqtt-001"
        assert record["value"] == 31.4
        assert record["resource_type"] == "water"

        # 4. Strict check: PII must NOT exist in the persisted record
        assert "latitude" not in record
        assert "longitude" not in record
        assert "mac_address" not in record
        assert "device_id" not in record
        assert _find_keys(record, {"latitude", "longitude", "mac_address", "device_id", "geo"}) == []


# ─── 2. Payload Size Enforcement (SRS REQ-TI-3: 512 KB) ──────────────────────

class TestMQTTPayloadSizeEnforcement:
    """Enforces 512 KB payload size limit before schema parsing."""

    @pytest.mark.asyncio
    async def test_oversized_payload_dropped_and_audited(self, mqtt_sub, queue, repository):
        """MQTT message > 512 KB must be dropped immediately with no rejection ACK."""
        oversized_bytes = b'{"sensor_id": "huge"}' + b"0" * (524_288 + 1)
        msg = MockMqttMessage(topic="telemetry/energy/sensor-large", payload=oversized_bytes)

        accepted = await mqtt_sub.handle_message(msg)
        assert accepted is False  # Silent drop

        # Must not enter queue
        assert queue.depth == 0
        assert mqtt_sub.stats["dropped_size"] == 1

        # Must be audit logged
        audit_logs = await repository.get_audit_logs()
        matching = [l for l in audit_logs if l["event_type"] == AuditEvent.PACKET_DROPPED_OVERSIZED]
        assert len(matching) == 1
        assert matching[0]["severity"] == "WARN"

    @pytest.mark.asyncio
    async def test_boundary_payload_size(self, mqtt_sub):
        """Payload up to exactly 512 KB passes the size gate."""
        # Exact 512 KB payload (malformed JSON but passes size check)
        exact_bytes = b" " * 524_288
        msg = MockMqttMessage(topic="telemetry/test", payload=exact_bytes)
        # Size gate passes, fails at JSON parser -> dropped_malformed
        await mqtt_sub.handle_message(msg)
        assert mqtt_sub.stats["dropped_size"] == 0
        assert mqtt_sub.stats["dropped_malformed"] == 1


# ─── 3. JSON & Schema Validation Enforcement (SRS REQ-TI-2) ──────────────────

class TestMQTTSchemaValidation:
    """Enforces JSON schema validation on incoming MQTT traffic."""

    @pytest.mark.asyncio
    async def test_malformed_json_dropped_and_audited(self, mqtt_sub, queue, repository):
        """Non-JSON bytes are dropped silently and audited."""
        msg = MockMqttMessage(topic="telemetry/water/bad", payload=b"INVALID_NOT_JSON{{")
        accepted = await mqtt_sub.handle_message(msg)

        assert accepted is False
        assert queue.depth == 0
        assert mqtt_sub.stats["dropped_malformed"] == 1

        audit_logs = await repository.get_audit_logs()
        matching = [l for l in audit_logs if l["event_type"] == AuditEvent.PACKET_DROPPED_MALFORMED]
        assert len(matching) == 1

    @pytest.mark.asyncio
    async def test_invalid_resource_type_dropped(self, mqtt_sub, queue):
        """Payload with unrecognized resource_type is rejected."""
        payload = {
            "sensor_id": "sensor-001",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "resource_type": "quantum_flux",  # Invalid
            "value": 10.0,
            "unit": "liters_per_min",
        }
        msg = MockMqttMessage(topic="telemetry/quantum", payload=json.dumps(payload))
        assert await mqtt_sub.handle_message(msg) is False
        assert queue.depth == 0
        assert mqtt_sub.stats["dropped_malformed"] == 1

    @pytest.mark.asyncio
    async def test_invalid_unit_for_resource_type_dropped(self, mqtt_sub, queue):
        """Payload with mismatched unit (e.g. kWh for water) is rejected."""
        payload = {
            "sensor_id": "sensor-001",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "resource_type": "water",
            "value": 10.0,
            "unit": "kWh",  # Invalid unit for water
        }
        msg = MockMqttMessage(topic="telemetry/water/sensor-001", payload=json.dumps(payload))
        assert await mqtt_sub.handle_message(msg) is False
        assert queue.depth == 0
        assert mqtt_sub.stats["dropped_malformed"] == 1


# ─── 4. Security Gate Enforcement (Cannot Bypass Inspector or Anonymizer) ─────

class TestMQTTSecurityGate:
    """Guarantees that telemetry from MQTT cannot bypass Inspector or Anonymizer."""

    @pytest.mark.asyncio
    async def test_mqtt_rate_limit_packet_dropped_by_inspector(
        self, mqtt_sub, queue, pipeline, repository
    ):
        """
        Packets from MQTT exceeding 50 pkt / 10s are dropped by the Inspector
        and NEVER reach the Anonymizer or Database.
        """
        sensor = "sensor-mqtt-flooder"

        # Ingest 50 valid packets through MQTT and pipeline
        for i in range(50):
            payload = {
                "sensor_id": sensor,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "resource_type": "energy",
                "value": float(i),
                "unit": "kWh",
            }
            msg = MockMqttMessage(topic=f"telemetry/energy/{sensor}", payload=json.dumps(payload))
            await mqtt_sub.handle_message(msg)
            packet = await queue.get()
            await pipeline.process_packet(packet)
            queue.task_done()

        assert await repository.count_readings(sensor) == 50

        # Send 51st packet via MQTT
        flood_payload = {
            "sensor_id": sensor,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "resource_type": "energy",
            "value": 99.0,
            "unit": "kWh",
        }
        flood_msg = MockMqttMessage(topic=f"telemetry/energy/{sensor}", payload=json.dumps(flood_payload))
        await mqtt_sub.handle_message(flood_msg)
        flood_packet = await queue.get()
        persisted = await pipeline.process_packet(flood_packet)
        queue.task_done()

        # Dropped by Inspector!
        assert persisted is False
        assert await repository.count_readings(sensor) == 50

    @pytest.mark.asyncio
    async def test_mqtt_impossible_value_dropped_by_inspector(
        self, mqtt_sub, queue, pipeline, repository
    ):
        """
        Packets from MQTT with negative power or AQI > 500 are dropped by Inspector.
        """
        bad_payload = {
            "sensor_id": "sensor-mqtt-impossible",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "resource_type": "energy",
            "value": -42.0,  # Negative power impossible
            "unit": "kWh",
        }
        msg = MockMqttMessage(topic="telemetry/energy/sensor-impossible", payload=json.dumps(bad_payload))
        await mqtt_sub.handle_message(msg)

        packet = await queue.get()
        persisted = await pipeline.process_packet(packet)
        queue.task_done()

        assert persisted is False
        assert await repository.count_readings("sensor-mqtt-impossible") == 0

    @pytest.mark.asyncio
    async def test_mqtt_anonymizer_failure_dropped_fail_secure(
        self, mqtt_sub, queue, pipeline, repository
    ):
        """
        If anonymization fails on an MQTT packet, it fails secure and is NOT persisted.
        """
        payload = {
            "sensor_id": "sensor-anon-fail",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "resource_type": "water",
            "value": 15.0,
            "unit": "liters_per_min",
        }
        msg = MockMqttMessage(topic="telemetry/water/sensor-anon-fail", payload=json.dumps(payload))
        await mqtt_sub.handle_message(msg)

        packet = await queue.get()
        with patch.object(pipeline._anonymizer, "anonymize", side_effect=AnonymizerError("Masking corrupted")):
            import pytest
            with pytest.raises(RuntimeError, match="Fail-closed"):
                await pipeline.process_packet(packet)
        queue.task_done()

        # Database must be empty for this sensor
        assert await repository.count_readings("sensor-anon-fail") == 0


# ─── 5. TLS 1.2+ & mTLS Certificate Configuration Tests ───────────────────────

class TestMQTTTLSConfiguration:
    """Verifies TLS 1.2+ requirement and client certificate authentication (mTLS)."""

    def test_tls_context_enforces_tls_1_2_minimum(self):
        """create_mqtt_tls_context must set minimum_version to TLSv1_2."""
        ctx = create_mqtt_tls_context()
        assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2

    def test_tls_context_with_certificates(self):
        """Loads CA cert and client cert chain for mTLS when files exist."""
        certs_dir = Path(__file__).parents[2] / "certs"
        ca_file = certs_dir / "ca.crt"
        client_cert = certs_dir / "client.crt"
        client_key = certs_dir / "client.key"

        if ca_file.is_file() and client_cert.is_file() and client_key.is_file():
            ctx = create_mqtt_tls_context(
                ca_cert_path=str(ca_file),
                client_cert_path=str(client_cert),
                client_key_path=str(client_key),
                insecure_skip_verify=False,
            )
            assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2
            assert ctx.verify_mode == ssl.CERT_REQUIRED
            assert ctx.check_hostname is True

    def test_insecure_skip_verify_for_development(self):
        """Development mode permits skipping hostname verification for self-signed certificates."""
        ctx = create_mqtt_tls_context(insecure_skip_verify=True)
        assert ctx.check_hostname is False
        assert ctx.verify_mode == ssl.CERT_NONE


# ─── 6. Local Development Mode vs Production Mode Configuration ───────────────

class TestMQTTEnvironmentConfig:
    """Verifies that development mode allows plain MQTT while production requires TLS."""

    def test_development_mode_allows_insecure_mqtt(self):
        """Development environment accepts plain MQTT on port 1883."""
        s = Settings(
            app_env="development",
            mqtt_broker_host="localhost",
            mqtt_broker_port=1883,
            mqtt_use_tls=False,
        )
        assert s.app_env == "development"
        assert s.mqtt_use_tls is False
        assert s.mqtt_broker_port == 1883

    def test_production_mode_rejects_insecure_mqtt(self):
        """Production environment rejects plain MQTT without TLS (SRS REQ-TI-1, REQ-SE-1)."""
        with pytest.raises(ConfigError, match="Production configuration requires TLS"):
            Settings(
                app_env="production",
                mqtt_broker_host="mqtt.enterprise.corp",
                mqtt_broker_port=1883,
                mqtt_use_tls=False,
            )

    def test_production_mode_rejects_plain_port(self):
        """Production environment requires TLS port 8883."""
        with pytest.raises(ConfigError, match="requires MQTT TLS port 8883"):
            Settings(
                app_env="production",
                mqtt_broker_host="mqtt.enterprise.corp",
                mqtt_broker_port=1883,
                mqtt_use_tls=True,
            )

    def test_production_mode_rejects_disabled_cert_verification(self):
        """Production cannot disable certificate verification."""
        with pytest.raises(ConfigError, match="cannot disable TLS certificate verification"):
            Settings(
                app_env="production",
                mqtt_broker_host="mqtt.enterprise.corp",
                mqtt_broker_port=8883,
                mqtt_use_tls=True,
                mqtt_insecure_skip_verify=True,
            )

    def test_production_mode_valid_configuration(self):
        """Valid production TLS configuration passes."""
        s = Settings(
            app_env="production",
            mqtt_broker_host="mqtt.enterprise.corp",
            mqtt_broker_port=8883,
            mqtt_use_tls=True,
            mqtt_insecure_skip_verify=False,
        )
        assert s.app_env == "production"
        assert s.mqtt_use_tls is True
        assert s.mqtt_broker_port == 8883

    def test_unified_mqtt_broker_url_parsing(self):
        """Settings parses mqtt_broker_url (mqtt:// and mqtts://)."""
        s_plain = Settings(
            app_env="development",
            mqtt_broker_url="mqtt://my-broker:1884",
        )
        assert s_plain.mqtt_broker_host == "my-broker"
        assert s_plain.mqtt_broker_port == 1884
        assert s_plain.mqtt_use_tls is False

        s_tls = Settings(
            app_env="development",
            mqtt_broker_url="mqtts://secure-broker:8884",
        )
        assert s_tls.mqtt_broker_host == "secure-broker"
        assert s_tls.mqtt_broker_port == 8884
        assert s_tls.mqtt_use_tls is True


# ─── 7. Connection Error Handling & Graceful Reconnect ─────────────────────────

class TestMQTTConnectionHandling:
    """Verifies graceful reconnect and error handling."""

    @pytest.mark.asyncio
    async def test_client_kwargs_construction(self, test_settings, queue):
        """Constructs appropriate arguments for aiomqtt.Client."""
        sub = MQTTSubscriber(test_settings, queue)
        kwargs = sub._build_client_kwargs()
        assert kwargs["hostname"] == "localhost"
        assert kwargs["port"] == 1883
        assert kwargs["identifier"] == "test-gateway-client"
        assert "tls_context" not in kwargs

        # With TLS enabled
        tls_settings = Settings(
            app_env="development",
            mqtt_use_tls=True,
            mqtt_broker_port=8883,
        )
        sub_tls = MQTTSubscriber(tls_settings, queue)
        tls_kwargs = sub_tls._build_client_kwargs()
        assert "tls_context" in tls_kwargs
        assert isinstance(tls_kwargs["tls_context"], ssl.SSLContext)

    @pytest.mark.asyncio
    async def test_reconnect_backoff_on_broker_failure(self, test_settings, queue):
        """Subscriber retries connection with backoff when broker is unreachable."""
        sub = MQTTSubscriber(
            settings=test_settings,
            queue=queue,
            reconnect_initial_interval=0.01,
            reconnect_max_interval=0.05,
        )

        attempts = 0

        class MockFailingClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                nonlocal attempts
                attempts += 1
                if attempts >= 3:
                    await sub.stop()
                raise ConnectionRefusedError("Broker offline")

            async def __aexit__(self, *args):
                pass

        with patch("aiomqtt.Client", MockFailingClient):
            task = asyncio.create_task(sub.start())
            await asyncio.wait_for(task, timeout=2.0)

        assert attempts >= 3
        assert not sub.is_connected
        assert not sub.is_running

    @pytest.mark.asyncio
    async def test_queue_full_backpressure_drop(self, test_settings):
        """When the ingestion queue is full, packet is dropped without crashing."""
        small_queue = IngestionQueue(max_size=1)
        sub = MQTTSubscriber(test_settings, small_queue)

        payload = {
            "sensor_id": "sensor-001",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "resource_type": "water",
            "value": 10.0,
            "unit": "liters_per_min",
        }
        msg = MockMqttMessage(topic="telemetry/water", payload=json.dumps(payload))

        # First message fills queue
        assert await sub.handle_message(msg) is True
        assert small_queue.depth == 1

        # Second message encounters backpressure
        assert await sub.handle_message(msg) is False
        assert sub.stats["dropped_queue_full"] == 1
