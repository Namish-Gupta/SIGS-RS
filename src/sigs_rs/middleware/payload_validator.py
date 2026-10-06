"""
Payload value validator for the Anomalous Packet Inspector.

SRS REQ-API-3: drop packets with statistically impossible resource values
               (e.g., negative power consumption, AQI > 500).

Design:
  - Bounds are loaded from constants.IMPOSSIBLE_VALUE_BOUNDS at construction.
  - update_bounds() allows runtime reconfiguration (Phase 7 Config API).
  - validate() raises ImpossibleValueError with a diagnostic message.
  - is_valid() wraps validate() and returns a bool (convenience).

The validator is stateless and purely functional — no I/O, no async, no
external dependencies — satisfying SRS REQ-QA-4 (independently testable).
"""
from __future__ import annotations

import math
from typing import Any

from sigs_rs.common.constants import IMPOSSIBLE_VALUE_BOUNDS
from sigs_rs.common.exceptions import ImpossibleValueError
from sigs_rs.telemetry.models import RawTelemetryPacket


class PayloadValidator:
    """
    Checks that telemetry values are physically plausible for the
    reported resource type.

    Bounds are sourced from constants.IMPOSSIBLE_VALUE_BOUNDS and can
    be extended at runtime via config API (Phase 7).
    """

    __slots__ = ("_bounds",)

    def __init__(self, bounds: dict[str, dict[str, float | None]] | None = None) -> None:
        # Deep-copy so runtime updates don't mutate the constant.
        self._bounds: dict[str, dict[str, float | None]] = {
            k: dict(v) for k, v in (bounds or IMPOSSIBLE_VALUE_BOUNDS).items()
        }

    @property
    def bounds(self) -> dict[str, dict[str, float | None]]:
        """Return a copy of the current bounds configuration."""
        return {k: dict(v) for k, v in self._bounds.items()}

    def update_bounds(self, resource_type: str, *, min_val: float | None = ..., max_val: float | None = ...) -> None:
        """
        Update the bounds for a specific resource type at runtime.
        Pass None for min_val or max_val to indicate "no bound".
        Use the sentinel ... (Ellipsis) to leave a bound unchanged.
        """
        if resource_type not in self._bounds:
            self._bounds[resource_type] = {"min": None, "max": None}
        if min_val is not ...:
            self._bounds[resource_type]["min"] = min_val
        if max_val is not ...:
            self._bounds[resource_type]["max"] = max_val

    def validate(self, packet: RawTelemetryPacket) -> None:
        """
        Validate the packet's value against known physical bounds.

        Raises:
            ImpossibleValueError: if the value is outside acceptable range
                                  or is NaN/Infinity.
        """
        value = packet.value
        resource = packet.resource_type

        # NaN and Infinity are always impossible regardless of resource type.
        if math.isnan(value) or math.isinf(value):
            raise ImpossibleValueError(
                f"sensor={packet.sensor_id}: value is {value} "
                f"(NaN/Infinity not accepted for {resource})"
            )

        bounds = self._bounds.get(resource)
        if bounds is None:
            # No bounds configured for this resource type — pass through.
            return

        lo = bounds.get("min")
        hi = bounds.get("max")

        if lo is not None and value < lo:
            raise ImpossibleValueError(
                f"sensor={packet.sensor_id}: {resource} value {value} "
                f"is below minimum {lo}"
            )
        if hi is not None and value > hi:
            raise ImpossibleValueError(
                f"sensor={packet.sensor_id}: {resource} value {value} "
                f"is above maximum {hi}"
            )

    def is_valid(self, packet: RawTelemetryPacket) -> bool:
        """Return True if packet passes validation, False otherwise."""
        try:
            self.validate(packet)
            return True
        except ImpossibleValueError:
            return False
