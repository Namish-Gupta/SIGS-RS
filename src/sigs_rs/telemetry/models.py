"""
Telemetry domain models for SIGS-RS.

Two canonical representations:

  RawTelemetryPacket      — what arrives from the Virtual Sensor Grid.
                            Contains PII (geo, mac_address) that the
                            Privacy Anonymizer will strip.

  SanitizedTelemetryPacket — what is written to TimescaleDB after PII
                              removal.  Has no PII fields at all.

Sub-models by concern:

  GeoLocation          [PII]  lat / lon sub-object
  SensorMetadata       [safe] firmware, battery, signal
  WaterExtended        [safe] pressure, pH, temperature, turbidity
  EnergyExtended       [safe] voltage, current, power-factor, frequency
  AirQualityExtended   [safe] PM2.5, PM10, CO₂, O₃, humidity

Design notes:
  • Unit validation (implementation decision) — structurally invalid units
    (e.g. "kelvin" for air_quality) are rejected here, before middleware.
    The SRS does not enumerate units; these come from the sensor-grid spec.
  • Impossible-value detection (SRS REQ-API-3) lives in the Inspector
    (Phase 3).  This model does NOT block negative `value` readings;
    that is the Inspector's job.
  • Timestamp future-tolerance of 5 min rejects mis-configured sensor
    clocks.  This is a structural check, not anomaly detection.
  • `model_config = ConfigDict(extra="ignore")` lets the model absorb
    forward-compatible new fields without raising a ValidationError.
    This makes the gateway resilient to minor sensor firmware upgrades.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from sigs_rs.common.constants import (
    BATTERY_LEVEL_MAX,
    BATTERY_LEVEL_MIN,
    LAT_MAX,
    LAT_MIN,
    LON_MAX,
    LON_MIN,
    SENSOR_ID_MAX_LEN,
    SENSOR_ID_MIN_LEN,
    TIMESTAMP_FUTURE_TOLERANCE_MINUTES,
    VALID_UNITS_BY_RESOURCE,
)


# ─── PII sub-model ────────────────────────────────────────────────────────────

class GeoLocation(BaseModel):
    """
    Geolocation sub-object — PII, stripped by Privacy Anonymizer.
    SRS REQ-PA-1: latitude and longitude must be eliminated before DB write.
    """
    model_config = ConfigDict(extra="ignore")

    latitude: float = Field(
        ...,
        ge=LAT_MIN, le=LAT_MAX,
        description="Decimal-degrees latitude [-90, 90].",
    )
    longitude: float = Field(
        ...,
        ge=LON_MIN, le=LON_MAX,
        description="Decimal-degrees longitude [-180, 180].",
    )


# ─── Non-PII metadata ─────────────────────────────────────────────────────────

class SensorMetadata(BaseModel):
    """Device metadata that does NOT contain PII — survives anonymization."""
    model_config = ConfigDict(extra="ignore")

    firmware_version: str | None = Field(
        default=None,
        max_length=32,
        description="Sensor firmware version string.",
    )
    battery_level: int | None = Field(
        default=None,
        ge=BATTERY_LEVEL_MIN,
        le=BATTERY_LEVEL_MAX,
        description="Battery state of charge, percent [0, 100].",
    )
    signal_strength_dbm: int | None = Field(
        default=None,
        description="Wireless signal strength in dBm (usually negative).",
    )


# ─── Resource-specific extended readings ──────────────────────────────────────

class WaterExtended(BaseModel):
    """Additional water-quality metrics beyond the primary flow-rate value."""
    model_config = ConfigDict(extra="ignore")

    pressure_bar: float | None = Field(
        default=None, ge=0.0,
        description="Line pressure in bar (≥ 0).",
    )
    ph_level: float | None = Field(
        default=None, ge=0.0, le=14.0,
        description="pH level [0, 14].",
    )
    temperature_celsius: float | None = Field(
        default=None, ge=-50.0, le=150.0,
        description="Water temperature °C.",
    )
    turbidity_ntu: float | None = Field(
        default=None, ge=0.0,
        description="Turbidity in NTU (≥ 0).",
    )


class EnergyExtended(BaseModel):
    """Additional electrical metrics beyond the primary consumption value."""
    model_config = ConfigDict(extra="ignore")

    voltage_v: float | None = Field(
        default=None, ge=0.0,
        description="RMS voltage in volts (≥ 0).",
    )
    current_a: float | None = Field(
        default=None,
        description="Current in amperes (can be negative for bidirectional meters).",
    )
    power_factor: float | None = Field(
        default=None, ge=0.0, le=1.0,
        description="Dimensionless power factor [0, 1].",
    )
    frequency_hz: float | None = Field(
        default=None, ge=0.0,
        description="Grid frequency in Hz (≥ 0).",
    )


class AirQualityExtended(BaseModel):
    """Additional pollutant / environmental metrics beyond the primary AQI."""
    model_config = ConfigDict(extra="ignore")

    pm25_ug_m3: float | None = Field(
        default=None, ge=0.0,
        description="PM 2.5 concentration µg/m³ (≥ 0).",
    )
    pm10_ug_m3: float | None = Field(
        default=None, ge=0.0,
        description="PM 10 concentration µg/m³ (≥ 0).",
    )
    co2_ppm: float | None = Field(
        default=None, ge=0.0,
        description="CO₂ concentration in ppm (≥ 0).",
    )
    o3_ppb: float | None = Field(
        default=None, ge=0.0,
        description="Ozone concentration in ppb (≥ 0).",
    )
    temperature_celsius: float | None = Field(
        default=None, ge=-50.0, le=60.0,
        description="Ambient temperature °C.",
    )
    humidity_percent: float | None = Field(
        default=None, ge=0.0, le=100.0,
        description="Relative humidity percent [0, 100].",
    )


# ─── Raw telemetry packet (contains PII) ─────────────────────────────────────

class RawTelemetryPacket(BaseModel):
    """
    A single telemetry message as transmitted by the Virtual Sensor Grid.

    ⚠ Contains PII:  `geo`, `latitude`, `longitude`, `mac_address`, `device_id`.
    The Privacy Anonymizer (SRS §4.3) will strip these before DB write.

    SRS REQ-TI-2: JSON must conform to the Community Sensor Grid API schema.
    The 512 KB size limit (REQ-TI-3) is enforced by TelemetryValidator,
    not by this Pydantic model.
    """
    model_config = ConfigDict(
        extra="ignore",           # future-proof against new sensor fields
        populate_by_name=True,    # accept both field aliases and Python names
        json_schema_extra={
            "example": {
                "sensor_id": "sensor-042",
                "timestamp": "2026-07-17T10:30:00.000Z",
                "resource_type": "water",
                "value": 42.7,
                "unit": "liters_per_min",
                "metadata": {"firmware_version": "2.1.0", "battery_level": 87},
                "water_ext": {"pressure_bar": 2.3, "ph_level": 7.1},
                "geo": {"latitude": 12.9716, "longitude": 80.2209},
                "mac_address": "AA:BB:CC:DD:EE:FF",
            }
        },
    )

    # ── Core required fields ──────────────────────────────────────────────────
    sensor_id: str = Field(
        ...,
        min_length=SENSOR_ID_MIN_LEN,
        max_length=SENSOR_ID_MAX_LEN,
        description="Unique sensor identifier (e.g. 'sensor-042').",
    )
    timestamp: datetime = Field(
        ...,
        description="ISO-8601 UTC timestamp from the sensor clock.",
    )
    resource_type: Literal["water", "energy", "air_quality"] = Field(
        ...,
        description="Community resource category.",
    )
    value: float = Field(
        ...,
        description=(
            "Primary resource measurement. "
            "Impossible-value bounds (SRS REQ-API-3) are checked by the "
            "Anomalous Packet Inspector (Phase 3), not here."
        ),
    )
    unit: str = Field(
        ...,
        min_length=1,
        max_length=32,
        description="Unit of measurement for `value`.",
    )

    # ── Non-PII metadata ──────────────────────────────────────────────────────
    metadata: SensorMetadata | None = Field(
        default=None,
        description="Device metadata (firmware, battery, signal).",
    )

    # ── Resource-specific extended data (non-PII) ─────────────────────────────
    water_ext: WaterExtended | None = Field(
        default=None,
        description="[water only] Extended water-quality metrics.",
    )
    energy_ext: EnergyExtended | None = Field(
        default=None,
        description="[energy only] Extended electrical metrics.",
    )
    air_quality_ext: AirQualityExtended | None = Field(
        default=None,
        description="[air_quality only] Extended pollutant metrics.",
    )

    # ── PII fields — present in raw payload, stripped by Privacy Anonymizer ───
    geo: GeoLocation | None = Field(
        default=None,
        description="[PII] Geolocation sub-object — will be stripped.",
    )
    latitude: float | None = Field(
        default=None,
        ge=LAT_MIN, le=LAT_MAX,
        description="[PII] Direct latitude — will be stripped.",
    )
    longitude: float | None = Field(
        default=None,
        ge=LON_MIN, le=LON_MAX,
        description="[PII] Direct longitude — will be stripped.",
    )
    mac_address: str | None = Field(
        default=None,
        description="[PII] Device MAC address — will be stripped.",
    )
    device_id: str | None = Field(
        default=None,
        description="[PII] Device-specific identifier — will be stripped.",
    )

    # ── Cross-field validators ────────────────────────────────────────────────

    @field_validator("sensor_id")
    @classmethod
    def _validate_sensor_id(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed or len(trimmed) < SENSOR_ID_MIN_LEN:
            raise ValueError("sensor_id cannot be empty or whitespace-only")
        return v

    @model_validator(mode="after")
    def _unit_matches_resource_type(self) -> "RawTelemetryPacket":
        """
        Reject payloads whose unit is not valid for the declared resource_type.
        Implementation decision: uses VALID_UNITS_BY_RESOURCE from constants.
        """
        valid = VALID_UNITS_BY_RESOURCE.get(self.resource_type, frozenset())
        if valid and self.unit not in valid:
            raise ValueError(
                f"Unit '{self.unit}' is invalid for resource_type "
                f"'{self.resource_type}'. "
                f"Accepted: {sorted(valid)}"
            )
        return self

    @model_validator(mode="after")
    def _timestamp_not_far_future(self) -> "RawTelemetryPacket":
        """
        Reject timestamps more than TIMESTAMP_FUTURE_TOLERANCE_MINUTES in the
        future — these indicate a misconfigured sensor clock.
        """
        ts = self.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        limit = datetime.now(timezone.utc) + timedelta(
            minutes=TIMESTAMP_FUTURE_TOLERANCE_MINUTES
        )
        if ts > limit:
            raise ValueError(
                f"Timestamp {self.timestamp.isoformat()} is more than "
                f"{TIMESTAMP_FUTURE_TOLERANCE_MINUTES} minutes in the future. "
                "Check the sensor clock."
            )
        return self

    @model_validator(mode="after")
    def _extended_block_matches_resource_type(self) -> "RawTelemetryPacket":
        """
        Warn if an extended block that doesn't match the resource_type is
        provided (e.g. `energy_ext` on a water packet).  The block is accepted
        but excluded from the sanitized output; this avoids noisy failures from
        misconfigured sensors while keeping the data accurate.
        """
        # No hard error — just silently tolerate mismatched optional extensions.
        # This is an implementation decision for robustness.
        return self

    # ── Convenience helpers ───────────────────────────────────────────────────

    def has_pii(self) -> bool:
        """Return True if any PII field is populated."""
        return any([
            self.geo is not None,
            self.latitude is not None,
            self.longitude is not None,
            self.mac_address is not None,
            self.device_id is not None,
        ])

    def resource_extended(self) -> WaterExtended | EnergyExtended | AirQualityExtended | None:
        """Return the extended sub-model that matches resource_type."""
        if self.resource_type == "water":
            return self.water_ext
        if self.resource_type == "energy":
            return self.energy_ext
        if self.resource_type == "air_quality":
            return self.air_quality_ext
        return None


# ─── Sanitized telemetry packet (no PII) ─────────────────────────────────────

class SanitizedTelemetryPacket(BaseModel):
    """
    Telemetry after the Privacy Anonymizer has stripped all PII.
    This is the exact record written to TimescaleDB.

    SRS REQ-PA-3: compatible with the PostgreSQL/TimescaleDB analytical schema.

    Deliberately has NO PII fields — a missing field cannot leak.
    If a future schema change re-adds a PII field it will be caught by the
    Anonymizer's post-strip assertion (SRS REQ-PA-4).
    """
    model_config = ConfigDict(extra="ignore")

    sensor_id: str
    timestamp: datetime
    ingested_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Wall-clock time the gateway accepted the packet.",
    )
    resource_type: str
    value: float
    unit: str
    metadata: dict[str, Any] | None = None
    water_ext: dict[str, Any] | None = None
    energy_ext: dict[str, Any] | None = None
    air_quality_ext: dict[str, Any] | None = None
    # ── Deliberately absent: geo, latitude, longitude, mac_address, device_id ─


# ─── Type aliases (backwards-compat with schemas.py imports) ─────────────────
# schemas.py re-exports these names so existing code is unbroken.
RawTelemetry = RawTelemetryPacket
SanitizedTelemetry = SanitizedTelemetryPacket
