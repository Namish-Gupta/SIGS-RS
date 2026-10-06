"""
Circuit Breaker — protects the database from runaway ingestion.

SRS REQ-SF-2: halt DB ingestion if write latency exceeds 2000 ms.

State machine:
    CLOSED  → normal operation (writes pass through)
    OPEN    → writes halted (SRS: drop all incoming MQTT connections)
    HALF_OPEN → testing recovery (one probe write allowed)

NOTE (Phase 1): Full state machine is defined. Transition logic
                implemented in Phase 5.
"""
from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sigs_rs.management.audit_logger import AuditLogger

logger = logging.getLogger(__name__)


class CBState(Enum):
    CLOSED    = auto()   # Normal — writes allowed
    OPEN      = auto()   # Tripped — writes halted
    HALF_OPEN = auto()   # Recovery probe — one write allowed


class CircuitBreaker:
    """
    Protects the database from sustained high-latency.

    When the breaker is OPEN:
    - All DB writes are rejected immediately with CircuitBreakerOpenError.
    - All new MQTT connections should be dropped (enforced in Phase 4).
    - /health endpoint reports 'degraded'.
    """

    def __init__(
        self,
        failure_threshold_ms: int,
        cooldown_seconds: int,
        audit_logger: "AuditLogger | None" = None,
    ) -> None:
        self.failure_threshold_ms = failure_threshold_ms
        self.cooldown_seconds = cooldown_seconds
        self._audit = audit_logger
        self._state = CBState.CLOSED
        self._opened_at: float | None = None

    @property
    def state(self) -> CBState:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state == CBState.OPEN

    async def call(self, coro) -> None:
        """
        Execute `coro` (a DB write coroutine) through the breaker.

        Raises:
            CircuitBreakerOpenError: if the breaker is OPEN.

        Phase 5: adds latency measurement and state transitions.
        """
        from sigs_rs.common.exceptions import CircuitBreakerOpenError
        if self._state == CBState.OPEN:
            raise CircuitBreakerOpenError("Circuit breaker is OPEN — DB writes halted.")
        # Stub: just await the coroutine without measuring latency.
        await coro

    def _trip(self) -> None:
        """Transition to OPEN state."""
        self._state = CBState.OPEN
        self._opened_at = time.monotonic()
        logger.critical("Circuit breaker OPENED — DB writes halted.")

    def _reset(self) -> None:
        """Transition to CLOSED state."""
        self._state = CBState.CLOSED
        self._opened_at = None
        logger.info("Circuit breaker CLOSED — DB writes resumed.")
