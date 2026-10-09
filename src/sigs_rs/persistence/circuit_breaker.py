"""
Circuit Breaker — protects the database from runaway ingestion.

SRS REQ-SF-2: halt DB ingestion if write latency exceeds 2000 ms.

State machine:
    CLOSED  → normal operation (writes pass through)
    OPEN    → writes halted (SRS: drop all incoming MQTT connections)
    HALF_OPEN → testing recovery (one probe write allowed)

Transitions:
    CLOSED  → OPEN      : when a DB write takes > failure_threshold_ms
    OPEN    → HALF_OPEN : after cooldown_seconds have elapsed
    HALF_OPEN → CLOSED  : if the probe write succeeds within threshold
    HALF_OPEN → OPEN    : if the probe write exceeds threshold or fails
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
    - All new MQTT connections should be dropped (enforced via on_open_callback).
    - /health endpoint reports 'degraded'.
    """

    def __init__(
        self,
        failure_threshold_ms: int,
        cooldown_seconds: int,
        audit_logger: "AuditLogger | None" = None,
        on_open_callback=None,
        on_close_callback=None,
        probe_action=None,
    ) -> None:
        self.failure_threshold_ms = failure_threshold_ms
        self.cooldown_seconds = cooldown_seconds
        self._audit = audit_logger
        self._state = CBState.CLOSED
        self._opened_at: float | None = None
        self._on_open = on_open_callback
        self._on_close = on_close_callback
        self._probe_action = probe_action
        self._probe_in_flight = False
        self._probe_task = None

    @property
    def state(self) -> CBState:
        """Return the current state, auto-transitioning OPEN→HALF_OPEN on cooldown expiry."""
        if self._state == CBState.OPEN and self._opened_at is not None:
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self.cooldown_seconds:
                self._state = CBState.HALF_OPEN
                logger.info(
                    "Circuit breaker HALF_OPEN — cooldown of %ds elapsed, allowing probe write.",
                    self.cooldown_seconds,
                )
        return self._state

    @property
    def is_open(self) -> bool:
        return self.state in (CBState.OPEN, CBState.HALF_OPEN)

    async def call(self, coro) -> None:
        """
        Execute `coro` (a DB write coroutine) through the breaker.

        Raises:
            CircuitBreakerOpenError: if the breaker is OPEN.
        """
        from sigs_rs.common.exceptions import CircuitBreakerOpenError

        current = self.state  # triggers OPEN → HALF_OPEN check

        if current == CBState.OPEN:
            import inspect
            if inspect.iscoroutine(coro):
                coro.close()
            raise CircuitBreakerOpenError("Circuit breaker is OPEN — DB writes halted.")

        if current == CBState.HALF_OPEN:
            if self._probe_in_flight:
                import inspect
                if inspect.iscoroutine(coro):
                    coro.close()
                raise CircuitBreakerOpenError("Circuit breaker is HALF_OPEN and probe is already in-flight.")
            self._probe_in_flight = True

        # CLOSED or HALF_OPEN: measure latency
        t0 = time.monotonic()
        try:
            await coro
        except Exception:
            # DB error during CLOSED or HALF_OPEN → trip
            if self._state in (CBState.CLOSED, CBState.HALF_OPEN):
                self._trip()
            raise
        finally:
            if current == CBState.HALF_OPEN:
                self._probe_in_flight = False

        elapsed_ms = (time.monotonic() - t0) * 1000

        if elapsed_ms > self.failure_threshold_ms:
            # Latency exceeded threshold → trip
            self._trip()
            raise CircuitBreakerOpenError(
                f"Circuit breaker tripped — DB write latency {elapsed_ms:.0f}ms "
                f"exceeds {self.failure_threshold_ms}ms threshold."
            )

        # Write succeeded within threshold
        if self._state == CBState.HALF_OPEN:
            self._reset()

    def _trip(self) -> None:
        """Transition to OPEN state."""
        self._state = CBState.OPEN
        self._opened_at = time.monotonic()
        self._probe_in_flight = False
        logger.critical("Circuit breaker OPENED — DB writes halted.")

        if self._on_open:
            try:
                self._on_open()
            except Exception:
                logger.exception("on_open_callback failed")

        if self._audit:
            self._fire_audit("CIRCUIT_BREAKER_OPEN", "CRITICAL")

        if self._probe_action:
            try:
                loop = asyncio.get_running_loop()
                if self._probe_task and not self._probe_task.done():
                    self._probe_task.cancel()
                self._probe_task = loop.create_task(self._cooldown_and_probe())
            except RuntimeError:
                pass

    async def _cooldown_and_probe(self) -> None:
        try:
            await asyncio.sleep(self.cooldown_seconds)
            if self.state == CBState.HALF_OPEN:
                try:
                    await self.call(self._probe_action())
                except Exception:
                    # call() handles re-tripping the breaker on failure
                    pass
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Error in synthetic probe task")

    def _reset(self) -> None:
        """Transition to CLOSED state."""
        self._state = CBState.CLOSED
        self._opened_at = None
        self._probe_in_flight = False
        logger.info("Circuit breaker CLOSED — DB writes resumed.")

        if self._on_close:
            try:
                self._on_close()
            except Exception:
                logger.exception("on_close_callback failed")

        if self._audit:
            self._fire_audit("CIRCUIT_BREAKER_RESET", "INFO")

    def _fire_audit(self, event_type: str, severity: str) -> None:
        """Log an audit event fire-and-forget (never blocks the breaker)."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(
                self._audit.log(
                    event_type=event_type,
                    severity=severity,
                    detail={"state": self._state.name},
                )
            )
        except RuntimeError:
            # No running event loop (sync tests)
            pass
