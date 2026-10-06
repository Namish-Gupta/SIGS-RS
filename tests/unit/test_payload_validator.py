"""
Comprehensive unit tests for the PayloadValidator — Phase 3.

Covers:
  - Valid values for water, energy, air_quality
  - Boundary values (exactly at min/max limits)
  - Invalid negative values (energy, water)
  - Invalid air quality (> 500, negative)
  - NaN and Infinity handling
  - Zero values (valid for all resource types)
  - Custom bounds configuration
  - Runtime bounds update
  - Unknown resource type passthrough
  - Error message content
"""
from __future__ import annotations

import math

import pytest

from sigs_rs.common.exceptions import ImpossibleValueError
from sigs_rs.middleware.payload_validator import PayloadValidator
from sigs_rs.telemetry.models import RawTelemetryPacket


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_packet(resource_type: str, value: float, unit: str | None = None) -> RawTelemetryPacket:
    """Build a minimal valid packet with the given resource_type and value."""
    _units = {"water": "liters_per_min", "energy": "kWh", "air_quality": "AQI"}
    return RawTelemetryPacket(
        sensor_id="test-sensor",
        timestamp="2026-01-01T00:00:00Z",
        resource_type=resource_type,
        value=value,
        unit=unit or _units[resource_type],
    )


@pytest.fixture
def validator():
    return PayloadValidator()


# ─── Valid values ─────────────────────────────────────────────────────────────

class TestValidValues:
    def test_water_positive(self, validator):
        p = _make_packet("water", 42.7)
        validator.validate(p)  # must not raise

    def test_energy_positive(self, validator):
        p = _make_packet("energy", 3.82)
        validator.validate(p)

    def test_air_quality_mid_range(self, validator):
        p = _make_packet("air_quality", 152.0)
        validator.validate(p)

    def test_is_valid_returns_true(self, validator):
        p = _make_packet("water", 10.0)
        assert validator.is_valid(p) is True


# ─── Boundary values ─────────────────────────────────────────────────────────

class TestBoundaryValues:
    def test_water_zero_accepted(self, validator):
        """Zero flow rate is physically possible (valve closed)."""
        p = _make_packet("water", 0.0)
        validator.validate(p)

    def test_energy_zero_accepted(self, validator):
        """Zero consumption is valid (all circuits off)."""
        p = _make_packet("energy", 0.0)
        validator.validate(p)

    def test_air_quality_zero_accepted(self, validator):
        """AQI 0 = pristine air, valid."""
        p = _make_packet("air_quality", 0.0)
        validator.validate(p)

    def test_air_quality_exactly_500_accepted(self, validator):
        """AQI 500 = maximum of the standard scale, valid."""
        p = _make_packet("air_quality", 500.0)
        validator.validate(p)

    def test_water_very_large_accepted(self, validator):
        """Water has no upper bound — large values pass."""
        p = _make_packet("water", 999999.0)
        validator.validate(p)

    def test_energy_very_large_accepted(self, validator):
        """Energy has no upper bound — large values pass."""
        p = _make_packet("energy", 1e9)
        validator.validate(p)


# ─── Invalid values — negatives ───────────────────────────────────────────────

class TestNegativeValues:
    def test_negative_water_rejected(self, validator):
        """SRS REQ-API-3: negative water flow is impossible."""
        p = _make_packet("water", -1.0)
        with pytest.raises(ImpossibleValueError, match="below minimum"):
            validator.validate(p)

    def test_negative_energy_rejected(self, validator):
        """SRS REQ-API-3: negative energy consumption is impossible."""
        p = _make_packet("energy", -0.001)
        with pytest.raises(ImpossibleValueError, match="below minimum"):
            validator.validate(p)

    def test_negative_air_quality_rejected(self, validator):
        """SRS REQ-API-3: negative AQI is impossible."""
        p = _make_packet("air_quality", -50.0)
        with pytest.raises(ImpossibleValueError, match="below minimum"):
            validator.validate(p)

    def test_is_valid_returns_false_on_negative(self, validator):
        p = _make_packet("water", -5.0)
        assert validator.is_valid(p) is False

    def test_barely_negative_rejected(self, validator):
        """Even -0.0001 must be rejected."""
        p = _make_packet("energy", -0.0001)
        with pytest.raises(ImpossibleValueError):
            validator.validate(p)


# ─── Invalid values — air quality upper bound ─────────────────────────────────

class TestAirQualityUpperBound:
    def test_aqi_501_rejected(self, validator):
        """SRS: AQI > 500 is impossible."""
        p = _make_packet("air_quality", 501.0)
        with pytest.raises(ImpossibleValueError, match="above maximum"):
            validator.validate(p)

    def test_aqi_999_rejected(self, validator):
        p = _make_packet("air_quality", 999.0)
        with pytest.raises(ImpossibleValueError, match="above maximum"):
            validator.validate(p)

    def test_aqi_500_01_rejected(self, validator):
        """Just over the boundary."""
        p = _make_packet("air_quality", 500.01)
        with pytest.raises(ImpossibleValueError):
            validator.validate(p)


# ─── NaN and Infinity ─────────────────────────────────────────────────────────

class TestNanInfinity:
    def test_nan_rejected(self, validator):
        p = _make_packet("water", float("nan"))
        with pytest.raises(ImpossibleValueError, match="NaN"):
            validator.validate(p)

    def test_positive_infinity_rejected(self, validator):
        p = _make_packet("energy", float("inf"))
        with pytest.raises(ImpossibleValueError, match="Infinity"):
            validator.validate(p)

    def test_negative_infinity_rejected(self, validator):
        p = _make_packet("air_quality", float("-inf"))
        with pytest.raises(ImpossibleValueError, match="Infinity"):
            validator.validate(p)


# ─── Error message quality ────────────────────────────────────────────────────

class TestErrorMessages:
    def test_error_includes_sensor_id(self, validator):
        p = _make_packet("water", -5.0)
        with pytest.raises(ImpossibleValueError) as exc_info:
            validator.validate(p)
        assert "test-sensor" in str(exc_info.value)

    def test_error_includes_resource_type(self, validator):
        p = _make_packet("water", -5.0)
        with pytest.raises(ImpossibleValueError) as exc_info:
            validator.validate(p)
        assert "water" in str(exc_info.value)

    def test_error_includes_value(self, validator):
        p = _make_packet("energy", -10.5)
        with pytest.raises(ImpossibleValueError) as exc_info:
            validator.validate(p)
        assert "-10.5" in str(exc_info.value)


# ─── Custom bounds ────────────────────────────────────────────────────────────

class TestCustomBounds:
    def test_custom_bounds_override(self):
        """Custom bounds completely replace the defaults."""
        v = PayloadValidator(bounds={"water": {"min": 5.0, "max": 100.0}})
        p = _make_packet("water", 3.0)
        with pytest.raises(ImpossibleValueError):
            v.validate(p)

    def test_custom_bounds_max(self):
        v = PayloadValidator(bounds={"water": {"min": 0.0, "max": 100.0}})
        p = _make_packet("water", 101.0)
        with pytest.raises(ImpossibleValueError, match="above maximum"):
            v.validate(p)

    def test_custom_bounds_no_min(self):
        """min=None means no lower bound."""
        v = PayloadValidator(bounds={"energy": {"min": None, "max": 1000.0}})
        p = _make_packet("energy", -100.0)
        v.validate(p)  # negative allowed because min=None

    def test_custom_bounds_no_max(self):
        """max=None means no upper bound."""
        v = PayloadValidator(bounds={"air_quality": {"min": 0.0, "max": None}})
        p = _make_packet("air_quality", 9999.0)
        v.validate(p)  # above 500 allowed because max=None

    def test_unknown_resource_type_passes(self):
        """A resource type with no configured bounds always passes.
        We verify by updating bounds to remove the 'water' entry."""
        v = PayloadValidator()
        # Remove water bounds entirely so it behaves like an unknown type
        v._bounds.pop("water", None)
        p = _make_packet("water", -100.0)
        v.validate(p)  # passes because no bounds for "water"

    def test_bounds_property_returns_copy(self, validator):
        b = validator.bounds
        b["water"]["min"] = 999.0
        assert validator.bounds["water"]["min"] != 999.0


# ─── Runtime update ───────────────────────────────────────────────────────────

class TestRuntimeUpdate:
    def test_update_min(self, validator):
        validator.update_bounds("water", min_val=10.0)
        p = _make_packet("water", 5.0)
        with pytest.raises(ImpossibleValueError):
            validator.validate(p)

    def test_update_max(self, validator):
        validator.update_bounds("energy", max_val=100.0)
        p = _make_packet("energy", 200.0)
        with pytest.raises(ImpossibleValueError):
            validator.validate(p)

    def test_update_removes_bound(self, validator):
        """Setting max_val=None removes the upper bound."""
        validator.update_bounds("air_quality", max_val=None)
        p = _make_packet("air_quality", 9999.0)
        validator.validate(p)

    def test_update_new_resource_type(self, validator):
        """Can add bounds for a resource type not in defaults."""
        validator.update_bounds("solar", min_val=0.0, max_val=1000.0)
        assert "solar" in validator.bounds

    def test_update_preserves_unchanged(self, validator):
        """Updating only max_val doesn't touch min_val."""
        original_min = validator.bounds["water"]["min"]
        validator.update_bounds("water", max_val=1000.0)
        assert validator.bounds["water"]["min"] == original_min
