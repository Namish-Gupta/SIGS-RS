"""
Workload generation module for SIGS-RS performance benchmarks.

Provides reproducible, controlled virtual sensor telemetry using
simulator.generator.TelemetryGenerator with deterministic seeds.
"""
from __future__ import annotations

import json
from typing import Any
from sigs_rs.telemetry.models import RawTelemetryPacket
from simulator.generator import TelemetryGenerator


class WorkloadGenerator:
    """
    Produces deterministic streams of compliant and anomalous packets for benchmarking.
    """

    def __init__(
        self,
        sensor_count: int = 100,
        seed: int = 42,
        anomalous_ratio: float = 0.0,
    ) -> None:
        self.sensor_count = max(1, sensor_count)
        self.seed = seed
        self.anomalous_ratio = anomalous_ratio
        self._gen = TelemetryGenerator(
            sensor_count=self.sensor_count,
            seed=self.seed,
        )

    def generate_packet_model(
        self,
        sensor_index: int | None = None,
        force_anomaly: bool = False,
    ) -> RawTelemetryPacket:
        """Generate a validated RawTelemetryPacket model directly."""
        sensor = self._gen.get_sensor(sensor_index)
        pkt = self._gen.generate_packet(
            sensor=sensor,
            force_anomaly=False,
        )
        if isinstance(pkt.payload, (bytes, str)):
            data = json.loads(pkt.payload)
        else:
            data = pkt.payload

        if force_anomaly:
            # Inject impossible environmental metric value (SRS REQ-API-3)
            if data.get("resource_type") == "air_quality":
                data["value"] = 999.0
            else:
                data["value"] = -100.0

        return RawTelemetryPacket.model_validate(data)

    def generate_batch(
        self,
        batch_size: int,
        sensor_pool_size: int | None = None,
    ) -> list[RawTelemetryPacket]:
        """
        Pre-generate a batch of packets to avoid measuring generator overhead.
        """
        pool = sensor_pool_size or self.sensor_count
        batch: list[RawTelemetryPacket] = []
        for i in range(batch_size):
            s_idx = i % pool
            force_ano = (self.anomalous_ratio > 0) and ((i % int(1.0 / self.anomalous_ratio)) == 0)
            batch.append(self.generate_packet_model(sensor_index=s_idx, force_anomaly=force_ano))
        return batch
