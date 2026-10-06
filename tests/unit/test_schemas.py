"""Unit tests for schema validation (Phase 2 — enhanced models)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from sigs_rs.telemetry.models import (
    RawTelemetryPacket,
    SanitizedTelemetryPacket,
    GeoLocation,
    SensorMetadata,
    WaterExtended,
    EnergyExtended,
    AirQualityExtended,
)
# Verify legacy aliases still work
from sigs_rs.schemas import TelemetryPayload, SanitizedTelemetry


class TestLegacyAliases:
    """Ensure backward-compat aliases in schemas.py still work."""

    def test_telemetry_payload_is_raw_packet(self):
        assert TelemetryPayload is RawTelemetryPacket

    def test_sanitized_telemetry_is_sanitized_packet(self):
        assert SanitizedTelemetry is SanitizedTelemetryPacket


class TestRawTelemetryPacket:
    def test_valid_water_payload(self, valid_payload_dict):
        p = RawTelemetryPacket(**valid_payload_dict)
        assert p.sensor_id == "sensor-001"
        assert p.resource_type == "water"
        assert p.unit == "liters_per_min"

    def test_valid_energy_payload(self):
        p = RawTelemetryPacket(
            sensor_id="sensor-e01",
            timestamp="2026-01-01T00:00:00Z",
            resource_type="energy",
            value=3.82,
            unit="kWh",
        )
        assert p.resource_type == "energy"

    def test_valid_air_quality_payload(self):
        p = RawTelemetryPacket(
            sensor_id="sensor-aq01",
            timestamp="2026-01-01T00:00:00Z",
            resource_type="air_quality",
            value=152.0,
            unit="AQI",
        )
        assert p.resource_type == "air_quality"

    def test_invalid_resource_type(self, valid_payload_dict):
        valid_payload_dict["resource_type"] = "nuclear"
        with pytest.raises(ValidationError):
            RawTelemetryPacket(**valid_payload_dict)

    def test_missing_required_fields(self):
        with pytest.raises(ValidationError):
            RawTelemetryPacket(sensor_id="s1")

    def test_sensor_id_max_length(self, valid_payload_dict):
        valid_payload_dict["sensor_id"] = "x" * 65
        with pytest.raises(ValidationError):
            RawTelemetryPacket(**valid_payload_dict)

    def test_sensor_id_min_length(self, valid_payload_dict):
        valid_payload_dict["sensor_id"] = ""
        with pytest.raises(ValidationError):
            RawTelemetryPacket(**valid_payload_dict)

    def test_pii_fields_are_optional(self, valid_payload_dict):
        del valid_payload_dict["geo"]
        del valid_payload_dict["mac_address"]
        p = RawTelemetryPacket(**valid_payload_dict)
        assert p.geo is None
        assert p.mac_address is None

    def test_has_pii_true_when_geo_present(self, valid_payload):
        assert valid_payload.has_pii() is True

    def test_has_pii_false_when_no_pii(self):
        p = RawTelemetryPacket(
            sensor_id="s1",
            timestamp="2026-01-01T00:00:00Z",
            resource_type="water",
            value=5.0,
            unit="liters_per_min",
        )
        assert p.has_pii() is False

    def test_unknown_extra_fields_are_ignored(self, valid_payload_dict):
        """ConfigDict extra='ignore' must absorb new sensor fields."""
        valid_payload_dict["future_sensor_field"] = "some_value"
        p = RawTelemetryPacket(**valid_payload_dict)
        assert not hasattr(p, "future_sensor_field")

    def test_wrong_unit_for_resource_type(self):
        with pytest.raises(ValidationError, match="invalid for resource_type"):
            RawTelemetryPacket(
                sensor_id="s1",
                timestamp="2026-01-01T00:00:00Z",
                resource_type="water",
                value=10.0,
                unit="kWh",   # energy unit on water packet
            )

    def test_future_timestamp_beyond_tolerance_rejected(self):
        from datetime import datetime, timedelta, timezone
        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        with pytest.raises(ValidationError, match="future"):
            RawTelemetryPacket(
                sensor_id="s1",
                timestamp=future,
                resource_type="water",
                value=10.0,
                unit="liters_per_min",
            )

    def test_future_within_tolerance_accepted(self):
        from datetime import datetime, timedelta, timezone
        near_future = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()
        p = RawTelemetryPacket(
            sensor_id="s1",
            timestamp=near_future,
            resource_type="water",
            value=10.0,
            unit="liters_per_min",
        )
        assert p.sensor_id == "s1"

    def test_resource_extended_water(self, valid_payload):
        ext = valid_payload.resource_extended()
        assert ext is not None
        assert ext.pressure_bar == 2.3

    def test_resource_extended_no_ext_returns_none(self):
        p = RawTelemetryPacket(
            sensor_id="s1",
            timestamp="2026-01-01T00:00:00Z",
            resource_type="energy",
            value=1.0,
            unit="kWh",
        )
        assert p.resource_extended() is None


class TestGeoLocation:
    def test_valid_coordinates(self):
        g = GeoLocation(latitude=12.97, longitude=80.22)
        assert g.latitude == 12.97

    def test_latitude_out_of_range(self):
        with pytest.raises(ValidationError):
            GeoLocation(latitude=91.0, longitude=0.0)

    def test_longitude_out_of_range(self):
        with pytest.raises(ValidationError):
            GeoLocation(latitude=0.0, longitude=181.0)

    def test_negative_latitude_valid(self):
        g = GeoLocation(latitude=-45.0, longitude=60.0)
        assert g.latitude == -45.0

    def test_negative_longitude_valid(self):
        g = GeoLocation(latitude=0.0, longitude=-120.0)
        assert g.longitude == -120.0


class TestSensorMetadata:
    def test_valid_metadata(self):
        m = SensorMetadata(firmware_version="2.1.0", battery_level=87)
        assert m.battery_level == 87

    def test_battery_level_over_100_rejected(self):
        with pytest.raises(ValidationError):
            SensorMetadata(battery_level=150)

    def test_battery_level_negative_rejected(self):
        with pytest.raises(ValidationError):
            SensorMetadata(battery_level=-1)

    def test_all_fields_optional(self):
        m = SensorMetadata()
        assert m.firmware_version is None
        assert m.battery_level is None


class TestWaterExtended:
    def test_valid(self):
        w = WaterExtended(pressure_bar=2.5, ph_level=7.0, temperature_celsius=20.0)
        assert w.ph_level == 7.0

    def test_ph_over_14_rejected(self):
        with pytest.raises(ValidationError):
            WaterExtended(ph_level=15.0)

    def test_negative_pressure_rejected(self):
        with pytest.raises(ValidationError):
            WaterExtended(pressure_bar=-0.1)

    def test_all_optional(self):
        w = WaterExtended()
        assert w.pressure_bar is None


class TestEnergyExtended:
    def test_valid(self):
        e = EnergyExtended(voltage_v=230.0, current_a=16.5, power_factor=0.98)
        assert e.power_factor == 0.98

    def test_power_factor_over_one_rejected(self):
        with pytest.raises(ValidationError):
            EnergyExtended(power_factor=1.5)

    def test_negative_voltage_rejected(self):
        with pytest.raises(ValidationError):
            EnergyExtended(voltage_v=-10.0)

    def test_negative_current_accepted(self):
        """Bidirectional meters can report negative current."""
        e = EnergyExtended(current_a=-5.0)
        assert e.current_a == -5.0


class TestAirQualityExtended:
    def test_valid(self):
        a = AirQualityExtended(pm25_ug_m3=55.0, humidity_percent=60.0)
        assert a.humidity_percent == 60.0

    def test_humidity_over_100_rejected(self):
        with pytest.raises(ValidationError):
            AirQualityExtended(humidity_percent=110.0)

    def test_negative_pm25_rejected(self):
        with pytest.raises(ValidationError):
            AirQualityExtended(pm25_ug_m3=-1.0)


class TestSanitizedTelemetryPacket:
    def test_has_no_pii_fields(self):
        """SanitizedTelemetryPacket model must not define any PII fields."""
        fields = SanitizedTelemetryPacket.model_fields
        pii = {"latitude", "longitude", "lat", "lon", "mac_address", "geo"}
        assert not pii.intersection(fields.keys()), (
            f"SanitizedTelemetryPacket contains PII fields: {pii.intersection(fields.keys())}"
        )

    def test_has_ingested_at(self):
        """ingested_at is auto-populated (Phase 2)."""
        from datetime import datetime, timezone
        s = SanitizedTelemetryPacket(
            sensor_id="s1",
            timestamp=datetime.now(timezone.utc),
            resource_type="energy",
            value=10.0,
            unit="kWh",
        )
        assert s.ingested_at is not None

    def test_valid_sanitized(self):
        from datetime import datetime, timezone
        s = SanitizedTelemetryPacket(
            sensor_id="s1",
            timestamp=datetime.now(timezone.utc),
            resource_type="energy",
            value=10.0,
            unit="kWh",
        )
        assert s.sensor_id == "s1"
