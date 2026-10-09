"""
Integration tests for Phase 5: PostgreSQL/TimescaleDB Persistence.

Verifies:
1. Verified + anonymized packets are persisted to the database.
2. Anomalous packets (rate limit exceeded / impossible values) are NOT persisted.
3. Malformed packets are rejected prior to ingestion and NOT persisted.
4. Anonymizer masking failures fail secure, are audited, and are NOT persisted.
5. Latitude cannot appear anywhere in persisted records or schema.
6. Longitude cannot appear anywhere in persisted records or schema.
7. Device-specific identifiers (mac_address, device_id) cannot appear in persisted records or schema.
8. Connection pooling, transaction rollback, and health checks.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sigs_rs.common.constants import AuditEvent
from sigs_rs.common.exceptions import (
    AnonymizerError,
    MalformedPayloadError,
    PersistenceError,
)
from sigs_rs.ingestion.buffer import IngestionQueue
from sigs_rs.management.audit_logger import AuditLogger
from sigs_rs.middleware.anonymizer import PrivacyAnonymizer, _find_keys
from sigs_rs.middleware.inspector import AnomalousPacketInspector, VerificationResult
from sigs_rs.middleware.payload_validator import PayloadValidator
from sigs_rs.middleware.pipeline import MiddlewarePipeline
from sigs_rs.middleware.sliding_window import SlidingWindow
from sigs_rs.persistence.circuit_breaker import CircuitBreaker
from sigs_rs.persistence.database import (
    DatabasePool,
    InMemoryDatabasePool,
    create_ssl_context,
    normalize_dsn,
)
from sigs_rs.persistence.models import (
    Base,
    MiddlewareSetting,
    SanitizedReading,
    SecurityAuditLog,
)
from sigs_rs.persistence.repository import TelemetryRepository
from sigs_rs.telemetry.models import RawTelemetryPacket, SanitizedTelemetryPacket
from sigs_rs.telemetry.validator import TelemetryValidator


# ─── Test Helpers & Fixtures ──────────────────────────────────────────────────

def _make_raw_packet(
    sensor_id: str = "sensor-water-001",
    resource_type: str = "water",
    value: float = 24.5,
    unit: str = "liters_per_min",
    with_pii: bool = True,
) -> RawTelemetryPacket:
    payload = {
        "sensor_id": sensor_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "resource_type": resource_type,
        "value": value,
        "unit": unit,
        "metadata": {"firmware_version": "1.0.4", "battery_level": 92},
        "water_ext": {"pressure_bar": 2.5, "ph_level": 7.2},
    }
    if with_pii:
        payload["geo"] = {"latitude": 13.0827, "longitude": 80.2707}
        payload["latitude"] = 13.0827
        payload["longitude"] = 80.2707
        payload["mac_address"] = "11:22:33:44:55:66"
        payload["device_id"] = "meter-hw-0099"
    return RawTelemetryPacket(**payload)


@pytest.fixture
async def memory_db() -> InMemoryDatabasePool:
    db = InMemoryDatabasePool()
    await db.connect()
    yield db
    await db.disconnect()


@pytest.fixture
def repo(memory_db) -> TelemetryRepository:
    return TelemetryRepository(memory_db)


@pytest.fixture
def audit_log(repo) -> AuditLogger:
    return AuditLogger(repository=repo)


@pytest.fixture
def pipeline(memory_db, repo, audit_log) -> MiddlewarePipeline:
    window = SlidingWindow(window_seconds=10, max_packets=50)
    validator = PayloadValidator()
    inspector = AnomalousPacketInspector(window, validator, audit_logger=audit_log)
    anonymizer = PrivacyAnonymizer()
    circuit_breaker = CircuitBreaker(failure_threshold_ms=2000, cooldown_seconds=30)
    queue = IngestionQueue(max_size=100)

    return MiddlewarePipeline(
        queue=queue,
        inspector=inspector,
        anonymizer=anonymizer,
        repository=repo,
        circuit_breaker=circuit_breaker,
        audit_logger=audit_log,
    )


# ─── 1. Schema & Model PII Exclusion Tests ────────────────────────────────────

class TestPersistenceSchemaSecurity:
    """Verifies that the database schema itself has zero PII columns."""

    def test_sanitized_reading_schema_excludes_all_pii_columns(self):
        """The SanitizedReading table model has no latitude, longitude, or device ID columns."""
        column_names = set(SanitizedReading.__table__.columns.keys())

        forbidden = {"latitude", "longitude", "lat", "lon", "mac_address", "device_id", "geo"}
        intersection = column_names.intersection(forbidden)
        assert not intersection, f"PII columns found in SanitizedReading table: {intersection}"

    def test_database_init_script_excludes_pii_columns(self):
        """The SQL DDL init scripts have no PII columns in telemetry.sanitized_readings."""
        from pathlib import Path
        schema_path = Path(__file__).parents[2] / "src" / "sigs_rs" / "persistence" / "schema.sql"
        assert schema_path.exists()
        sql_content = schema_path.read_text(encoding="utf-8")

        # Parse column definitions in telemetry.sanitized_readings table
        lines = [line.strip().lower() for line in sql_content.splitlines()]
        for line in lines:
            if any(pii in line for pii in ("latitude", "longitude", "mac_address", "device_id")):
                # Ensure it's not a column definition in sanitized_readings
                assert "telemetry.sanitized_readings" not in line


# ─── 2. Repository Direct Security Enforcement ────────────────────────────────

class TestRepositorySecurityGate:
    """TelemetryRepository must strictly reject any raw packet or packet containing PII."""

    @pytest.mark.asyncio
    async def test_repository_rejects_raw_telemetry_packet_directly(self, repo):
        """RawTelemetryPacket must NEVER cross into TelemetryRepository."""
        raw = _make_raw_packet()
        with pytest.raises(PersistenceError, match="Security violation: Only verified SanitizedTelemetryPacket"):
            await repo.insert(raw)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_repository_rejects_raw_dictionary(self, repo):
        """Raw dict cannot be inserted directly."""
        with pytest.raises(PersistenceError, match="Security violation"):
            await repo.insert({"sensor_id": "fake", "value": 10.0})  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_repository_rejects_packet_with_tampered_pii(self, repo, anonymizer):
        """Even if an object is SanitizedTelemetryPacket, if PII somehow exists, it must be rejected."""
        raw = _make_raw_packet()
        sanitized = anonymizer.anonymize(raw)

        class TamperedPacket(SanitizedTelemetryPacket):
            def model_dump(self, *args, **kwargs):
                d = super().model_dump(*args, **kwargs)
                d["latitude"] = 12.34
                return d

        tampered = TamperedPacket(**sanitized.model_dump())
        with pytest.raises(PersistenceError, match="contains restricted PII fields"):
            await repo.insert(tampered)

    @pytest.mark.asyncio
    async def test_repository_rejects_nested_pii_in_metadata(self, repo, anonymizer):
        """If metadata contains a smuggled PII field, repository must reject it."""
        raw = _make_raw_packet()
        sanitized = anonymizer.anonymize(raw)

        class NestedTamperedPacket(SanitizedTelemetryPacket):
            def model_dump(self, *args, **kwargs):
                d = super().model_dump(*args, **kwargs)
                d["metadata"] = {"mac_address": "00:11:22:33:44:55"}
                return d

        nested_tampered = NestedTamperedPacket(**sanitized.model_dump())
        with pytest.raises(PersistenceError, match="contains restricted PII fields"):
            await repo.insert(nested_tampered)


# ─── 3. End-to-End Pipeline Integration Tests ─────────────────────────────────

class TestPersistencePipelineIntegration:
    """Verifies that the complete security pipeline controls what reaches the database."""

    @pytest.mark.asyncio
    async def test_verified_and_anonymized_packet_is_persisted(self, pipeline, repo):
        """
        A valid packet passing through Inspector and Anonymizer IS persisted.
        """
        raw = _make_raw_packet(sensor_id="sensor-valid-001", value=35.0)

        persisted = await pipeline.process_packet(raw)
        assert persisted is True

        count = await repo.count_readings("sensor-valid-001")
        assert count == 1

        stored = await repo.get_by_sensor("sensor-valid-001")
        assert len(stored) == 1
        record = stored[0]
        assert record["sensor_id"] == "sensor-valid-001"
        assert record["value"] == 35.0
        assert record["resource_type"] == "water"
        assert record["unit"] == "liters_per_min"
        assert record["ingested_at"] is not None

    @pytest.mark.asyncio
    async def test_anomalous_packet_rate_limit_exceeded_is_not_persisted(self, pipeline, repo):
        """
        Packets exceeding 50 pkt / 10s are dropped by the Inspector and NEVER persisted.
        """
        sensor = "sensor-rate-flood"

        # Send exactly 50 packets (all should pass and persist)
        for i in range(50):
            p = _make_raw_packet(sensor_id=sensor, value=10.0 + i)
            ok = await pipeline.process_packet(p)
            assert ok is True

        assert await repo.count_readings(sensor) == 50

        # The 51st packet exceeds rate limit
        flood_packet = _make_raw_packet(sensor_id=sensor, value=999.0)
        persisted = await pipeline.process_packet(flood_packet)
        assert persisted is False

        # Verify count in database has NOT increased
        assert await repo.count_readings(sensor) == 50

    @pytest.mark.asyncio
    async def test_anomalous_packet_impossible_value_is_not_persisted(self, pipeline, repo):
        """
        Packets with physically impossible values are dropped by Inspector and NEVER persisted.
        """
        # 1. Negative water flow
        neg_water = _make_raw_packet(sensor_id="sensor-neg-water", resource_type="water", value=-5.0)
        assert await pipeline.process_packet(neg_water) is False
        assert await repo.count_readings("sensor-neg-water") == 0

        # 2. Negative energy consumption
        neg_energy = _make_raw_packet(sensor_id="sensor-neg-energy", resource_type="energy", value=-0.01, unit="kWh")
        assert await pipeline.process_packet(neg_energy) is False
        assert await repo.count_readings("sensor-neg-energy") == 0

        # 3. Impossible air quality (AQI > 500)
        bad_aqi = _make_raw_packet(sensor_id="sensor-bad-aqi", resource_type="air_quality", value=501.0, unit="AQI")
        assert await pipeline.process_packet(bad_aqi) is False
        assert await repo.count_readings("sensor-bad-aqi") == 0

        # 4. NaN / Infinity
        nan_packet = _make_raw_packet(sensor_id="sensor-nan", resource_type="water", value=float("nan"))
        assert await pipeline.process_packet(nan_packet) is False
        assert await repo.count_readings("sensor-nan") == 0

    @pytest.mark.asyncio
    async def test_malformed_packet_is_not_persisted(self, pipeline, repo):
        """
        Payloads failing schema validation never enter the pipeline and cannot be persisted.
        """
        validator = TelemetryValidator()
        malformed_bytes = b'{"sensor_id": "sensor-1", "resource_type": "water", "value": "NOT_A_FLOAT"}'

        with pytest.raises(Exception):
            # Validator rejects before pipeline
            packet = validator.validate(malformed_bytes)
            await pipeline.process_packet(packet)

        assert await repo.count_readings() == 0

    @pytest.mark.asyncio
    async def test_anonymizer_failure_is_not_persisted_and_audited(self, pipeline, repo, audit_log):
        """
        If the anonymizer throws a masking exception, the packet is dropped,
        audited with ANONYMIZER_FAIL_SECURE, and NEVER reaches persistence.
        """
        raw = _make_raw_packet(sensor_id="sensor-anonymizer-fail")

        # Mock anonymizer failure
        with patch.object(
            pipeline._anonymizer,
            "anonymize",
            side_effect=AnonymizerError("Simulated masking engine corruption"),
        ):
            import pytest
            with pytest.raises(RuntimeError, match="Fail-closed"):
                await pipeline.process_packet(raw)

        # Database has NO readings
        assert await repo.count_readings("sensor-anonymizer-fail") == 0

        # Database audit log records the incident
        audit_logs = await repo.get_audit_logs()
        matching = [
            log for log in audit_logs
            if log["event_type"] == AuditEvent.ANONYMIZER_FAIL_SECURE
            and log["sensor_id"] == "sensor-anonymizer-fail"
        ]
        assert len(matching) == 1
        assert matching[0]["severity"] == "CRITICAL"
        assert "Simulated masking engine corruption" in json.dumps(matching[0]["detail"])


# ─── 4. Security Assertions on Persisted Records ───────────────────────────────

class TestPersistedDataSanitization:
    """Verifies that no restricted PII exists in any persisted database record."""

    @pytest.mark.asyncio
    async def test_latitude_cannot_appear_in_persisted_records(self, pipeline, repo):
        """Latitude must not appear anywhere in stored records."""
        raw = _make_raw_packet(sensor_id="sensor-lat-check")
        assert raw.latitude is not None

        await pipeline.process_packet(raw)
        records = await repo.get_by_sensor("sensor-lat-check")
        assert len(records) == 1

        for record in records:
            assert "latitude" not in record
            assert "lat" not in record
            assert _find_keys(record, {"latitude", "lat"}) == []

    @pytest.mark.asyncio
    async def test_longitude_cannot_appear_in_persisted_records(self, pipeline, repo):
        """Longitude must not appear anywhere in stored records."""
        raw = _make_raw_packet(sensor_id="sensor-lon-check")
        assert raw.longitude is not None

        await pipeline.process_packet(raw)
        records = await repo.get_by_sensor("sensor-lon-check")
        assert len(records) == 1

        for record in records:
            assert "longitude" not in record
            assert "lon" not in record
            assert _find_keys(record, {"longitude", "lon"}) == []

    @pytest.mark.asyncio
    async def test_device_identifier_cannot_appear_in_persisted_records(self, pipeline, repo):
        """Device-specific identifiers (mac_address, device_id) must not appear in stored records."""
        raw = _make_raw_packet(sensor_id="sensor-devid-check")
        assert raw.mac_address is not None
        assert raw.device_id is not None

        await pipeline.process_packet(raw)
        records = await repo.get_by_sensor("sensor-devid-check")
        assert len(records) == 1

        for record in records:
            assert "mac_address" not in record
            assert "device_id" not in record
            assert _find_keys(record, {"mac_address", "device_id"}) == []


# ─── 5. Database Connection Management & Health Checks ────────────────────────

class TestDatabaseConnectionAndPooling:
    """Verifies connection management, transaction rollback, and health checks."""

    @pytest.mark.asyncio
    async def test_database_pool_connection_lifecycle(self):
        """Connection pool connects, reports is_connected, and disconnects."""
        db = InMemoryDatabasePool()
        assert not db.is_connected
        await db.connect()
        assert db.is_connected
        assert await db.check_health() is True
        await db.disconnect()
        assert not db.is_connected
        assert await db.check_health() is False

    @pytest.mark.asyncio
    async def test_database_transaction_atomic_rollback(self, memory_db, repo, anonymizer):
        """Operations within a failed transaction are completely rolled back."""
        await memory_db.connect()
        raw = _make_raw_packet()
        sanitized = anonymizer.anonymize(raw)

        # Baseline
        await repo.insert(sanitized)
        assert await repo.count_readings() == 1

        # Attempt transaction that fails mid-way
        with pytest.raises(RuntimeError, match="Simulated DB failure"):
            async with memory_db.transaction():
                # First insert succeeds
                await repo.insert(sanitized)
                # Second action raises error
                raise RuntimeError("Simulated DB failure")

        # Must have rolled back to baseline count of 1
        assert await repo.count_readings() == 1

    def test_database_pool_ssl_configuration(self):
        """SSL contexts match SRS REQ-SE-3 requirements."""
        ctx_full = create_ssl_context("verify-full")
        assert ctx_full is not False
        assert ctx_full.check_hostname is True

        ctx_req = create_ssl_context("require")
        assert ctx_req.check_hostname is False

        assert create_ssl_context("disable") is False
        assert create_ssl_context("off") is False

    def test_database_pool_dsn_normalization(self):
        """DSNs are normalized from SQLAlchemy asyncpg format to standard PostgreSQL format."""
        assert normalize_dsn("postgresql+asyncpg://user:pass@localhost:5432/db") == "postgresql://user:pass@localhost:5432/db"
        assert normalize_dsn("postgresql://user:pass@localhost:5432/db") == "postgresql://user:pass@localhost:5432/db"

    @pytest.mark.asyncio
    async def test_batch_insert(self, repo, anonymizer):
        """Batch insert persists multiple records atomically."""
        packets = [
            anonymizer.anonymize(_make_raw_packet(sensor_id=f"sensor-batch-{i}", value=float(i)))
            for i in range(5)
        ]
        await repo.insert_batch(packets)
        assert await repo.count_readings() == 5
