"""
In-memory ingestion queue.

Wraps asyncio.Queue to provide a bounded buffer between the ingestion
layer (MQTT / HTTP) and the security middleware pipeline.

SRS REQ-TI-4: verified, parsed packets are enqueued for sequential
processing by the Anomalous Packet Inspector.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sigs_rs.schemas import TelemetryPayload


class IngestionQueue:
    """Bounded async queue for raw telemetry packets."""

    def __init__(self, max_size: int) -> None:
        self._q: asyncio.Queue[TelemetryPayload] = asyncio.Queue(maxsize=max_size)

    async def put(self, packet: "TelemetryPayload") -> None:
        """
        Enqueue a packet. Raises QueueFullError if at capacity.
        Non-blocking: uses put_nowait to avoid stalling the ingestion path.
        """
        from sigs_rs.common.exceptions import QueueFullError
        try:
            self._q.put_nowait(packet)
        except asyncio.QueueFull as exc:
            raise QueueFullError("Ingestion queue is full — back-pressure.") from exc

    async def get(self) -> "TelemetryPayload":
        """Dequeue the next packet (blocks until one is available)."""
        return await self._q.get()

    def task_done(self) -> None:
        self._q.task_done()

    @property
    def depth(self) -> int:
        return self._q.qsize()

    @property
    def is_full(self) -> bool:
        return self._q.full()
