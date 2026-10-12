"""
Category 4 Benchmark: Full End-to-End Pipeline (Concurrent Load Test).

Evaluates:
  - Concurrent Producer (paced at offered_rate_pps) -> IngestionQueue -> Batch Consumer -> InMemoryDatabasePool
  - Queue backpressure and dynamic depth tracking throughout execution
  - Real per-packet arrival-to-persistence latency distribution (avg, p50, p95, p99)
  - Explicit In-Memory Database batch execution latency
  - Strict packet accounting conservation and security invariant
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from benchmarks.collector import BenchmarkCollector
from benchmarks.safety import SafetyWatchdog
from benchmarks.workload import WorkloadGenerator
from sigs_rs.ingestion.buffer import IngestionQueue
from sigs_rs.middleware.anonymizer import PrivacyAnonymizer
from sigs_rs.middleware.inspector import AnomalousPacketInspector, VerificationResult
from sigs_rs.middleware.payload_validator import PayloadValidator
from sigs_rs.persistence.circuit_breaker import CircuitBreaker
from sigs_rs.persistence.database import InMemoryDatabasePool
from sigs_rs.persistence.repository import TelemetryRepository
from tests.mock_sliding_window import MockAsyncSlidingWindow


async def run_e2e_pipeline_benchmark(
    sensor_count: int,
    offered_rate_pps: int,
    duration_seconds: float,
    batch_size: int = 100,
    anomalous_ratio: float = 0.05,
    seed: int = 42,
    watchdog: SafetyWatchdog | None = None,
) -> BenchmarkCollector:
    """
    Execute rate-controlled concurrent end-to-end pipeline benchmark.
    """
    collector = BenchmarkCollector(
        suite_name="e2e_pipeline_inmemory",
        stage_name=f"{sensor_count}_sensors_{offered_rate_pps}_pps",
        benchmark_mode="rate_controlled_load_test",
        target_offered_rate_pps=offered_rate_pps,
    )
    dog = watchdog or SafetyWatchdog(timeout_seconds=duration_seconds + 30.0)
    dog.reset()

    # 1. Pipeline components
    queue = IngestionQueue(max_size=50_000)
    window = MockAsyncSlidingWindow(window_seconds=10, max_packets=50)
    validator = PayloadValidator()
    inspector = AnomalousPacketInspector(window=window, validator=validator)
    anonymizer = PrivacyAnonymizer()
    db = InMemoryDatabasePool()
    await db.connect()
    repo = TelemetryRepository(db)
    cb = CircuitBreaker(failure_threshold_ms=2000, cooldown_seconds=30)

    # 2. Workload preparation (pre-generate to eliminate generator overhead during timing)
    workload = WorkloadGenerator(sensor_count=sensor_count, seed=seed, anomalous_ratio=anomalous_ratio)
    total_packets = int(offered_rate_pps * duration_seconds)

    # Pre-generate all packets
    all_packets = workload.generate_batch(batch_size=total_packets)

    # 3. Warm-up (prime JIT, classes, and async event loop with 20 packets, excluded from metrics)
    warmup_packets = workload.generate_batch(batch_size=min(20, total_packets))
    for wp in warmup_packets:
        v = await inspector.inspect(wp)
        if v == VerificationResult.VERIFIED:
            san = anonymizer.anonymize(wp)
            await repo.insert_batch([san])
    await window.reset()
    await db.connect()

    # 4. Concurrent execution harness
    collector.start()
    t_start = time.perf_counter()

    async def producer() -> None:
        """Paces packet injection at offered_rate_pps."""
        submitted = 0
        tick_interval = 0.005  # 5 ms ticks
        packets_per_tick = max(1, int(offered_rate_pps * tick_interval))

        while submitted < total_packets:
            dog.check()
            target_count = min(
                packets_per_tick,
                total_packets - submitted,
            )

            # Determine if we need to wait to maintain target rate
            elapsed = time.perf_counter() - t_start
            expected_submitted = int(elapsed * offered_rate_pps)
            if submitted > expected_submitted:
                await asyncio.sleep(tick_interval)
                continue

            for _ in range(target_count):
                pkt = all_packets[submitted]
                t_enq = time.perf_counter()
                collector.packets_submitted += 1
                try:
                    queue._q.put_nowait((t_enq, pkt))
                    collector.packets_received += 1
                except asyncio.QueueFull:
                    collector.packets_dropped_queue += 1
                submitted += 1

            collector.sample_queue_depth(queue.depth)
            await asyncio.sleep(0)  # Yield control to consumer

        # Sentinel to signal producer completion
        await queue._q.put((None, None))

    async def consumer() -> None:
        """Continuously drains queue in batches, executing middleware and persistence."""
        while True:
            dog.check()
            collector.sample_queue_depth(queue.depth)

            # Fetch first item (blocking wait)
            t_enq, first_pkt = await queue._q.get()
            if first_pkt is None:
                queue.task_done()
                break

            batch: list[tuple[float, Any]] = [(t_enq, first_pkt)]
            queue.task_done()

            # Drain up to batch_size - 1 additional items non-blocking
            sentinel_seen = False
            while len(batch) < batch_size and queue.depth > 0:
                try:
                    t_item, pkt_item = queue._q.get_nowait()
                    if pkt_item is None:
                        sentinel_seen = True
                        queue.task_done()
                        break
                    batch.append((t_item, pkt_item))
                    queue.task_done()
                except asyncio.QueueEmpty:
                    break

            collector.sample_queue_depth(queue.depth)

            # Process batch through middleware
            sanitized_batch: list[tuple[float, Any]] = []
            for t_enqueued, packet in batch:
                t_insp_start = time.perf_counter()
                insp_res = await inspector.inspect(packet)
                t_insp_end = time.perf_counter()
                collector.record_latency("inspector", t_insp_end - t_insp_start)

                if insp_res is not VerificationResult.VERIFIED:
                    if insp_res == VerificationResult.RATE_LIMIT_EXCEEDED:
                        collector.packets_dropped_rate += 1
                    elif insp_res == VerificationResult.INVALID_METRIC:
                        collector.packets_dropped_bounds += 1
                    collector.record_latency("e2e_dropped", time.perf_counter() - t_enqueued)
                    continue

                collector.packets_verified += 1

                t_anon_start = time.perf_counter()
                try:
                    sanitized = anonymizer.anonymize(packet)
                    t_anon_end = time.perf_counter()
                    collector.record_latency("anonymizer", t_anon_end - t_anon_start)
                    collector.record_latency("middleware", t_anon_end - t_insp_start)
                    sanitized_batch.append((t_enqueued, sanitized))
                except Exception:
                    collector.packets_dropped_anonymizer += 1
                    collector.record_latency("e2e_dropped", time.perf_counter() - t_enqueued)

            # Persist sanitized batch
            if sanitized_batch:
                t_db_start = time.perf_counter()
                await cb.call(repo.insert_batch([s for _, s in sanitized_batch]))
                t_db_elapsed = time.perf_counter() - t_db_start
                collector.record_latency("database_batch", t_db_elapsed)

                t_persisted = time.perf_counter()
                for t_enqueued, _ in sanitized_batch:
                    # Record per-packet arrival-to-persistence latency
                    collector.record_latency("e2e_persisted", t_persisted - t_enqueued)
                    collector.packets_persisted += 1

            collector.sample_resources()

            if sentinel_seen:
                break

    # Run producer and consumer concurrently
    await asyncio.gather(producer(), consumer())

    collector.packets_in_flight = queue.depth
    collector.stop()
    return collector
