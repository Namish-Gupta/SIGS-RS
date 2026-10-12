"""
Category 3 Benchmark: Complete Inspector -> Anonymizer Middleware Pipeline.

Evaluates:
  - Strict security invariant: only verified packets pass to the Anonymizer
  - REQ-PF-2: combined middleware latency under 15 ms per packet
  - Stage-by-stage latency attribution and combined throughput
"""
from __future__ import annotations

import asyncio
import time
from typing import Literal

from benchmarks.collector import BenchmarkCollector
from benchmarks.safety import SafetyWatchdog
from benchmarks.workload import WorkloadGenerator
from sigs_rs.common.exceptions import AnonymizerError
from sigs_rs.middleware.anonymizer import PrivacyAnonymizer
from sigs_rs.middleware.inspector import AnomalousPacketInspector, VerificationResult
from sigs_rs.middleware.payload_validator import PayloadValidator
from tests.mock_sliding_window import MockAsyncSlidingWindow


async def run_middleware_benchmark(
    sensor_count: int,
    offered_rate_pps: int,
    duration_seconds: float,
    backend: Literal["in_memory", "fakeredis"] = "in_memory",
    anomalous_ratio: float = 0.05,
    seed: int = 42,
    watchdog: SafetyWatchdog | None = None,
) -> BenchmarkCollector:
    """
    Execute combined Inspector -> Anonymizer middleware benchmark.
    """
    collector = BenchmarkCollector(
        suite_name=f"middleware_combined_{backend}",
        stage_name=f"{sensor_count}_sensors_{offered_rate_pps}_pps",
        benchmark_mode="saturation_microbenchmark",
    )
    dog = watchdog or SafetyWatchdog(timeout_seconds=duration_seconds + 30.0)
    dog.reset()

    # 1. Initialize sliding window
    if backend == "fakeredis":
        import fakeredis.aioredis
        from sigs_rs.middleware.sliding_window import SlidingWindow, LUA_RATE_LIMIT
        fake_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
        window = SlidingWindow(window_seconds=10, max_packets=50, redis_url="redis://localhost:6379")
        window._redis = fake_client
        window._script = fake_client.register_script(LUA_RATE_LIMIT)
    else:
        window = MockAsyncSlidingWindow(window_seconds=10, max_packets=50)

    validator = PayloadValidator()
    inspector = AnomalousPacketInspector(window=window, validator=validator)
    anonymizer = PrivacyAnonymizer()

    # 2. Pre-generate workload with controlled anomaly ratio (e.g. 5%)
    workload = WorkloadGenerator(
        sensor_count=sensor_count,
        seed=seed,
        anomalous_ratio=anomalous_ratio,
    )
    total_packets = int(offered_rate_pps * duration_seconds)
    collector.packets_submitted = total_packets

    # Warm-up (20 packets to prime JIT/allocations, excluded from metrics)
    warmup_batch = workload.generate_batch(batch_size=min(20, total_packets))
    for wp in warmup_batch:
        res = await inspector.inspect(wp)
        if res == VerificationResult.VERIFIED:
            try:
                anonymizer.anonymize(wp)
            except Exception:
                pass
    await window.reset()

    chunk_size = min(total_packets, 5000)
    batches: list[list] = []
    generated = 0
    while generated < total_packets:
        this_chunk = min(chunk_size, total_packets - generated)
        batches.append(workload.generate_batch(batch_size=this_chunk))
        generated += this_chunk

    # 3. Execution loop
    collector.start()
    for batch in batches:
        dog.check()
        for packet in batch:
            collector.packets_received += 1
            t_mid_start = time.perf_counter()

            # Stage 1: Inspector
            t_insp_start = time.perf_counter()
            insp_res = await inspector.inspect(packet)
            t_insp_end = time.perf_counter()
            collector.record_latency("inspector", t_insp_end - t_insp_start)

            if insp_res is not VerificationResult.VERIFIED:
                if insp_res == VerificationResult.RATE_LIMIT_EXCEEDED:
                    collector.packets_dropped_rate += 1
                elif insp_res == VerificationResult.INVALID_METRIC:
                    collector.packets_dropped_bounds += 1
                # Dropped packet stops here; never reaches Anonymizer
                collector.record_latency("middleware", t_insp_end - t_mid_start)
                continue

            collector.packets_verified += 1

            # Stage 2: Privacy Anonymizer
            t_anon_start = time.perf_counter()
            try:
                sanitized = anonymizer.anonymize(packet)
                t_anon_end = time.perf_counter()
                collector.record_latency("anonymizer", t_anon_end - t_anon_start)
                collector.record_latency("middleware", t_anon_end - t_mid_start)
                collector.packets_sanitized += 1  # Successfully sanitized packet count
            except AnonymizerError:
                t_anon_end = time.perf_counter()
                collector.record_latency("anonymizer", t_anon_end - t_anon_start)
                collector.record_latency("middleware", t_anon_end - t_mid_start)
                collector.packets_dropped_anonymizer += 1

        collector.sample_resources()

    collector.stop()
    return collector
