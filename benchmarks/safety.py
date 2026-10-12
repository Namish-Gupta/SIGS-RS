"""
Safety monitor and watchdog for host resource protection during benchmarks.

Enforces:
  - Process resident memory (RSS) ceiling (default 1536 MiB)
  - Minimum available host memory (default 512 MiB)
  - Maximum test run duration timeout
"""
from __future__ import annotations

import time
from benchmarks.collector import get_current_rss_mib


class SafetyLimitExceededError(RuntimeError):
    """Raised when a benchmark exceeds safe resource boundaries on the host."""
    pass


def get_available_host_memory_mib() -> float:
    """Read available host memory from /proc/meminfo in MiB."""
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    return float(parts[1]) / 1024.0
    except Exception:
        pass
    return 1024.0  # safe assumption if unreadable


class SafetyWatchdog:
    """
    Guards the local machine against memory exhaustion and runaway benchmark loops.
    """

    def __init__(
        self,
        max_rss_mib: float = 1536.0,
        min_available_mib: float = 512.0,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.max_rss_mib = max_rss_mib
        self.min_available_mib = min_available_mib
        self.timeout_seconds = timeout_seconds
        self._start_time: float = time.perf_counter()

    def reset(self) -> None:
        self._start_time = time.perf_counter()

    def check(self) -> None:
        """
        Verify that the current process and host memory remain within safe bounds.
        Raises SafetyLimitExceededError if boundaries are breached.
        """
        # 1. Process RSS check
        current_rss = get_current_rss_mib()
        if current_rss > self.max_rss_mib:
            raise SafetyLimitExceededError(
                f"SAFETY ABORT: Benchmark process RSS ({current_rss:.1f} MiB) exceeded "
                f"safety limit ({self.max_rss_mib:.1f} MiB). Aborting to prevent host OOM."
            )

        # 2. Host available memory check
        available = get_available_host_memory_mib()
        if available < self.min_available_mib:
            raise SafetyLimitExceededError(
                f"SAFETY ABORT: Available host memory ({available:.1f} MiB) dropped below "
                f"safety threshold ({self.min_available_mib:.1f} MiB). Aborting immediately."
            )

        # 3. Timeout check
        elapsed = time.perf_counter() - self._start_time
        if elapsed > self.timeout_seconds:
            raise SafetyLimitExceededError(
                f"SAFETY ABORT: Benchmark execution timed out after {elapsed:.1f}s "
                f"(limit: {self.timeout_seconds:.1f}s)."
            )
