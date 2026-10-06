"""
TelemetryValidator — multi-stage validation pipeline.

Separates concerns that Pydantic models cannot address alone:

  Stage 1 — Size gate     Enforces 512 KB limit (SRS REQ-TI-3).
             Evaluated on raw bytes before any parsing.

  Stage 2 — JSON parse    Catches malformed JSON (SRS: RFC 8259 compliance).
             Must produce a Python dict.

  Stage 3 — Schema check  Validates the parsed dict against RawTelemetryPacket.
             Catches missing required fields, wrong types, invalid units,
             future timestamps, and out-of-bounds sub-field values.

All three stages raise specific exceptions from sigs_rs.common.exceptions
so the caller can audit-log the exact failure reason.

NOTE: Impossible-value detection (SRS REQ-API-3) is NOT implemented here.
      That belongs to the Anomalous Packet Inspector (Phase 3).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError

from sigs_rs.common.constants import MAX_PAYLOAD_BYTES
from sigs_rs.common.exceptions import MalformedPayloadError, PayloadTooLargeError
from sigs_rs.telemetry.models import RawTelemetryPacket

logger = logging.getLogger(__name__)


class TelemetryValidator:
    """
    Stateless, independently-testable telemetry validation pipeline.

    Instantiate once (e.g. per process) and call validate_bytes() for every
    incoming packet regardless of transport (MQTT or HTTP).

    SRS REQ-QA-4: security middleware must be independently testable.
    """

    def __init__(self, max_bytes: int = MAX_PAYLOAD_BYTES) -> None:
        self.max_bytes = max_bytes

    # ── Public API ────────────────────────────────────────────────────────────

    def validate_bytes(self, raw: bytes) -> RawTelemetryPacket:
        """
        Full three-stage pipeline for raw MQTT / HTTP body bytes.

        Args:
            raw: The raw body as bytes.

        Returns:
            A validated RawTelemetryPacket.

        Raises:
            PayloadTooLargeError:  bytes exceed max_bytes.
            MalformedPayloadError: invalid JSON or schema violation.
        """
        self._check_size(raw)
        data = self._parse_json(raw)
        return self._validate_schema(data)

    def validate_str(self, json_str: str) -> RawTelemetryPacket:
        """
        Validate a JSON string (e.g. from an HTTP body already decoded).

        Raises:
            PayloadTooLargeError:  encoded UTF-8 length exceeds max_bytes.
            MalformedPayloadError: invalid JSON or schema violation.
        """
        return self.validate_bytes(json_str.encode("utf-8"))

    def validate_dict(self, data: dict[str, Any]) -> RawTelemetryPacket:
        """
        Validate a pre-parsed dict (e.g. from FastAPI's Pydantic body parsing).

        Skips the size and JSON-parse stages — use when those have already
        been handled upstream (FastAPI enforces Content-Length).

        Raises:
            MalformedPayloadError: schema violation.
        """
        return self._validate_schema(data)

    def check_size(self, raw: bytes) -> None:
        """
        Public single-concern check: raises PayloadTooLargeError if too large.
        Useful for middleware that wants to gate on size before reading body.
        """
        self._check_size(raw)

    # ── Private stages ────────────────────────────────────────────────────────

    def _check_size(self, raw: bytes) -> None:
        if len(raw) > self.max_bytes:
            raise PayloadTooLargeError(
                f"Payload is {len(raw):,} bytes; "
                f"maximum allowed is {self.max_bytes:,} bytes "
                f"(SRS REQ-TI-3: 512 KB limit)."
            )

    @staticmethod
    def _parse_json(raw: bytes) -> dict[str, Any]:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MalformedPayloadError(
                f"JSON parse error: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise MalformedPayloadError(
                f"Expected a JSON object (dict), got {type(data).__name__}."
            )
        return data

    @staticmethod
    def _validate_schema(data: dict[str, Any]) -> RawTelemetryPacket:
        try:
            return RawTelemetryPacket.model_validate(data)
        except ValidationError as exc:
            # Condense Pydantic's error list into a readable message.
            errors = exc.errors(include_url=False)
            summary = "; ".join(
                f"[{'.'.join(str(l) for l in e['loc'])}] {e['msg']}"
                for e in errors[:5]           # cap at 5 to avoid huge logs
            )
            raise MalformedPayloadError(
                f"Schema validation failed ({len(errors)} error(s)): {summary}"
            ) from exc


# ─── Module-level singleton ───────────────────────────────────────────────────
# Applications import this directly; tests can instantiate their own.
default_validator = TelemetryValidator()
