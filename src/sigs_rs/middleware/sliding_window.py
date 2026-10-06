"""
Sliding-window algorithm for the Anomalous Packet Inspector.

SRS REQ-API-1: track timestamps of all packets per sensor ID over a
               rolling 10-second window.
SRS REQ-API-2: drop packets if sensor exceeds 50 packets per window.

Data structure: dict[sensor_id, deque[timestamp_float]]
  - O(1) amortized per check (deque popleft + append)
  - ~4 MB total RAM for 10,000 sensors × 50 timestamps (well under 24 GB limit)

Concurrency model:
  The window is designed for single-threaded asyncio (one event loop, one
  call at a time).  time.monotonic() is used for timestamps to avoid
  system-clock jumps (NTP, DST, leap-seconds).  For future multi-worker
  deployment, each worker gets its own SlidingWindow instance — the
  aggregate rate limit is effectively max_packets × N_workers, which is
  acceptable per the SRS's stated single-process deployment model.

Injecting `now` via parameter supports deterministic testing: tests can
advance virtual time without sleeping.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque


class SlidingWindow:
    """
    Per-sensor rolling timestamp window.

    Each sensor ID maps to a deque of float timestamps (monotonic seconds).
    On each check():
      1. Evict entries older than (now - window_seconds)
      2. If count >= max_packets → rate exceeded → return False
      3. Else append current timestamp → return True

    The deque is always sorted (timestamps are appended in order), so
    eviction from the left is O(k) where k is the number of expired
    entries — amortized O(1) per call since each entry is evicted at
    most once.
    """

    __slots__ = ("window_seconds", "max_packets", "_windows")

    def __init__(self, window_seconds: int, max_packets: int) -> None:
        if window_seconds <= 0:
            raise ValueError(f"window_seconds must be positive, got {window_seconds}")
        if max_packets <= 0:
            raise ValueError(f"max_packets must be positive, got {max_packets}")
        self.window_seconds = window_seconds
        self.max_packets = max_packets
        self._windows: dict[str, deque[float]] = defaultdict(deque)

    def check(self, sensor_id: str, now: float | None = None) -> bool:
        """
        Check whether the sensor is within the allowed rate and, if so,
        record the current timestamp.

        Args:
            sensor_id: Unique identifier of the originating sensor.
            now: Current time as a monotonic float.  Defaults to
                 time.monotonic().  Inject a specific value for testing.

        Returns:
            True  → packet is within rate limit (allowed).
            False → packet exceeds rate limit (must be dropped).
        """
        if now is None:
            now = time.monotonic()

        window = self._windows[sensor_id]
        cutoff = now - self.window_seconds

        # 1. Evict expired entries from the left of the deque.
        #    Because timestamps are appended in monotonic order, once we
        #    find one that is >= cutoff the rest are also valid.
        while window and window[0] < cutoff:
            window.popleft()

        # 2. Check count BEFORE appending the new timestamp.
        if len(window) >= self.max_packets:
            return False

        # 3. Record this packet's timestamp.
        window.append(now)
        return True

    def sensor_count(self, sensor_id: str) -> int:
        """Return the number of (possibly expired) timestamps stored for this sensor."""
        return len(self._windows.get(sensor_id, deque()))

    def active_sensor_count(self) -> int:
        """Return the number of sensors with at least one stored timestamp."""
        return sum(1 for d in self._windows.values() if d)

    def cleanup(self, now: float | None = None) -> int:
        """
        Remove entries for sensors whose entire window has expired.
        Called periodically by a background asyncio task.

        Returns the number of sensor entries removed.
        """
        if now is None:
            now = time.monotonic()
        cutoff = now - self.window_seconds

        # Collect sensor IDs whose most-recent timestamp is before the cutoff.
        stale = [
            sid for sid, dq in self._windows.items()
            if not dq or dq[-1] < cutoff
        ]
        for sid in stale:
            del self._windows[sid]
        return len(stale)

    def reset(self) -> None:
        """Clear all tracked state.  Useful for tests."""
        self._windows.clear()
