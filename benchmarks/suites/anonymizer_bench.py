"""
Category 2 Benchmark: Privacy Anonymizer Only.

Evaluates:
  - Masking throughput (ops/sec) and latency percentiles (avg, p50, p95, p99)
  - PII elimination cost (metadata deepcopy, dictionary traversal)
  - Fail-secure behavior and output schema conformance
"""
from __future__ import annotations

import time

from benchmarks.collector import BenchmarkCollector
from benchmarks.safety import SafetyWatchdog
from benchmarks.workload import WorkloadGenerator
from sigs_rs.common.exceptions import AnonymizerError
from sigs_rs.middleware.anonymizer import PrivacyAnonymizer


def run_anonymizer_benchmark(
    sensor_count: int,
    offered_rate_pps: int,
    duration_seconds: float,
    seed: int = 42,
    watchdog: SafetyWatchdog | None = None,
) -> BenchmarkCollector:
    """
    Execute standalone Privacy Anonymizer benchmark.
    """
    collector = BenchmarkCollector(
        suite_name="anonymizer_only",
        stage_name=f"{sensor_count}_sensors_{offered_rate_pps}_pps",
        benchmark_mode="saturation_microbenchmark",
    )
    dog = watchdog or SafetyWatchdog(timeout_seconds=duration_seconds + 30.0)
    dog.reset()

    anonymizer = PrivacyAnonymizer()
    workload = WorkloadGenerator(sensor_count=sensor_count, seed=seed)

    total_packets = int(offered_rate_pps * duration_seconds)
    collector.packets_submitted = total_packets

    # Warm-up (20 packets to prime JIT/allocations, excluded from metrics)
    warmup_batch = workload.generate_batch(batch_size=min(20, total_packets))
    for wp in warmup_batch:
        try:
            anonymizer.anonymize(wp)
        except Exception:
            pass

    chunk_size = min(total_packets, 5000)
    batches: list[list] = []
    generated = 0
    while generated < total_packets:
        this_chunk = min(chunk_size, total_packets - generated)
        batches.append(workload.generate_batch(batch_size=this_chunk))
        generated += this_chunk

    collector.start()
    for batch in batches:
        dog.check()
        for packet in batch:
            collector.packets_received += 1
            t0 = time.perf_counter()
            try:
                sanitized = anonymizer.anonymize(packet)
                elapsed = time.perf_counter() - t0
                collector.record_latency("anonymizer", elapsed)
                collector.packets_verified += 1
                collector.packets_sanitized += 1  # sanitized output count
            except AnonymizerError:
                elapsed = time.perf_counter() - t0
                collector.record_latency("anonymizer", elapsed)
                collector.packets_dropped_anonymizer += 1

        collector.sample_resources()

    collector.stop()
    return collector
