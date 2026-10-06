"""
Unit tests for telemetry fixtures — Phase 2.

Verifies that:
  - Every VALID_PAYLOAD parses cleanly through TelemetryValidator
  - Every INVALID_PAYLOAD raises MalformedPayloadError or PayloadTooLargeError
  - make_oversized_payload() produces a payload that exceeds the limit

This file acts as a contract test between the fixtures catalogue and the
validator.  If a fixture is added to fixtures.py but not accepted/rejected
as expected, this test will catch it immediately.
"""
from __future__ import annotations

import json
from typing import Union

import pytest

from sigs_rs.common.exceptions import MalformedPayloadError, PayloadTooLargeError
from sigs_rs.telemetry.fixtures import (
    INVALID_PAYLOADS,
    VALID_PAYLOADS,
    make_oversized_payload,
)
from sigs_rs.telemetry.models import RawTelemetryPacket
from sigs_rs.telemetry.validator import TelemetryValidator

validator = TelemetryValidator()


# ─── Valid fixtures ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("payload_dict", VALID_PAYLOADS)
def test_valid_fixture_accepted(payload_dict: dict):
    """Every entry in VALID_PAYLOADS must parse into RawTelemetryPacket."""
    raw = json.dumps(payload_dict).encode()
    result = validator.validate_bytes(raw)
    assert isinstance(result, RawTelemetryPacket)
    assert result.sensor_id == payload_dict["sensor_id"]
    assert result.resource_type == payload_dict["resource_type"]


@pytest.mark.parametrize("payload_dict", VALID_PAYLOADS)
def test_valid_fixture_validate_dict(payload_dict: dict):
    """validate_dict() must also accept every VALID_PAYLOAD."""
    result = validator.validate_dict(payload_dict)
    assert isinstance(result, RawTelemetryPacket)


@pytest.mark.parametrize("payload_dict", VALID_PAYLOADS)
def test_valid_fixture_within_size_limit(payload_dict: dict):
    """Every VALID_PAYLOAD must be within the 512 KB size limit."""
    raw = json.dumps(payload_dict).encode()
    assert len(raw) < validator.max_bytes, (
        f"Fixture '{payload_dict['sensor_id']}' exceeds size limit: {len(raw)} bytes"
    )


# ─── Invalid fixtures ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("label,payload", INVALID_PAYLOADS)
def test_invalid_fixture_rejected(label: str, payload: Union[bytes, dict]):
    """Every entry in INVALID_PAYLOADS must raise a validation error."""
    with pytest.raises((MalformedPayloadError, PayloadTooLargeError)):
        if isinstance(payload, bytes):
            validator.validate_bytes(payload)
        else:
            validator.validate_dict(payload)


# ─── Specific invalid fixture assertions ─────────────────────────────────────

def _get_invalid(label: str) -> bytes | dict:
    for name, p in INVALID_PAYLOADS:
        if name == label:
            return p
    raise KeyError(f"Invalid fixture '{label}' not found")


def test_missing_sensor_id_raises_malformed():
    payload = _get_invalid("missing_sensor_id")
    with pytest.raises(MalformedPayloadError):
        validator.validate_bytes(payload)


def test_missing_timestamp_raises_malformed():
    payload = _get_invalid("missing_timestamp")
    with pytest.raises(MalformedPayloadError):
        validator.validate_bytes(payload)


def test_unknown_resource_type_raises_malformed():
    payload = _get_invalid("unknown_resource_type")
    with pytest.raises(MalformedPayloadError):
        validator.validate_bytes(payload)


def test_water_with_energy_unit_raises_malformed():
    payload = _get_invalid("water_with_energy_unit")
    with pytest.raises(MalformedPayloadError, match="invalid for resource_type"):
        validator.validate_bytes(payload)


def test_future_timestamp_raises_malformed():
    payload = _get_invalid("timestamp_too_far_in_future")
    with pytest.raises(MalformedPayloadError, match="future"):
        validator.validate_bytes(payload)


def test_malformed_json_raises_malformed():
    payload = _get_invalid("malformed_json")
    with pytest.raises(MalformedPayloadError, match="JSON"):
        validator.validate_bytes(payload)


def test_empty_bytes_raises_malformed():
    payload = _get_invalid("empty_bytes")
    with pytest.raises(MalformedPayloadError):
        validator.validate_bytes(payload)


def test_geo_lat_out_of_range_raises():
    payload = _get_invalid("geo_latitude_out_of_range")
    with pytest.raises(MalformedPayloadError):
        validator.validate_bytes(payload)


def test_water_ext_ph_raises():
    payload = _get_invalid("water_ext_ph_out_of_range")
    with pytest.raises(MalformedPayloadError):
        validator.validate_bytes(payload)


# ─── Oversized payload ────────────────────────────────────────────────────────

def test_make_oversized_payload_raises_too_large():
    raw = make_oversized_payload(validator.max_bytes)
    assert len(raw) > validator.max_bytes
    with pytest.raises(PayloadTooLargeError):
        validator.validate_bytes(raw)


def test_make_oversized_payload_custom_limit():
    """Oversized for a tiny limit."""
    small_limit = 100
    tiny = TelemetryValidator(max_bytes=small_limit)
    raw = make_oversized_payload(small_limit)
    with pytest.raises(PayloadTooLargeError):
        tiny.validate_bytes(raw)
