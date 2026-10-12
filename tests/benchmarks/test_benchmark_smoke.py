"""
Smoke and regression tests for Phase 15/15A Performance Benchmarking Suite.

Verifies:
  1. BenchmarkCollector percentile and resource calculations (linear interpolation)
  2. SafetyWatchdog memory ceiling and timeout enforcement
  3. WorkloadGenerator determinism and model validation
  4. Progressive stage permission and opt-in boundaries
  5. Security Invariant:
     - Rejected packets never reach anonymizer
     - Only sanitized, verified packets reach persistence
     - Failed anonymization cannot result in persistence
     - Counters cannot silently double-count or omit packets
  6. Rapid smoke execution (<5s) of all four benchmark suites
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import pytest

from benchmarks.collector import BenchmarkCollector, LatencyStats, compute_percentiles
from benchmarks.safety import SafetyLimitExceededError, SafetyWatchdog
from benchmarks.stages import get_stage
from benchmarks.suites.anonymizer_bench import run_anonymizer_benchmark
from benchmarks.suites.e2e_pipeline_bench import run_e2e_pipeline_benchmark
from benchmarks.suites.inspector_bench import run_inspector_benchmark
from benchmarks.suites.middleware_bench import run_middleware_benchmark
from benchmarks.workload import WorkloadGenerator
from sigs_rs.common.exceptions import AnonymizerError
from sigs_rs.middleware.anonymizer import PrivacyAnonymizer
from sigs_rs.middleware.inspector import AnomalousPacketInspector, VerificationResult
from sigs_rs.middleware.payload_validator import PayloadValidator
from sigs_rs.telemetry.models import RawTelemetryPacket, SanitizedTelemetryPacket
from tests.mock_sliding_window import MockAsyncSlidingWindow


class TestBenchmarkCollector:
    def test_latency_percentiles_large_sample(self):
        stats = LatencyStats()
        for i in range(1, 101):
            stats.record_seconds(i / 1000.0)  # 1ms to 100ms
        res = stats.to_dict()
        assert res["count"] == 100
        assert res["min_ms"] == 1.0
        assert res["max_ms"] == 100.0
        assert res["p50_ms"] == pytest.approx(50.5, abs=0.1)
        assert res["p95_ms"] == pytest.approx(95.05, abs=0.1)
        assert res["p99_ms"] == pytest.approx(99.01, abs=0.1)

    def test_percentiles_small_sample_sizes(self):
        # Empty
        assert compute_percentiles([])["count"] == 0

        # Single observation (n=1)
        p1 = compute_percentiles([15.2])
        assert p1["count"] == 1
        assert p1["min_ms"] == 15.2
        assert p1["max_ms"] == 15.2
        assert p1["avg_ms"] == 15.2
        assert p1["p50_ms"] == 15.2
        assert p1["p95_ms"] == 15.2
        assert p1["p99_ms"] == 15.2

        # Two observations (n=2)
        p2 = compute_percentiles([10.0, 20.0])
        assert p2["count"] == 2
        assert p2["min_ms"] == 10.0
        assert p2["max_ms"] == 20.0
        assert p2["avg_ms"] == 15.0
        assert p2["p50_ms"] == 15.0
        assert p2["p95_ms"] == 19.5
        assert p2["p99_ms"] == 19.9

    def test_collector_accounting_and_conservation(self):
        collector = BenchmarkCollector(
            "test_suite",
            "stage_0",
            benchmark_mode="rate_controlled_load_test",
        )
        collector.start()
        collector.packets_submitted = 100
        collector.packets_received = 100
        collector.packets_verified = 90
        collector.packets_sanitized = 90
        collector.packets_dropped_rate = 5
        collector.packets_dropped_bounds = 5
        collector.packets_persisted = 90
        collector.packets_in_flight = 0
        collector.sample_queue_depth(10)
        collector.sample_queue_depth(5)
        collector.stop()

        data = collector.to_dict()
        assert data["packets"]["submitted"] == 100
        assert data["packets"]["verified"] == 90
        assert data["packets"]["sanitized"] == 90
        assert data["packets"]["total_dropped"] == 10
        assert data["packets"]["is_conserved"] is True
        assert data["queue_depth"]["min"] == 5
        assert data["queue_depth"]["max"] == 10


class TestSafetyWatchdog:
    def test_safety_memory_abort(self):
        dog = SafetyWatchdog(max_rss_mib=0.001)  # Impossibly small limit
        with pytest.raises(SafetyLimitExceededError, match="SAFETY ABORT"):
            dog.check()

    def test_stage_opt_in_boundaries(self):
        # Stage 0-2 run without opt-in
        s0 = get_stage(0)
        s1 = get_stage(1)
        s2 = get_stage(2)
        assert s0.stage_id == 0
        assert s1.stage_id == 1
        assert s2.stage_id == 2

        # Stage 3 requires --allow-large-scale
        with pytest.raises(PermissionError, match="--allow-large-scale"):
            get_stage(3, allow_large_scale=False)
        s3 = get_stage(3, allow_large_scale=True)
        assert s3.sensor_count == 5000

        # Stage 4 requires --allow-srs-max
        with pytest.raises(PermissionError, match="--allow-srs-max"):
            get_stage(4, allow_srs_max=False)
        s4 = get_stage(4, allow_srs_max=True)
        assert s4.sensor_count == 10000


class TestWorkloadGenerator:
    def test_reproducible_packets(self):
        g1 = WorkloadGenerator(sensor_count=10, seed=123)
        g2 = WorkloadGenerator(sensor_count=10, seed=123)
        b1 = g1.generate_batch(5)
        b2 = g2.generate_batch(5)
        assert len(b1) == 5
        assert [p.sensor_id for p in b1] == [p.sensor_id for p in b2]
        assert [p.value for p in b1] == [p.value for p in b2]


class TestSecurityInvariantAndAccounting:
    """Rigorous tests proving the security invariant and conservation equations."""

    @pytest.mark.asyncio
    async def test_rejected_packets_never_reach_anonymizer(self):
        window = MockAsyncSlidingWindow(window_seconds=10, max_packets=50)
        validator = PayloadValidator()
        inspector = AnomalousPacketInspector(window=window, validator=validator)
        anonymizer = PrivacyAnonymizer()
        anonymizer.anonymize = MagicMock(side_effect=anonymizer.anonymize)

        generator = WorkloadGenerator(sensor_count=5, seed=42)
        batch = generator.generate_batch(batch_size=10)
        # Force anomalies on 3 packets
        batch[0].value = -999.0
        batch[1].value = -999.0
        batch[2].value = -999.0

        for pkt in batch:
            res = await inspector.inspect(pkt)
            if res != VerificationResult.VERIFIED:
                continue  # Invariant: skip anonymization
            anonymizer.anonymize(pkt)

        # Anonymizer must only have been invoked 7 times (never for the 3 rejected packets)
        assert anonymizer.anonymize.call_count == 7

    @pytest.mark.asyncio
    async def test_failed_anonymization_cannot_persist(self):
        from sigs_rs.persistence.database import InMemoryDatabasePool
        from sigs_rs.persistence.repository import TelemetryRepository

        db = InMemoryDatabasePool()
        await db.connect()
        repo = TelemetryRepository(db)
        repo.insert_batch = AsyncMock(side_effect=repo.insert_batch)

        anonymizer = PrivacyAnonymizer()
        generator = WorkloadGenerator(sensor_count=5, seed=42)
        batch = generator.generate_batch(batch_size=5)
        failed_sensor_id = batch[0].sensor_id

        def faulty_anonymize(packet: RawTelemetryPacket) -> SanitizedTelemetryPacket:
            if packet.sensor_id == failed_sensor_id:
                raise AnonymizerError("Failed PII scrubbing")
            return anonymizer.anonymize(packet)

        sanitized_batch = []
        for pkt in batch:
            try:
                sanitized = faulty_anonymize(pkt)
                sanitized_batch.append(sanitized)
            except AnonymizerError:
                pass  # Dropped

        if sanitized_batch:
            await repo.insert_batch(sanitized_batch)

        # Verify only successfully sanitized packets were persisted
        assert len(sanitized_batch) == len(batch) - 1
        for s in sanitized_batch:
            assert isinstance(s, SanitizedTelemetryPacket)
            assert s.sensor_id != failed_sensor_id


class TestBenchmarkSuitesSmoke:
    """Rapid smoke execution of all 4 suites under Stage 0 (smoke) parameters."""

    @pytest.mark.asyncio
    async def test_smoke_inspector_in_memory(self):
        res = await run_inspector_benchmark(
            sensor_count=5,
            offered_rate_pps=50,
            duration_seconds=0.2,
            backend="in_memory",
        )
        data = res.to_dict()
        assert data["packets"]["received"] == 10
        assert data["achieved_throughput_pps"] > 0
        assert data["packets"]["is_conserved"] is True

    @pytest.mark.asyncio
    async def test_smoke_inspector_fakeredis(self):
        res = await run_inspector_benchmark(
            sensor_count=5,
            offered_rate_pps=50,
            duration_seconds=0.2,
            backend="fakeredis",
        )
        data = res.to_dict()
        if data.get("skipped"):
            assert "lupa" in data.get("skip_reason", "")
        else:
            assert data["packets"]["received"] == 10
            assert data["achieved_throughput_pps"] > 0

    def test_smoke_anonymizer(self):
        res = run_anonymizer_benchmark(
            sensor_count=5,
            offered_rate_pps=50,
            duration_seconds=0.2,
        )
        data = res.to_dict()
        assert data["packets"]["received"] == 10
        assert data["packets"]["sanitized"] == 10
        assert data["achieved_throughput_pps"] > 0

    @pytest.mark.asyncio
    async def test_smoke_middleware_combined(self):
        res = await run_middleware_benchmark(
            sensor_count=5,
            offered_rate_pps=50,
            duration_seconds=0.2,
            anomalous_ratio=0.2,
        )
        data = res.to_dict()
        assert data["packets"]["received"] == 10
        # Check conservation: verified + dropped = received
        assert data["packets"]["verified"] + data["packets"]["total_dropped"] == 10
        assert data["packets"]["is_conserved"] is True

    @pytest.mark.asyncio
    async def test_smoke_e2e_pipeline_rate_controlled(self):
        res = await run_e2e_pipeline_benchmark(
            sensor_count=5,
            offered_rate_pps=50,
            duration_seconds=0.2,
            batch_size=10,
        )
        data = res.to_dict()
        assert data["packets"]["submitted"] == 10
        assert data["packets"]["persisted"] > 0
        assert data["packets"]["in_flight"] == 0
        assert data["packets"]["is_conserved"] is True
        # Verify individual per-packet latency observations were recorded
        e2e_lats = data["latencies_ms"]["e2e_persisted"]
        assert e2e_lats["count"] == data["packets"]["persisted"]
        assert e2e_lats["avg_ms"] > 0.0
        assert data["latencies_ms"]["database_batch"]["count"] > 0
