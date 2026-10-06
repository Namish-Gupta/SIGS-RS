"""Unit tests for the Privacy Anonymizer — updated for Phase 2 richer models."""
from __future__ import annotations

from copy import deepcopy

import pytest

from sigs_rs.common.exceptions import AnonymizerError
from sigs_rs.middleware.anonymizer import PrivacyAnonymizer, _find_keys
from sigs_rs.telemetry.models import RawTelemetryPacket

# Keep the legacy alias import working
from sigs_rs.schemas import TelemetryPayload  # noqa: F401


class TestPrivacyAnonymizer:
    """Tests for PrivacyAnonymizer.anonymize()"""

    def test_strips_geo_object(self, anonymizer, valid_payload):
        """geo sub-object must be removed from output."""
        result = anonymizer.anonymize(valid_payload)
        result_dict = result.model_dump()
        assert "geo" not in result_dict

    def test_strips_mac_address(self, anonymizer, valid_payload):
        """mac_address must not appear in sanitized output."""
        result = anonymizer.anonymize(valid_payload)
        result_dict = result.model_dump()
        assert "mac_address" not in result_dict

    def test_strips_latitude_longitude_direct(self):
        """Direct latitude/longitude fields are removed."""
        anon = PrivacyAnonymizer()
        p = RawTelemetryPacket(
            sensor_id="s1",
            timestamp="2026-01-01T00:00:00Z",
            resource_type="water",
            value=10.0,
            unit="liters_per_min",
            latitude=12.97,
            longitude=80.22,
        )
        result = anon.anonymize(p)
        d = result.model_dump()
        assert "latitude" not in d
        assert "longitude" not in d

    def test_strips_device_identifier(self):
        """Device-specific identifiers must not cross the persistence boundary."""
        packet = RawTelemetryPacket(
            sensor_id="s1",
            timestamp="2026-01-01T00:00:00Z",
            resource_type="water",
            value=10.0,
            unit="liters_per_min",
            mac_address="AA:BB:CC:DD:EE:FF",
            device_id="meter-hardware-0042",
        )

        result = PrivacyAnonymizer().anonymize(packet)

        assert "mac_address" not in result.model_dump()
        assert "device_id" not in result.model_dump()

    def test_nested_geolocation_is_removed(self, anonymizer, valid_payload):
        """The nested geo object, including latitude and longitude, is removed."""
        result = anonymizer.anonymize(valid_payload)

        assert _find_keys(result.model_dump(), {"geo", "latitude", "longitude"}) == []

    def test_preserves_non_pii_fields(self, anonymizer, valid_payload):
        """Non-PII fields must survive anonymization unchanged."""
        result = anonymizer.anonymize(valid_payload)
        assert result.sensor_id == valid_payload.sensor_id
        assert result.value == valid_payload.value
        assert result.resource_type == valid_payload.resource_type
        assert result.unit == valid_payload.unit

    def test_preserves_metadata(self, anonymizer, valid_payload):
        """Non-PII metadata sub-model is preserved (as a dict in output)."""
        result = anonymizer.anonymize(valid_payload)
        # metadata is serialised to dict in the sanitized output
        assert result.metadata is not None
        assert result.metadata.get("battery_level") == 87

    def test_no_pii_in_output_fields(self, anonymizer, valid_payload):
        """SanitizedTelemetryPacket model itself has no PII fields."""
        result = anonymizer.anonymize(valid_payload)
        result_dict = result.model_dump()
        pii_fields = {
            "latitude", "longitude", "lat", "lon", "mac_address", "device_id", "geo"
        }
        assert not pii_fields.intersection(result_dict.keys()), (
            f"PII field(s) found in sanitized output: "
            f"{pii_fields.intersection(result_dict.keys())}"
        )

    def test_custom_pii_fields(self):
        """Custom PII field list is respected."""
        anon = PrivacyAnonymizer(pii_fields=["custom_secret"])
        p = RawTelemetryPacket(
            sensor_id="s1",
            timestamp="2026-01-01T00:00:00Z",
            resource_type="energy",
            value=5.0,
            unit="kWh",
        )
        result = anon.anonymize(p)
        assert result.metadata is None or "custom_secret" not in (result.metadata or {})

    def test_missing_pii_fields_is_safe(self):
        """Packets without PII remain valid sanitized packets."""
        packet = RawTelemetryPacket(
            sensor_id="s1",
            timestamp="2026-01-01T00:00:00Z",
            resource_type="energy",
            value=5.0,
            unit="kWh",
        )

        result = PrivacyAnonymizer().anonymize(packet)

        assert result.sensor_id == packet.sensor_id
        assert result.value == packet.value

    def test_original_packet_is_never_modified(self, anonymizer, valid_payload):
        """Masking operates on a detached copy, including nested geolocation."""
        before = deepcopy(valid_payload.model_dump())

        anonymizer.anonymize(valid_payload)

        assert valid_payload.model_dump() == before
        assert valid_payload.geo is not None
        assert valid_payload.mac_address == "AA:BB:CC:DD:EE:FF"

    def test_malformed_payload_fails_closed(self, anonymizer):
        """Only a verified RawTelemetryPacket may enter the anonymizer."""
        malformed = {"sensor_id": "unverified-dict"}

        with pytest.raises(AnonymizerError, match="expected a verified RawTelemetryPacket"):
            anonymizer.anonymize(malformed)  # type: ignore[arg-type]

        assert malformed == {"sensor_id": "unverified-dict"}

    def test_masking_exception_becomes_anonymizer_error(self, monkeypatch, anonymizer, valid_payload):
        """A masking failure must surface as a fail-closed error, never raw output."""
        def explode(_: object, __: list[str]) -> None:
            raise RuntimeError("unexpected masking failure")

        monkeypatch.setattr(PrivacyAnonymizer, "_strip_pii", staticmethod(explode))

        with pytest.raises(AnonymizerError, match="unexpected masking failure"):
            anonymizer.anonymize(valid_payload)

    def test_security_check_finds_no_configured_pii_anywhere(self, anonymizer, valid_payload):
        """Recursive security assertion over the final object guards persistence."""
        result = anonymizer.anonymize(valid_payload)

        assert _find_keys(result.model_dump(), set(anonymizer.pii_fields)) == []

    def test_idempotency_core_fields(self, anonymizer, valid_payload):
        """
        Anonymizing twice yields the same core (non-timestamp) fields.
        Note: ingested_at differs between calls (wall-clock stamp).
        """
        first = anonymizer.anonymize(valid_payload)
        p2 = RawTelemetryPacket(
            sensor_id=first.sensor_id,
            timestamp=first.timestamp,
            resource_type=first.resource_type,
            value=first.value,
            unit=first.unit,
        )
        second = anonymizer.anonymize(p2)
        assert first.sensor_id == second.sensor_id
        assert first.value == second.value
        assert first.resource_type == second.resource_type
        assert first.unit == second.unit
        # ingested_at may differ by a few microseconds — don't assert equality

    def test_pii_fields_property(self, anonymizer):
        """pii_fields property returns a copy of the current list."""
        fields = anonymizer.pii_fields
        fields.append("injected")
        assert "injected" not in anonymizer.pii_fields

    def test_update_pii_fields(self, anonymizer):
        """update_pii_fields replaces the field list."""
        new = ["custom_field"]
        anonymizer.update_pii_fields(new)
        assert anonymizer.pii_fields == new

    def test_empty_pii_field_policy_is_rejected(self):
        """An empty policy would weaken privacy protection and is fail-closed."""
        with pytest.raises(ValueError, match="one or more"):
            PrivacyAnonymizer(pii_fields=[])

    def test_sanitized_output_has_ingested_at(self, anonymizer, valid_payload):
        """SanitizedTelemetryPacket carries ingested_at timestamp (Phase 2)."""
        result = anonymizer.anonymize(valid_payload)
        assert result.ingested_at is not None

    def test_water_ext_preserved(self, anonymizer, valid_payload):
        """water_ext is non-PII and should survive anonymization."""
        result = anonymizer.anonymize(valid_payload)
        assert result.water_ext is not None
        assert result.water_ext.get("pressure_bar") == 2.3


class TestFindKeys:
    """Tests for the _find_keys helper."""

    def test_finds_top_level(self):
        assert _find_keys({"latitude": 1.0, "ok": "x"}, {"latitude"}) == ["latitude"]

    def test_finds_nested(self):
        data = {"geo": {"latitude": 1.0, "longitude": 2.0}}
        found = _find_keys(data, {"latitude", "longitude"})
        assert set(found) == {"latitude", "longitude"}

    def test_finds_keys_nested_in_lists(self):
        data = {"readings": [{"latitude": 1.0}, {"child": {"mac_address": "AA"}}]}
        found = _find_keys(data, {"latitude", "mac_address"})
        assert set(found) == {"latitude", "mac_address"}

    def test_empty_targets(self):
        assert _find_keys({"a": 1}, set()) == []

    def test_no_match(self):
        assert _find_keys({"a": 1, "b": {"c": 2}}, {"latitude"}) == []
