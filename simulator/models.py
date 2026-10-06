"""
Domain models, configuration, and statistics for the Virtual Sensor Grid Simulator.

SRS §2.4.1.3: Simulated community sensor nodes generating realistic JSON telemetry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import time
from typing import Any


class TrafficMode(str, Enum):
    """Supported traffic generation modes for the simulator."""
    NORMAL = "NORMAL"
    BURST = "BURST"
    RATE_ATTACK = "RATE_ATTACK"
    INVALID_METRICS = "INVALID_METRICS"
    MALFORMED_PAYLOAD = "MALFORMED_PAYLOAD"
    MIXED = "MIXED"

    @classmethod
    def from_str(cls, val: str) -> "TrafficMode":
        """Case-insensitive parsing."""
        normalized = val.strip().upper()
        for member in cls:
            if member.value == normalized:
                return member
        raise ValueError(
            f"Unknown traffic mode '{val}'. Available: {[m.value for m in cls]}"
        )


@dataclass
class SimulatorConfig:
    """Configuration parameters for the Virtual Sensor Grid Simulator."""
    sensors: int = 50
    packets_per_sec: float = 10.0
    duration_seconds: float = 10.0  # <= 0 means run indefinitely
    broker_host: str = "localhost"
    broker_port: int = 1883
    broker_username: str | None = None
    broker_password: str | None = None
    use_tls: bool = False
    ca_cert_path: str | None = None
    client_cert_path: str | None = None
    client_key_path: str | None = None
    insecure_skip_verify: bool = False
    topic: str = "telemetry/sensor-grid"
    topic_template: bool = False  # If True, appends /{resource_type}/{sensor_id}
    anomaly_percent: float = 0.0  # 0.0 to 100.0
    malformed_percent: float = 0.0  # 0.0 to 100.0
    resource_types: list[str] = field(default_factory=lambda: ["water", "energy", "air_quality"])
    seed: int | None = None
    traffic_mode: TrafficMode = TrafficMode.NORMAL
    dry_run: bool = False  # Generate without sending over network
    burst_interval: float = 3.0
    burst_multiplier: float = 5.0
    rate_attack_target: str | None = None
    rate_attack_burst_count: int = 60  # Intentionally exceeds 50 pkts / 10s


@dataclass
class SimulationStats:
    """Runtime and summary statistics for a simulation execution."""
    packets_generated: int = 0
    packets_sent: int = 0
    packets_failed: int = 0
    malformed_packets: int = 0
    malicious_packets: int = 0
    start_time: float = field(default_factory=time.monotonic)
    end_time: float = 0.0

    @property
    def elapsed_seconds(self) -> float:
        end = self.end_time if self.end_time > 0.0 else time.monotonic()
        return max(end - self.start_time, 0.0001)

    @property
    def packets_per_sec(self) -> float:
        # Based on packets sent if network active, otherwise generated
        count = self.packets_sent if self.packets_sent > 0 else self.packets_generated
        return count / self.elapsed_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "packets_generated": self.packets_generated,
            "packets_sent": self.packets_sent,
            "packets_failed": self.packets_failed,
            "packets_per_sec": round(self.packets_per_sec, 2),
            "malformed_packets": self.malformed_packets,
            "intentionally_malicious_packets": self.malicious_packets,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
        }

    def report(self) -> str:
        lines = [
            "======================================================",
            "        Virtual Sensor Grid Simulation Report         ",
            "======================================================",
            f"  Packets Generated:            {self.packets_generated:,}",
            f"  Packets Sent:                 {self.packets_sent:,}",
            f"  Packets Failed (network):     {self.packets_failed:,}",
            f"  Achieved Packets/sec:         {self.packets_per_sec:,.2f}",
            f"  Malformed Packets:            {self.malformed_packets:,}",
            f"  Intentionally Malicious:      {self.malicious_packets:,}",
            f"  Elapsed Time:                 {self.elapsed_seconds:,.2f} s",
            "======================================================",
        ]
        return "\n".join(lines)
