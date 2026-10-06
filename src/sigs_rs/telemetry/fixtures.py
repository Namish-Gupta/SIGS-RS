"""
Representative telemetry payload fixtures.

Provides ready-to-use dicts (and JSON bytes) for both valid and invalid
payloads covering every resource type and every rejection path.

Used by:
  - Unit tests (test_telemetry_models.py, test_telemetry_validator.py)
  - Integration tests
  - Manual testing / REPL exploration
  - Smoke-test assertions in CI

Structure:
  VALID_PAYLOADS   — list of dicts, each parseable into RawTelemetryPacket
  INVALID_PAYLOADS — list of (label, bytes|dict) pairs that MUST be rejected
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _past(minutes: int = 5) -> str:
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

def _future(minutes: int = 10) -> str:
    dt = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── Valid payloads ────────────────────────────────────────────────────────────

VALID_WATER = {
    "sensor_id": "sensor-water-001",
    "timestamp": _past(1),
    "resource_type": "water",
    "value": 42.7,
    "unit": "liters_per_min",
    "metadata": {
        "firmware_version": "2.1.0",
        "battery_level": 87,
        "signal_strength_dbm": -65,
    },
    "water_ext": {
        "pressure_bar": 2.3,
        "ph_level": 7.1,
        "temperature_celsius": 18.5,
        "turbidity_ntu": 0.4,
    },
    "geo": {"latitude": 12.9716, "longitude": 80.2209},
    "mac_address": "AA:BB:CC:DD:EE:FF",
}

VALID_ENERGY = {
    "sensor_id": "sensor-energy-007",
    "timestamp": _past(2),
    "resource_type": "energy",
    "value": 3.82,
    "unit": "kWh",
    "metadata": {
        "firmware_version": "1.4.2",
        "battery_level": 55,
    },
    "energy_ext": {
        "voltage_v": 230.1,
        "current_a": 16.6,
        "power_factor": 0.98,
        "frequency_hz": 50.0,
    },
    "geo": {"latitude": 28.6139, "longitude": 77.2090},
    "mac_address": "11:22:33:44:55:66",
}

VALID_AIR_QUALITY = {
    "sensor_id": "sensor-aq-012",
    "timestamp": _now_utc(),
    "resource_type": "air_quality",
    "value": 152.0,
    "unit": "AQI",
    "metadata": {
        "firmware_version": "3.0.0",
        "battery_level": 100,
        "signal_strength_dbm": -70,
    },
    "air_quality_ext": {
        "pm25_ug_m3": 55.2,
        "pm10_ug_m3": 88.1,
        "co2_ppm": 415.0,
        "o3_ppb": 32.0,
        "temperature_celsius": 27.3,
        "humidity_percent": 62.0,
    },
    "geo": {"latitude": 19.0760, "longitude": 72.8777},
    "mac_address": "DE:AD:BE:EF:00:01",
}

VALID_MINIMAL_WATER = {
    # Only required fields — no PII, no metadata, no extended data
    "sensor_id": "sensor-min-001",
    "timestamp": _past(3),
    "resource_type": "water",
    "value": 10.0,
    "unit": "liters_per_min",
}

VALID_MINIMAL_ENERGY = {
    "sensor_id": "sensor-min-002",
    "timestamp": _past(3),
    "resource_type": "energy",
    "value": 0.5,
    "unit": "W",
}

VALID_MINIMAL_AQ = {
    "sensor_id": "sensor-min-003",
    "timestamp": _past(3),
    "resource_type": "air_quality",
    "value": 0.0,
    "unit": "AQI",
}

VALID_WATER_NO_PII = {
    # Valid packet without any PII — already anonymized by sensor (unusual but valid)
    "sensor_id": "sensor-nopii-001",
    "timestamp": _past(5),
    "resource_type": "water",
    "value": 25.5,
    "unit": "gallons_per_min",
}

VALID_ENERGY_ZERO = {
    # Zero energy reading is structurally valid (not an anomaly per schema)
    "sensor_id": "sensor-zero-001",
    "timestamp": _past(1),
    "resource_type": "energy",
    "value": 0.0,
    "unit": "kWh",
}

VALID_AQ_MAX_AQI = {
    # AQI = 500 is the structural maximum and IS valid at schema level.
    # The Inspector (Phase 3) handles REQ-API-3 > 500 detection.
    "sensor_id": "sensor-aqi-max",
    "timestamp": _past(1),
    "resource_type": "air_quality",
    "value": 500.0,
    "unit": "AQI",
}

VALID_FUTURE_WITHIN_TOLERANCE = {
    # 3 minutes in the future — within the 5-minute clock-skew tolerance
    "sensor_id": "sensor-skew-001",
    "timestamp": _future(3),
    "resource_type": "water",
    "value": 12.0,
    "unit": "liters_per_min",
}

VALID_WATER_WITH_UNKNOWN_FIELDS = {
    # Extra fields are silently ignored (ConfigDict extra="ignore")
    "sensor_id": "sensor-extra-001",
    "timestamp": _past(1),
    "resource_type": "water",
    "value": 5.0,
    "unit": "liters_per_min",
    "unknown_future_field": "ignored",
    "another_field": 42,
}

# Collected list for parametric testing
VALID_PAYLOADS: list[dict] = [
    VALID_WATER,
    VALID_ENERGY,
    VALID_AIR_QUALITY,
    VALID_MINIMAL_WATER,
    VALID_MINIMAL_ENERGY,
    VALID_MINIMAL_AQ,
    VALID_WATER_NO_PII,
    VALID_ENERGY_ZERO,
    VALID_AQ_MAX_AQI,
    VALID_FUTURE_WITHIN_TOLERANCE,
    VALID_WATER_WITH_UNKNOWN_FIELDS,
]


# ─── Invalid payload cases ────────────────────────────────────────────────────
# Each entry is (label: str, payload: bytes | dict)
# All must raise MalformedPayloadError or PayloadTooLargeError from the
# TelemetryValidator.

def _as_bytes(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8")

INVALID_PAYLOADS: list[tuple[str, bytes | dict]] = [

    # ── Missing required fields ───────────────────────────────────────────────
    (
        "missing_sensor_id",
        _as_bytes({
            "timestamp": _past(1),
            "resource_type": "water",
            "value": 10.0,
            "unit": "liters_per_min",
        }),
    ),
    (
        "missing_timestamp",
        _as_bytes({
            "sensor_id": "sensor-001",
            "resource_type": "water",
            "value": 10.0,
            "unit": "liters_per_min",
        }),
    ),
    (
        "missing_resource_type",
        _as_bytes({
            "sensor_id": "sensor-001",
            "timestamp": _past(1),
            "value": 10.0,
            "unit": "liters_per_min",
        }),
    ),
    (
        "missing_value",
        _as_bytes({
            "sensor_id": "sensor-001",
            "timestamp": _past(1),
            "resource_type": "water",
            "unit": "liters_per_min",
        }),
    ),
    (
        "missing_unit",
        _as_bytes({
            "sensor_id": "sensor-001",
            "timestamp": _past(1),
            "resource_type": "water",
            "value": 10.0,
        }),
    ),
    (
        "empty_payload",
        _as_bytes({}),
    ),

    # ── Wrong types ───────────────────────────────────────────────────────────
    (
        "sensor_id_is_integer",
        _as_bytes({
            "sensor_id": 12345,
            "timestamp": _past(1),
            "resource_type": "water",
            "value": 10.0,
            "unit": "liters_per_min",
        }),
    ),
    (
        "value_is_string",
        _as_bytes({
            "sensor_id": "sensor-001",
            "timestamp": _past(1),
            "resource_type": "water",
            "value": "forty-two",
            "unit": "liters_per_min",
        }),
    ),
    (
        "timestamp_not_iso8601",
        _as_bytes({
            "sensor_id": "sensor-001",
            "timestamp": "not-a-date",
            "resource_type": "water",
            "value": 10.0,
            "unit": "liters_per_min",
        }),
    ),
    (
        "timestamp_is_integer_epoch",
        # Pydantic will coerce int to datetime for unix timestamps — this
        # is actually accepted by Pydantic v2.  Documented here for clarity.
        # The validator should NOT reject it — leave as-is.
        # (This entry is intentionally commented out — it PASSES validation.)
        # _as_bytes({...}),
        # Instead, test an actual invalid type:
        _as_bytes({
            "sensor_id": "sensor-001",
            "timestamp": ["array", "not", "a", "date"],
            "resource_type": "water",
            "value": 10.0,
            "unit": "liters_per_min",
        }),
    ),

    # ── Invalid resource_type ─────────────────────────────────────────────────
    (
        "unknown_resource_type",
        _as_bytes({
            "sensor_id": "sensor-001",
            "timestamp": _past(1),
            "resource_type": "nuclear",
            "value": 10.0,
            "unit": "mSv",
        }),
    ),
    (
        "resource_type_empty_string",
        _as_bytes({
            "sensor_id": "sensor-001",
            "timestamp": _past(1),
            "resource_type": "",
            "value": 10.0,
            "unit": "liters_per_min",
        }),
    ),
    (
        "resource_type_null",
        _as_bytes({
            "sensor_id": "sensor-001",
            "timestamp": _past(1),
            "resource_type": None,
            "value": 10.0,
            "unit": "liters_per_min",
        }),
    ),

    # ── Invalid unit ──────────────────────────────────────────────────────────
    (
        "water_with_energy_unit",
        _as_bytes({
            "sensor_id": "sensor-001",
            "timestamp": _past(1),
            "resource_type": "water",
            "value": 10.0,
            "unit": "kWh",        # energy unit on a water packet
        }),
    ),
    (
        "energy_with_aq_unit",
        _as_bytes({
            "sensor_id": "sensor-001",
            "timestamp": _past(1),
            "resource_type": "energy",
            "value": 3.5,
            "unit": "AQI",        # air-quality unit on an energy packet
        }),
    ),
    (
        "air_quality_with_water_unit",
        _as_bytes({
            "sensor_id": "sensor-001",
            "timestamp": _past(1),
            "resource_type": "air_quality",
            "value": 120.0,
            "unit": "liters_per_min",
        }),
    ),
    (
        "completely_unknown_unit",
        _as_bytes({
            "sensor_id": "sensor-001",
            "timestamp": _past(1),
            "resource_type": "water",
            "value": 10.0,
            "unit": "furlongs_per_fortnight",
        }),
    ),

    # ── Field length violations ───────────────────────────────────────────────
    (
        "sensor_id_too_long",
        _as_bytes({
            "sensor_id": "x" * 65,
            "timestamp": _past(1),
            "resource_type": "water",
            "value": 10.0,
            "unit": "liters_per_min",
        }),
    ),
    (
        "sensor_id_empty_string",
        _as_bytes({
            "sensor_id": "",
            "timestamp": _past(1),
            "resource_type": "water",
            "value": 10.0,
            "unit": "liters_per_min",
        }),
    ),

    # ── Timestamp violations ──────────────────────────────────────────────────
    (
        "timestamp_too_far_in_future",
        _as_bytes({
            "sensor_id": "sensor-001",
            "timestamp": _future(10),   # 10 min > 5 min tolerance
            "resource_type": "water",
            "value": 10.0,
            "unit": "liters_per_min",
        }),
    ),

    # ── Geo sub-model violations ──────────────────────────────────────────────
    (
        "geo_latitude_out_of_range",
        _as_bytes({
            "sensor_id": "sensor-001",
            "timestamp": _past(1),
            "resource_type": "water",
            "value": 10.0,
            "unit": "liters_per_min",
            "geo": {"latitude": 91.0, "longitude": 0.0},
        }),
    ),
    (
        "geo_longitude_out_of_range",
        _as_bytes({
            "sensor_id": "sensor-001",
            "timestamp": _past(1),
            "resource_type": "water",
            "value": 10.0,
            "unit": "liters_per_min",
            "geo": {"latitude": 0.0, "longitude": 181.0},
        }),
    ),
    (
        "geo_missing_longitude",
        _as_bytes({
            "sensor_id": "sensor-001",
            "timestamp": _past(1),
            "resource_type": "water",
            "value": 10.0,
            "unit": "liters_per_min",
            "geo": {"latitude": 12.97},     # longitude is required in GeoLocation
        }),
    ),

    # ── Extended sub-model violations ────────────────────────────────────────
    (
        "water_ext_ph_out_of_range",
        _as_bytes({
            "sensor_id": "sensor-001",
            "timestamp": _past(1),
            "resource_type": "water",
            "value": 10.0,
            "unit": "liters_per_min",
            "water_ext": {"ph_level": 15.0},    # pH must be ≤ 14
        }),
    ),
    (
        "energy_ext_power_factor_exceeds_one",
        _as_bytes({
            "sensor_id": "sensor-001",
            "timestamp": _past(1),
            "resource_type": "energy",
            "value": 3.5,
            "unit": "kWh",
            "energy_ext": {"power_factor": 1.5},    # must be ≤ 1
        }),
    ),
    (
        "aq_ext_humidity_over_100",
        _as_bytes({
            "sensor_id": "sensor-001",
            "timestamp": _past(1),
            "resource_type": "air_quality",
            "value": 120.0,
            "unit": "AQI",
            "air_quality_ext": {"humidity_percent": 110.0},    # must be ≤ 100
        }),
    ),
    (
        "battery_level_over_100",
        _as_bytes({
            "sensor_id": "sensor-001",
            "timestamp": _past(1),
            "resource_type": "water",
            "value": 10.0,
            "unit": "liters_per_min",
            "metadata": {"battery_level": 150},     # must be ≤ 100
        }),
    ),
    (
        "battery_level_negative",
        _as_bytes({
            "sensor_id": "sensor-001",
            "timestamp": _past(1),
            "resource_type": "water",
            "value": 10.0,
            "unit": "liters_per_min",
            "metadata": {"battery_level": -1},      # must be ≥ 0
        }),
    ),

    # ── JSON structure violations ─────────────────────────────────────────────
    (
        "malformed_json",
        b'{"sensor_id": "s1", "timestamp": "2026-01-01T00:00:00Z", BROKEN',
    ),
    (
        "json_array_not_object",
        b'[{"sensor_id": "s1"}]',
    ),
    (
        "empty_bytes",
        b"",
    ),
    (
        "not_json_at_all",
        b"hello world",
    ),
    (
        "null_json",
        b"null",
    ),
]


# ─── Size-violation fixture ────────────────────────────────────────────────────
def make_oversized_payload(max_bytes: int = 524_288) -> bytes:
    """Return a valid-structure JSON payload that exceeds max_bytes."""
    base = {
        "sensor_id": "sensor-big",
        "timestamp": _past(1),
        "resource_type": "water",
        "value": 1.0,
        "unit": "liters_per_min",
        "metadata": {"note": ""},
    }
    base["metadata"]["note"] = "X" * (max_bytes + 1)
    return json.dumps(base).encode("utf-8")
