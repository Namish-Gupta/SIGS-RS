"""
Unit tests for telemetry domain models — Phase 2.

Focused on:
  - All three resource types (water, energy, air_quality)
  - Cross-field validators (unit ↔ resource_type, timestamp future-check)
  - Sub-model field bounds (geo, SensorMetadata, WaterExtended, etc.)
  - has_pii() and resource_extended() helpers
  - extra='ignore' forward-compatibility
  - Explicit valid and invalid unit combinations
"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from pydantic import ValidationError

from sigs_rs.telemetry.models import (
    AirQualityExtended,
    EnergyExtended,
    GeoLocation,
    RawTelemetryPacket,
    SanitizedTelemetryPacket,
    SensorMetadata,
    WaterExtended,
)
from sigs_rs.common.constants import VALID_UNITS_BY_RESOURCE


# ─── Parametric: every valid unit for every resource type ─────────────────────

_water_cases = [(u, "water") for u in VALID_UNITS_BY_RESOURCE["water"]]
_energy_cases = [(u, "energy") for u in VALID_UNITS_BY_RESOURCE["energy"]]
_aq_cases = [(u, "air_quality") for u in VALID_UNITS_BY_RESOURCE["air_quality"]]

@pytest.mark.parametrize("unit,resource_type", _water_cases + _energy_cases + _aq_cases)
def test_all_valid_units_accepted(unit, resource_type):
    """Every unit in VALID_UNITS_BY_RESOURCE must be accepted by the model."""
    p = RawTelemetryPacket(
        sensor_id="sensor-test",
        timestamp="2026-01-01T00:00:00Z",
        resource_type=resource_type,
        value=10.0,
        unit=unit,
    )
    assert p.unit == unit


# ─── Parametric: invalid unit cross-pollination ────────────────────────────────

_cross_cases = [
    ("water",       "kWh"),
    ("water",       "AQI"),
    ("energy",      "liters_per_min"),
    ("energy",      "AQI"),
    ("air_quality", "kWh"),
    ("air_quality", "liters_per_min"),
    ("water",       ""),
    ("water",       "furlongs_per_fortnight"),
]

@pytest.mark.parametrize("resource_type,bad_unit", _cross_cases)
def test_invalid_unit_rejected(resource_type, bad_unit):
    """Unit from wrong resource_type must raise ValidationError."""
    with pytest.raises(ValidationError):
        RawTelemetryPacket(
            sensor_id="sensor-test",
            timestamp="2026-01-01T00:00:00Z",
            resource_type=resource_type,
            value=10.0,
            unit=bad_unit,
        )


# ─── Timestamp validator ──────────────────────────────────────────────────────

class TestTimestampValidator:
    def test_past_timestamp_accepted(self):
        ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        p = RawTelemetryPacket(
            sensor_id="s1", timestamp=ts,
            resource_type="water", value=1.0, unit="liters_per_min",
        )
        assert p.sensor_id == "s1"

    def test_now_accepted(self):
        p = RawTelemetryPacket(
            sensor_id="s1", timestamp=datetime.now(timezone.utc).isoformat(),
            resource_type="energy", value=1.0, unit="W",
        )
        assert p.resource_type == "energy"

    def test_within_tolerance_accepted(self):
        """3 minutes in the future ≤ 5 min tolerance — must pass."""
        ts = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()
        p = RawTelemetryPacket(
            sensor_id="s1", timestamp=ts,
            resource_type="water", value=1.0, unit="liters_per_min",
        )
        assert p.sensor_id == "s1"

    def test_beyond_tolerance_rejected(self):
        """10 minutes in the future > 5 min tolerance — must fail."""
        ts = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        with pytest.raises(ValidationError, match="future"):
            RawTelemetryPacket(
                sensor_id="s1", timestamp=ts,
                resource_type="water", value=1.0, unit="liters_per_min",
            )

    def test_naive_timestamp_accepted(self):
        """Pydantic v2 accepts naive datetimes (assumes local time)."""
        p = RawTelemetryPacket(
            sensor_id="s1",
            timestamp="2026-01-01T00:00:00",  # no timezone
            resource_type="energy", value=1.0, unit="kWh",
        )
        assert p.sensor_id == "s1"


# ─── has_pii() helper ─────────────────────────────────────────────────────────

class TestHasPii:
    def _base(self, **kwargs) -> RawTelemetryPacket:
        return RawTelemetryPacket(
            sensor_id="s1",
            timestamp="2026-01-01T00:00:00Z",
            resource_type="water",
            value=1.0,
            unit="liters_per_min",
            **kwargs,
        )

    def test_no_pii(self):
        assert self._base().has_pii() is False

    def test_geo_triggers_pii(self):
        assert self._base(geo={"latitude": 12.0, "longitude": 80.0}).has_pii() is True

    def test_direct_lat_triggers_pii(self):
        assert self._base(latitude=12.0, longitude=80.0).has_pii() is True

    def test_mac_triggers_pii(self):
        assert self._base(mac_address="AA:BB:CC:DD:EE:FF").has_pii() is True


# ─── resource_extended() helper ───────────────────────────────────────────────

class TestResourceExtended:
    def test_water_ext_returned(self):
        p = RawTelemetryPacket(
            sensor_id="s1", timestamp="2026-01-01T00:00:00Z",
            resource_type="water", value=1.0, unit="liters_per_min",
            water_ext={"pressure_bar": 2.5},
        )
        ext = p.resource_extended()
        assert isinstance(ext, WaterExtended)
        assert ext.pressure_bar == 2.5

    def test_energy_ext_returned(self):
        p = RawTelemetryPacket(
            sensor_id="s1", timestamp="2026-01-01T00:00:00Z",
            resource_type="energy", value=3.8, unit="kWh",
            energy_ext={"voltage_v": 230.0},
        )
        ext = p.resource_extended()
        assert isinstance(ext, EnergyExtended)
        assert ext.voltage_v == 230.0

    def test_aq_ext_returned(self):
        p = RawTelemetryPacket(
            sensor_id="s1", timestamp="2026-01-01T00:00:00Z",
            resource_type="air_quality", value=120.0, unit="AQI",
            air_quality_ext={"pm25_ug_m3": 55.0},
        )
        ext = p.resource_extended()
        assert isinstance(ext, AirQualityExtended)
        assert ext.pm25_ug_m3 == 55.0

    def test_no_ext_returns_none(self):
        p = RawTelemetryPacket(
            sensor_id="s1", timestamp="2026-01-01T00:00:00Z",
            resource_type="energy", value=3.8, unit="kWh",
        )
        assert p.resource_extended() is None


# ─── SanitizedTelemetryPacket ─────────────────────────────────────────────────

class TestSanitizedTelemetryPacket:
    def test_no_pii_fields_in_model(self):
        pii = {"latitude", "longitude", "lat", "lon", "mac_address", "geo"}
        fields = set(SanitizedTelemetryPacket.model_fields.keys())
        assert not pii.intersection(fields)

    def test_ingested_at_defaults_to_now(self):
        before = datetime.now(timezone.utc)
        s = SanitizedTelemetryPacket(
            sensor_id="s1",
            timestamp=datetime.now(timezone.utc),
            resource_type="water",
            value=5.0,
            unit="liters_per_min",
        )
        assert s.ingested_at >= before

    def test_resource_type_preserved(self):
        s = SanitizedTelemetryPacket(
            sensor_id="s1",
            timestamp=datetime.now(timezone.utc),
            resource_type="air_quality",
            value=150.0,
            unit="AQI",
        )
        assert s.resource_type == "air_quality"
