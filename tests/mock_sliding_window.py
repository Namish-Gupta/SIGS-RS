import collections
import time
from typing import Optional

class MockAsyncSlidingWindow:
    def __init__(self, window_seconds: int, max_packets: int, redis_url: str = None) -> None:
        if window_seconds <= 0:
            raise ValueError(f"window_seconds must be positive, got {window_seconds}")
        if max_packets <= 0:
            raise ValueError(f"max_packets must be positive, got {max_packets}")

        self.window_seconds = window_seconds
        self.max_packets = max_packets
        self._windows = collections.defaultdict(collections.deque)

    async def check(self, sensor_id: str, now: Optional[float] = None) -> bool:
        if now is None:
            now = time.time()

        window = self._windows[sensor_id]
        cutoff = now - self.window_seconds

        # Remove expired timestamps
        while window and window[0] < cutoff:
            window.popleft()

        if len(window) >= self.max_packets:
            return False

        window.append(now)
        return True

    async def cleanup(self, now: Optional[float] = None) -> int:
        if now is None:
            now = time.time()
        keys = list(self._windows.keys())
        removed = 0
        cutoff = now - self.window_seconds
        for k in keys:
            window = self._windows[k]
            while window and window[0] < cutoff:
                window.popleft()
            if not window:
                del self._windows[k]
                removed += 1
        return removed


    async def reset(self) -> None:
        self._windows.clear()

    def sensor_count(self, sensor_id: str) -> int:
        return len(self._windows[sensor_id])

    def active_sensor_count(self) -> int:
        return len([s for s, w in self._windows.items() if w])
