"""
Performance and resource metrics collector for SIGS-RS benchmarks.

Tracks:
  - High-precision latency percentiles (avg, min, max, p50, p95, p99)
  - Process CPU utilization (% of total CPU over benchmark duration)
  - Memory utilization (Peak RSS, start RSS, final RSS, delta in MiB)
  - Ingestion queue depth dynamics (min, max, avg, final)
  - Strict packet accounting and security invariant conservation
"""
from __future__ import annotations

import os
import resource
import statistics
import time
from typing import Any


def get_current_rss_mib() -> float:
    """
    Read current Resident Set Size (RSS) in MiB from /proc/self/status or getrusage.
    """
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    return float(parts[1]) / 1024.0  # kB to MiB
    except Exception:
        pass
    # Fallback to getrusage (ru_maxrss is in kB on Linux)
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return float(usage.ru_maxrss) / 1024.0


def compute_percentiles(samples: list[float]) -> dict[str, float]:
    """
    Compute exact linear-interpolation percentiles (min, avg, max, p50, p95, p99).
    Handles small n (e.g. n=1, 2) and large n correctly.
    """
    n = len(samples)
    if n == 0:
        return {
            "count": 0,
            "avg_ms": 0.0,
            "min_ms": 0.0,
            "max_ms": 0.0,
            "p50_ms": 0.0,
            "p95_ms": 0.0,
            "p99_ms": 0.0,
        }
    sorted_s = sorted(samples)

    def _pct(p: float) -> float:
        if n == 1:
            return sorted_s[0]
        idx = (n - 1) * p
        low = int(idx)
        high = min(low + 1, n - 1)
        weight = idx - low
        return sorted_s[low] * (1.0 - weight) + sorted_s[high] * weight

    return {
        "count": n,
        "avg_ms": round(sum(sorted_s) / n, 4),
        "min_ms": round(sorted_s[0], 4),
        "max_ms": round(sorted_s[-1], 4),
        "p50_ms": round(_pct(0.50), 4),
        "p95_ms": round(_pct(0.95), 4),
        "p99_ms": round(_pct(0.99), 4),
    }


class LatencyStats:
    """Calculates latency statistics and percentiles from raw duration samples."""

    def __init__(self, max_samples: int = 100_000) -> None:
        self._samples: list[float] = []
        self._max_samples = max_samples
        self._count: int = 0
        self._sum_ms: float = 0.0

    def record_seconds(self, duration_seconds: float) -> None:
        ms = duration_seconds * 1000.0
        self._count += 1
        self._sum_ms += ms
        if len(self._samples) < self._max_samples:
            self._samples.append(ms)

    def to_dict(self) -> dict[str, float]:
        if self._count == 0 or not self._samples:
            return {
                "count": 0,
                "avg_ms": 0.0,
                "min_ms": 0.0,
                "max_ms": 0.0,
                "p50_ms": 0.0,
                "p95_ms": 0.0,
                "p99_ms": 0.0,
            }
        pcts = compute_percentiles(self._samples)
        pcts["count"] = self._count
        pcts["avg_ms"] = round(self._sum_ms / self._count, 4)
        return pcts


class BenchmarkCollector:
    """
    Central performance collector for an individual benchmark run.
    """

    def __init__(
        self,
        suite_name: str,
        stage_name: str = "",
        benchmark_mode: str = "saturation_microbenchmark",
        target_offered_rate_pps: int | None = None,
    ) -> None:
        self.suite_name = suite_name
        self.stage_name = stage_name
        self.benchmark_mode = benchmark_mode  # 'saturation_microbenchmark' | 'rate_controlled_load_test'
        self.target_offered_rate_pps = target_offered_rate_pps

        # Host logical CPUs
        self._host_cpus = os.cpu_count() or 1

        # Timing
        self._wall_start: float = 0.0
        self._wall_end: float = 0.0
        self._cpu_start: float = 0.0
        self._cpu_end: float = 0.0

        # Status
        self.skipped: bool = False
        self.skip_reason: str = ""

        # Memory
        self._start_rss_mib: float = 0.0
        self._peak_rss_mib: float = 0.0
        self._end_rss_mib: float = 0.0

        # Latency buckets
        self.latencies: dict[str, LatencyStats] = {
            "inspector": LatencyStats(),
            "anonymizer": LatencyStats(),
            "middleware": LatencyStats(),
            "database_batch": LatencyStats(),
            "e2e_persisted": LatencyStats(),
            "e2e_dropped": LatencyStats(),
        }

        # Queue depth samples
        self._queue_samples: list[int] = []

        # Strict packet accounting
        self.packets_submitted: int = 0
        self.packets_received: int = 0
        self.packets_verified: int = 0
        self.packets_sanitized: int = 0
        self.packets_dropped_rate: int = 0
        self.packets_dropped_bounds: int = 0
        self.packets_dropped_anonymizer: int = 0
        self.packets_dropped_queue: int = 0
        self.packets_persisted: int = 0
        self.packets_in_flight: int = 0

    def start(self) -> None:
        """Start the benchmark timing and resource collection."""
        self._wall_start = time.perf_counter()
        self._cpu_start = time.process_time()
        self._start_rss_mib = get_current_rss_mib()
        self._peak_rss_mib = self._start_rss_mib

    def sample_resources(self) -> None:
        """Sample transient resources (e.g. peak RSS)."""
        current_rss = get_current_rss_mib()
        if current_rss > self._peak_rss_mib:
            self._peak_rss_mib = current_rss

    def sample_queue_depth(self, depth: int) -> None:
        """Record an in-flight queue depth sample."""
        self._queue_samples.append(depth)

    def record_latency(self, stage: str, duration_seconds: float) -> None:
        """Record duration for a specific pipeline stage."""
        if stage not in self.latencies:
            self.latencies[stage] = LatencyStats()
        self.latencies[stage].record_seconds(duration_seconds)

    def stop(self) -> None:
        """Stop timing and finalize resource measurements."""
        self._wall_end = time.perf_counter()
        self._cpu_end = time.process_time()
        self._end_rss_mib = get_current_rss_mib()
        if self._end_rss_mib > self._peak_rss_mib:
            self._peak_rss_mib = self._end_rss_mib

    @property
    def packets_offered(self) -> int:
        return self.packets_submitted

    @packets_offered.setter
    def packets_offered(self, val: int) -> None:
        self.packets_submitted = val

    @property
    def duration_seconds(self) -> float:
        return max(0.0001, self._wall_end - self._wall_start)

    @property
    def achieved_throughput_pps(self) -> float:
        # Throughput based on total processed packets (verified + dropped)
        processed = (
            self.packets_verified
            + self.packets_dropped_rate
            + self.packets_dropped_bounds
            + self.packets_dropped_anonymizer
        )
        if processed == 0:
            processed = self.packets_persisted or self.packets_received
        return round(processed / self.duration_seconds, 2)

    @property
    def cpu_utilization_single_core_percent(self) -> float:
        cpu_time = max(0.0, self._cpu_end - self._cpu_start)
        return round((cpu_time / self.duration_seconds) * 100.0, 2)

    @property
    def cpu_utilization_total_host_percent(self) -> float:
        cpu_time = max(0.0, self._cpu_end - self._cpu_start)
        return round((cpu_time / (self.duration_seconds * self._host_cpus)) * 100.0, 2)

    @property
    def cpu_utilization_percent(self) -> float:
        return self.cpu_utilization_single_core_percent

    def get_queue_stats(self) -> dict[str, float]:
        if not self._queue_samples:
            return {"min": 0, "max": 0, "avg": 0.0, "final": 0}
        return {
            "min": min(self._queue_samples),
            "max": max(self._queue_samples),
            "avg": round(sum(self._queue_samples) / len(self._queue_samples), 2),
            "final": self._queue_samples[-1],
        }

    def total_dropped(self) -> int:
        return (
            self.packets_dropped_rate
            + self.packets_dropped_bounds
            + self.packets_dropped_anonymizer
            + self.packets_dropped_queue
        )

    @property
    def is_conserved(self) -> bool:
        """Verify strict packet accounting conservation."""
        if self.benchmark_mode == "rate_controlled_load_test":
            expected = self.packets_submitted
            actual = self.packets_persisted + self.total_dropped() + self.packets_in_flight
            return expected == actual
        # Saturation microbenchmark
        return self.packets_received == (self.packets_verified + self.packets_dropped_rate + self.packets_dropped_bounds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite_name,
            "stage": self.stage_name,
            "benchmark_mode": self.benchmark_mode,
            "target_offered_rate_pps": self.target_offered_rate_pps,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "duration_seconds": round(self.duration_seconds, 3),
            "achieved_throughput_pps": self.achieved_throughput_pps,
            "packets": {
                "submitted": self.packets_submitted,
                "offered": self.packets_submitted,
                "received": self.packets_received,
                "verified": self.packets_verified,
                "sanitized": self.packets_sanitized,
                "dropped_rate": self.packets_dropped_rate,
                "dropped_bounds": self.packets_dropped_bounds,
                "dropped_anonymizer": self.packets_dropped_anonymizer,
                "dropped_queue": self.packets_dropped_queue,
                "total_dropped": self.total_dropped(),
                "persisted": self.packets_persisted,
                "in_flight": self.packets_in_flight,
                "is_conserved": self.is_conserved,
            },
            "resources": {
                "cpu_single_core_percent": self.cpu_utilization_single_core_percent,
                "cpu_total_host_percent": self.cpu_utilization_total_host_percent,
                "cpu_utilization_percent": self.cpu_utilization_percent,
                "host_cpus": self._host_cpus,
                "start_rss_mib": round(self._start_rss_mib, 2),
                "peak_rss_mib": round(self._peak_rss_mib, 2),
                "end_rss_mib": round(self._end_rss_mib, 2),
                "delta_rss_mib": round(self._peak_rss_mib - self._start_rss_mib, 2),
            },
            "queue_depth": self.get_queue_stats(),
            "latencies_ms": {k: v.to_dict() for k, v in self.latencies.items() if v._count > 0},
        }
