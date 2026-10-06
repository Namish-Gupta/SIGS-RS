"""
Virtual Sensor Grid Simulator — load generator for SIGS-RS.

SRS §2.4.1.3: Python load-generation scripts act as simulated community sensor nodes.

Usage (Phase 9):
    python -m loadtest.sensor_simulator --profile smoke
    python -m loadtest.sensor_simulator --profile normal
    python -m loadtest.sensor_simulator --profile stress
    python -m loadtest.sensor_simulator --profile benchmark
    python -m loadtest.sensor_simulator --profile dos
    python -m loadtest.sensor_simulator --mode RATE_ATTACK --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import sys

from simulator.cli import build_parser, parse_args_to_config
from simulator.engine import Simulator
from simulator.models import SimulationStats, SimulatorConfig, TrafficMode


@dataclass
class LoadProfile:
    name: str
    sensors: int
    rate_per_sensor: float   # packets per second
    duration_seconds: int
    malicious_ratio: float = 0.05   # fraction of packets that are spoofed


PROFILES: dict[str, LoadProfile] = {
    "smoke":     LoadProfile("smoke",     10,     1.0,   30),
    "normal":    LoadProfile("normal",    100,    5.0,   300),
    "stress":    LoadProfile("stress",    1000,   5.0,   600),
    "benchmark": LoadProfile("benchmark", 10000,  5.0,   300),
    "dos":       LoadProfile("dos",       50,     100.0, 120),
}


async def run_profile(
    profile: LoadProfile,
    dry_run: bool = False,
    broker_host: str = "localhost",
    broker_port: int = 1883,
    use_tls: bool = False,
) -> SimulationStats:
    """
    Execute a load profile using the full Virtual Sensor Grid simulator engine.
    """
    total_rate = profile.sensors * profile.rate_per_sensor
    mode = TrafficMode.RATE_ATTACK if profile.name == "dos" else TrafficMode.NORMAL

    config = SimulatorConfig(
        sensors=profile.sensors,
        packets_per_sec=total_rate,
        duration_seconds=float(profile.duration_seconds),
        anomaly_percent=profile.malicious_ratio * 100.0,
        traffic_mode=mode,
        broker_host=broker_host,
        broker_port=broker_port,
        use_tls=use_tls,
        dry_run=dry_run,
    )
    sim = Simulator(config)
    return await sim.run()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    config = parse_args_to_config(args)
    sim = Simulator(config)

    try:
        stats = asyncio.run(sim.run())
        print("\n" + stats.report())
        return 0
    except KeyboardInterrupt:
        print("\n[simulator] Simulation stopped by user.")
        return 0
    except Exception as exc:
        print(f"\n[simulator] Error running profile: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
