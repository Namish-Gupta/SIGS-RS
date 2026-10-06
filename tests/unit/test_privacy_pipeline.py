"""Tests for the Inspector -> Anonymizer persistence boundary."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sigs_rs.common.constants import AuditEvent
from sigs_rs.common.exceptions import AnonymizerError
from sigs_rs.ingestion.buffer import IngestionQueue
from sigs_rs.middleware.anonymizer import PrivacyAnonymizer, _find_keys
from sigs_rs.middleware.inspector import AnomalousPacketInspector, VerificationResult
from sigs_rs.middleware.payload_validator import PayloadValidator
from sigs_rs.middleware.pipeline import MiddlewarePipeline
from sigs_rs.middleware.sliding_window import SlidingWindow
from sigs_rs.persistence.circuit_breaker import CircuitBreaker
from sigs_rs.telemetry.models import RawTelemetryPacket, SanitizedTelemetryPacket


def _packet() -> RawTelemetryPacket:
    return RawTelemetryPacket(
        sensor_id="sensor-privacy-001",
        timestamp="2026-01-01T00:00:00Z",
        resource_type="water",
        value=12.0,
        unit="liters_per_min",
        geo={"latitude": 12.97, "longitude": 80.22},
        mac_address="AA:BB:CC:DD:EE:FF",
        device_id="water-meter-001",
    )


def _pipeline(inspector, anonymizer, repository, audit_logger) -> MiddlewarePipeline:
    return MiddlewarePipeline(
        queue=IngestionQueue(max_size=10),
        inspector=inspector,
        anonymizer=anonymizer,
        repository=repository,
        circuit_breaker=CircuitBreaker(2_000, 30),
        audit_logger=audit_logger,
    )


@pytest.mark.asyncio
async def test_pipeline_only_anonymizes_verified_packets():
    inspector = MagicMock()
    inspector.inspect.return_value = VerificationResult.INVALID_METRIC
    anonymizer = MagicMock()
    repository = MagicMock()
    repository.insert = AsyncMock()
    audit_logger = AsyncMock()

    persisted = await _pipeline(inspector, anonymizer, repository, audit_logger).process_packet(_packet())

    assert persisted is False
    anonymizer.anonymize.assert_not_called()
    repository.insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_pipeline_drops_and_audits_anonymizer_failure_without_persisting_raw():
    inspector = MagicMock()
    inspector.inspect.return_value = VerificationResult.VERIFIED
    anonymizer = MagicMock()
    anonymizer.anonymize.side_effect = AnonymizerError("unsafe payload structure")
    repository = MagicMock()
    repository.insert = AsyncMock()
    audit_logger = AsyncMock()

    persisted = await _pipeline(inspector, anonymizer, repository, audit_logger).process_packet(_packet())

    assert persisted is False
    repository.insert.assert_not_awaited()
    audit_logger.log.assert_awaited_once()
    assert audit_logger.log.call_args.args[0] == AuditEvent.ANONYMIZER_FAIL_SECURE
    assert audit_logger.log.call_args.kwargs["severity"] == "ERROR"
    assert "unsafe payload structure" in audit_logger.log.call_args.kwargs["detail"]["reason"]


@pytest.mark.asyncio
async def test_pipeline_persists_only_a_sanitized_representation():
    inspector = AnomalousPacketInspector(
        SlidingWindow(window_seconds=10, max_packets=50), PayloadValidator()
    )
    repository = MagicMock()
    repository.insert = AsyncMock()
    audit_logger = AsyncMock()
    packet = _packet()

    persisted = await _pipeline(
        inspector, PrivacyAnonymizer(), repository, audit_logger
    ).process_packet(packet)

    assert persisted is True
    stored = repository.insert.await_args.args[0]
    assert isinstance(stored, SanitizedTelemetryPacket)
    assert stored is not packet
    assert _find_keys(stored.model_dump(), {"geo", "latitude", "longitude", "mac_address", "device_id"}) == []
