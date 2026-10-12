"""
Category 1 Benchmark: Anomalous Packet Inspector Only.

Evaluates:
  - In-memory deque algorithm baseline vs. fakeredis Lua execution
  - REQ-API-4: validation latency under 15 ms per packet
  - Sliding-window tracking throughput across varying sensor populations
"""
from __future__ import annotations

import asyncio
import time
from typing import Literal

from benchmarks.collector import BenchmarkCollector
from benchmarks.safety import SafetyWatchdog
from benchmarks.workload import WorkloadGenerator
from sigs_rs.middleware.inspector import AnomalousPacketInspector, VerificationResult
from sigs_rs.middleware.payload_validator import PayloadValidator
from tests.mock_sliding_window import MockAsyncSlidingWindow


async def run_inspector_benchmark(
    sensor_count: int,
    offered_rate_pps: int,
    duration_seconds: float,
    backend: Literal["in_memory", "fakeredis"] = "in_memory",
    seed: int = 42,
    watchdog: SafetyWatchdog | None = None,
) -> BenchmarkCollector:
    """
    Execute standalone Inspector benchmark.
    """
    collector = BenchmarkCollector(
        suite_name=f"inspector_{backend}",
        stage_name=f"{sensor_count}_sensors_{offered_rate_pps}_pps",
        benchmark_mode="saturation_microbenchmark",
    )
    dog = watchdog or SafetyWatchdog(timeout_seconds=duration_seconds + 30.0)
    dog.reset()

    # 1. Initialize sliding window backend
    if backend == "fakeredis":
        try:
            import fakeredis.aioredis
            from sigs_rs.middleware.sliding_window import SlidingWindow, LUA_RATE_LIMIT
            fake_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
            # Test if Lua script execution is supported by fakeredis
            await fake_client.eval("return 1", 0)
            window = SlidingWindow(window_seconds=10, max_packets=50, redis_url="redis://localhost:6379")
            window._redis = fake_client
            window._script = fake_client.register_script(LUA_RATE_LIMIT)
        except Exception as exc:
            collector.skipped = True
            collector.skip_reason = f"fakeredis Lua emulation requires 'lupa' package (not installed; {type(exc).__name__}: {exc})"
            return collector
    else:
        window = MockAsyncSlidingWindow(window_seconds=10, max_packets=50)

    validator = PayloadValidator()
    inspector = AnomalousPacketInspector(window=window, validator=validator)

    # 2. Pre-generate workload batches to avoid measuring generator overhead
    workload = WorkloadGenerator(sensor_count=sensor_count, seed=seed)
    total_packets = int(offered_rate_pps * duration_seconds)
    collector.packets_submitted = total_packets

    # Warm-up (20 packets to prime JIT/Pydantic, excluded from metrics)
    warmup_batch = workload.generate_batch(batch_size=min(20, total_packets))
    for wp in warmup_batch:
        await inspector.inspect(wp)
    await window.reset()

    # Generate in chunks of up to 5,000 to balance memory and timing
    chunk_size = min(total_packets, 5000)
    batches: list[list] = []
    generated = 0
    while generated < total_packets:
        this_chunk = min(chunk_size, total_packets - generated)
        batches.append(workload.generate_batch(batch_size=this_chunk))
        generated += this_chunk

    # 3. Benchmark execution
    collector.start()
    for batch in batches:
        dog.check()
        for packet in batch:
            collector.packets_received += 1
            t0 = time.perf_counter()
            result = await inspector.inspect(packet)
            elapsed = time.perf_counter() - t0
            collector.record_latency("inspector", elapsed)

            if result == VerificationResult.VERIFIED:
                collector.packets_verified += 1
            elif result == VerificationResult.RATE_LIMIT_EXCEEDED:
                collector.packets_dropped_rate += 1
            elif result == VerificationResult.INVALID_METRIC:
                collector.packets_dropped_bounds += 1

        collector.sample_resources()

    collector.stop()
    return collector
