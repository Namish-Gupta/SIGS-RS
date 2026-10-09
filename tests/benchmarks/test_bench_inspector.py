"""
Performance benchmarks for the Anomalous Packet Inspector pipeline.

Run with: python -m pytest tests/benchmarks/ -v -s
   or:    python tests/benchmarks/bench_inspector.py

Measures:
  1. SlidingWindow.check() throughput  (ops/sec)
  2. PayloadValidator.validate()       (ops/sec)
  3. Full Inspector.inspect()          (ops/sec, latency p50/p95/p99)
  4. Multi-sensor throughput           (ops/sec with 1000 sensors)
  5. Rate-limit storm (all rejected)   (ops/sec under rejection load)

SRS REQ-API-4: validation must complete in < 15 ms per packet.
"""
from __future__ import annotations

import statistics
import time

import pytest

from sigs_rs.middleware.inspector import AnomalousPacketInspector, VerificationResult
from sigs_rs.middleware.payload_validator import PayloadValidator
from tests.mock_sliding_window import MockAsyncSlidingWindow as SlidingWindow
from sigs_rs.telemetry.models import RawTelemetryPacket


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_packet(sensor_id: str = "bench-sensor", resource_type: str = "water", value: float = 42.0) -> RawTelemetryPacket:
    _units = {"water": "liters_per_min", "energy": "kWh", "air_quality": "AQI"}
    return RawTelemetryPacket(
        sensor_id=sensor_id,
        timestamp="2026-01-01T00:00:00Z",
        resource_type=resource_type,
        value=value,
        unit=_units[resource_type],
    )


# ─── Benchmark: SlidingWindow.check() ─────────────────────────────────────────

class TestSlidingWindowBenchmark:
    async def test_throughput_single_sensor(self):
        """Benchmark SlidingWindow.check() for a single sensor below rate limit."""
        sw = SlidingWindow(window_seconds=10, max_packets=1_000_000)
        n = 100_000
        t0 = time.perf_counter()
        for i in range(n):
            await sw.check("s1", now=1000.0 + i * 0.0001)
        elapsed = time.perf_counter() - t0
        ops_per_sec = n / elapsed
        print(f"\n  SlidingWindow.check (single sensor): {ops_per_sec:,.0f} ops/sec ({elapsed*1000:.1f}ms for {n:,} ops)")
        assert ops_per_sec > 100_000, "Expected > 100K ops/sec for single-sensor check"

    async def test_throughput_multi_sensor(self):
        """Benchmark check() across 1000 different sensors."""
        sw = SlidingWindow(window_seconds=10, max_packets=1_000_000)
        n = 100_000
        t0 = time.perf_counter()
        for i in range(n):
            await sw.check(f"sensor-{i % 1000}", now=1000.0 + i * 0.0001)
        elapsed = time.perf_counter() - t0
        ops_per_sec = n / elapsed
        print(f"\n  SlidingWindow.check (1000 sensors): {ops_per_sec:,.0f} ops/sec ({elapsed*1000:.1f}ms for {n:,} ops)")
        assert ops_per_sec > 50_000, "Expected > 50K ops/sec for multi-sensor check"

    async def test_eviction_performance(self):
        """Benchmark when every call triggers eviction of expired entries."""
        sw = SlidingWindow(window_seconds=10, max_packets=1_000_000)
        n = 50_000
        # Fill the window with old entries
        for i in range(50):
            await sw.check("s1", now=0.0 + i * 0.01)
        # Now each new call evicts old + adds new
        t0 = time.perf_counter()
        for i in range(n):
            await sw.check("s1", now=100.0 + i * 0.0001)
        elapsed = time.perf_counter() - t0
        ops_per_sec = n / elapsed
        print(f"\n  SlidingWindow.check (with eviction): {ops_per_sec:,.0f} ops/sec")
        assert ops_per_sec > 50_000


# ─── Benchmark: PayloadValidator.validate() ────────────────────────────────────

class TestPayloadValidatorBenchmark:
    async def test_throughput(self):
        """Benchmark PayloadValidator.validate() for valid packets."""
        v = PayloadValidator()
        packet = _make_packet()
        n = 100_000
        t0 = time.perf_counter()
        for _ in range(n):
            v.validate(packet)
        elapsed = time.perf_counter() - t0
        ops_per_sec = n / elapsed
        print(f"\n  PayloadValidator.validate: {ops_per_sec:,.0f} ops/sec ({elapsed*1000:.1f}ms for {n:,} ops)")
        assert ops_per_sec > 500_000, "Expected > 500K ops/sec for value validation"


# ─── Benchmark: Full Inspector.inspect() ──────────────────────────────────────

class TestInspectorBenchmark:
    async def test_throughput(self):
        """Full pipeline: SlidingWindow + PayloadValidator, single sensor."""
        insp = AnomalousPacketInspector(
            window=SlidingWindow(window_seconds=10, max_packets=1_000_000),
            validator=PayloadValidator(),
        )
        packet = _make_packet()
        n = 50_000
        t0 = time.perf_counter()
        for _ in range(n):
            await insp.inspect(packet)
        elapsed = time.perf_counter() - t0
        ops_per_sec = n / elapsed
        print(f"\n  Inspector.inspect (full pipeline): {ops_per_sec:,.0f} ops/sec ({elapsed*1000:.1f}ms for {n:,} ops)")
        assert ops_per_sec > 50_000

    async def test_latency_p99(self):
        """Measure per-packet latency and verify < 15ms (SRS REQ-API-4)."""
        insp = AnomalousPacketInspector(
            window=SlidingWindow(window_seconds=10, max_packets=1_000_000),
            validator=PayloadValidator(),
        )
        packet = _make_packet()
        latencies: list[float] = []
        n = 10_000
        for _ in range(n):
            t0 = time.perf_counter()
            await insp.inspect(packet)
            latencies.append((time.perf_counter() - t0) * 1000)  # ms

        p50 = statistics.median(latencies)
        p95 = sorted(latencies)[int(n * 0.95)]
        p99 = sorted(latencies)[int(n * 0.99)]
        p_max = max(latencies)
        print(f"\n  Inspector latency (ms): p50={p50:.4f} p95={p95:.4f} p99={p99:.4f} max={p_max:.4f}")
        assert p99 < 15.0, f"SRS REQ-API-4: p99 latency {p99:.2f}ms exceeds 15ms"

    async def test_multi_sensor_throughput(self):
        """1000 distinct sensors, each under the rate limit."""
        insp = AnomalousPacketInspector(
            window=SlidingWindow(window_seconds=10, max_packets=1_000_000),
            validator=PayloadValidator(),
        )
        n = 50_000
        packets = [_make_packet(sensor_id=f"sensor-{i % 1000}") for i in range(n)]
        t0 = time.perf_counter()
        for p in packets:
            await insp.inspect(p)
        elapsed = time.perf_counter() - t0
        ops_per_sec = n / elapsed
        print(f"\n  Inspector.inspect (1000 sensors): {ops_per_sec:,.0f} ops/sec ({elapsed*1000:.1f}ms for {n:,} ops)")
        assert ops_per_sec > 30_000

    async def test_rejection_storm_throughput(self):
        """All packets rejected (rate-limited) — tests fast-path performance.
        Logging is suppressed so we measure pure algorithm speed."""
        import logging
        inspector_logger = logging.getLogger("sigs_rs.middleware.inspector")
        original_level = inspector_logger.level
        inspector_logger.setLevel(logging.CRITICAL)  # suppress WARNING
        try:
            insp = AnomalousPacketInspector(
                window=SlidingWindow(window_seconds=10, max_packets=1),
                validator=PayloadValidator(),
            )
            packet = _make_packet()
            await insp.inspect(packet)  # first one passes, fills the 1-slot window

            n = 50_000
            t0 = time.perf_counter()
            for _ in range(n):
                await insp.inspect(packet)
            elapsed = time.perf_counter() - t0
            ops_per_sec = n / elapsed
            print(f"\n  Inspector.inspect (all rejected): {ops_per_sec:,.0f} ops/sec ({elapsed*1000:.1f}ms for {n:,} ops)")
            assert ops_per_sec > 100_000, "Rejection fast-path should be very fast"
            assert insp.stats["dropped_rate"] == n
        finally:
            inspector_logger.setLevel(original_level)


# ─── CLI runner ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    """Run benchmarks directly: python tests/benchmarks/bench_inspector.py"""
    print("=" * 70)
    print("SIGS-RS Anomalous Packet Inspector — Benchmarks")
    print("=" * 70)
    t = TestSlidingWindowBenchmark()
    t.test_throughput_single_sensor()
    t.test_throughput_multi_sensor()
    t.test_eviction_performance()

    t2 = TestPayloadValidatorBenchmark()
    t2.test_throughput()

    t3 = TestInspectorBenchmark()
    t3.test_throughput()
    t3.test_latency_p99()
    t3.test_multi_sensor_throughput()
    t3.test_rejection_storm_throughput()
    print("\n" + "=" * 70)
    print("All benchmarks passed.")
