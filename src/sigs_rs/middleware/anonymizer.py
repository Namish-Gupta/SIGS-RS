"""
Privacy Anonymizer — secondary security middleware subsystem.

SRS §4.3: strips PII (latitude, longitude, mac_address, geo) from
          verified packets before database persistence.

SRS REQ-PA-1: eliminate PII fields.
SRS REQ-PA-4: fail-secure — drop entire packet on any masking exception.

NOTE (Phase 1): Full implementation. This module is self-contained
                and does not depend on any I/O, making it independently
                testable (SRS REQ-QA-4).
"""
from __future__ import annotations

from copy import deepcopy
import logging
from datetime import datetime, timezone
from typing import Any

from sigs_rs.common.constants import DEFAULT_PII_FIELDS
from sigs_rs.common.exceptions import AnonymizerError
from sigs_rs.telemetry.models import RawTelemetryPacket, SanitizedTelemetryPacket

# Legacy name aliases kept so other modules that import from schemas still work.
TelemetryPayload = RawTelemetryPacket
SanitizedTelemetry = SanitizedTelemetryPacket

logger = logging.getLogger(__name__)


class PrivacyAnonymizer:
    """
    Data-masking pipeline that strips PII from telemetry packets.

    The PII field list is configurable at runtime (Phase 7 Config API).
    Default list is defined in constants.DEFAULT_PII_FIELDS.

    Fail-secure: any exception during masking raises AnonymizerError,
    which the pipeline uses to audit and drop the packet (SRS REQ-PA-4).
    This class only accepts a verified RawTelemetryPacket; it never parses or
    mutates an untrusted transport payload.
    """

    def __init__(self, pii_fields: list[str] | None = None) -> None:
        self._pii_fields = self._validate_pii_fields(
            list(DEFAULT_PII_FIELDS) if pii_fields is None else pii_fields
        )

    @property
    def pii_fields(self) -> list[str]:
        return list(self._pii_fields)

    def update_pii_fields(self, fields: list[str]) -> None:
        """Update the PII field list (called by Config API — Phase 7)."""
        self._pii_fields = self._validate_pii_fields(fields)

    def anonymize(self, packet: RawTelemetryPacket) -> SanitizedTelemetryPacket:
        """
        Strip all PII fields from the packet and return a SanitizedTelemetryPacket.

        Raises:
            AnonymizerError: on any masking failure (triggers fail-secure drop).
        """
        sensor_id = getattr(packet, "sensor_id", "<unknown>")
        try:
            if not isinstance(packet, RawTelemetryPacket):
                raise TypeError("expected a verified RawTelemetryPacket")

            # Work from a detached copy.  A failed mask must never alter the
            # raw packet that was supplied to the security boundary.
            raw = deepcopy(packet.model_dump())
            self._strip_pii(raw, self._pii_fields)
            self._assert_no_pii(raw, self._pii_fields)

            # Whitelist only fields present on the safe output type.  This is
            # the final persistence boundary: raw PII keys have no path into
            # SanitizedTelemetryPacket even if a masking rule is misconfigured.
            sanitized_fields = {k: raw[k] for k in SanitizedTelemetryPacket.model_fields if k in raw}
            sanitized_fields["ingested_at"] = datetime.now(timezone.utc)
            return SanitizedTelemetryPacket(**sanitized_fields)
        except AnonymizerError:
            raise
        except Exception as exc:
            raise AnonymizerError(
                f"Masking failed for sensor '{sensor_id}': {exc}"
            ) from exc

    # ─── Private helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _strip_pii(data: Any, pii_fields: list[str]) -> None:
        """
        Recursively remove configured PII keys from a detached JSON-like value.
        Lists are traversed as well as objects so nested PII cannot survive.
        Matches keys case-insensitively.
        """
        if isinstance(data, dict):
            targets_lower = {field.lower() for field in pii_fields}
            keys_to_remove = [k for k in list(data.keys()) if k.lower() in targets_lower]
            for field in keys_to_remove:
                data.pop(field, None)
            for value in data.values():
                PrivacyAnonymizer._strip_pii(value, pii_fields)
        elif isinstance(data, list):
            for value in data:
                PrivacyAnonymizer._strip_pii(value, pii_fields)

    @staticmethod
    def _assert_no_pii(data: dict[str, Any], pii_fields: list[str]) -> None:
        """
        Post-anonymization verification: ensure no PII key survives.
        If any PII field is found, raise AnonymizerError (fail-secure).
        """
        found = _find_keys(data, set(pii_fields))
        if found:
            raise AnonymizerError(
                f"PII verification failed — residual fields detected: {found}"
            )

    @staticmethod
    def _validate_pii_fields(fields: list[str]) -> list[str]:
        """Reject an empty or malformed privacy policy rather than weaken it."""
        cleaned = [field.strip() for field in fields if isinstance(field, str) and field.strip()]
        if not cleaned or len(cleaned) != len(fields):
            raise ValueError("pii_fields must contain one or more non-empty field names")
        return list(dict.fromkeys(cleaned))


def _find_keys(data: Any, targets: set[str]) -> list[str]:
    """Return configured keys found anywhere in a nested JSON-like value (case-insensitive)."""
    targets_lower = {t.lower() for t in targets}
    found: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            if key.lower() in targets_lower:
                found.append(key)
            found.extend(_find_keys(value, targets))
    elif isinstance(data, list):
        for value in data:
            found.extend(_find_keys(value, targets))
    return found
