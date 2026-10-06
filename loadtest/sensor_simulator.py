"""
Virtual Sensor Grid Simulator — load generator for SIGS-RS.

SRS §2.4.1.3: Python load-generation scripts act as simulated community sensor nodes.

NOTE (Phase 1): Skeleton with profiles defined. Phase 9 adds the full
                asyncio MQTT publisher implementation.

Usage (Phase 9):
    python -m loadtest.sensor_simulator --profile smoke
    python -m loadtest.sensor_simulator --profile normal
    python -m loadtest.sensor_simulator --profile stress
    python -m loadtest.sensor_simulator --profile benchmark
    python -m loadtest.sensor_simulator --profile dos

Profiles (from Architecture Blueprint §15.2):
    smoke     — 10 sensors, 1 pkt/s, 30s
    normal    — 100 sensors, 5 pkt/s, 5min
    stress    — 1000 sensors, 5 pkt/s, 10min
    benchmark — 10000 sensors, 5 pkt/s, 5min  (needs 16vCPU, 32GB RAM)
    dos       — 50 sensors, 100 pkt/s, 2min (tests rate limiting)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone


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

RESOURCE_TYPES = ["water", "energy", "air_quality"]
UNITS = {
    "water": "liters_per_min",
    "energy": "kWh",
    "air_quality": "AQI",
}


def make_packet(sensor_id: str, malicious: bool = False) -> dict:
    """Generate a single telemetry packet payload."""
    resource = random.choice(RESOURCE_TYPES)
    if malicious:
        value = -999.0 if resource != "air_quality" else 9999.0
    else:
        value = round(random.uniform(0.1, 100.0), 2)

    return {
        "sensor_id": sensor_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "resource_type": resource,
        "value": value,
        "unit": UNITS[resource],
        "metadata": {"battery_level": random.randint(20, 100)},
        "geo": {
            "latitude": round(random.uniform(-90, 90), 6),
            "longitude": round(random.uniform(-180, 180), 6),
        },
        "mac_address": ":".join(f"{random.randint(0, 255):02X}" for _ in range(6)),
    }


async def run_profile(profile: LoadProfile) -> None:
    """
    Execute a load profile.
    Phase 9: replaces this stub with real MQTT publisher connections.
    """
    print(f"[simulator] Profile '{profile.name}' stub — Phase 9 implements MQTT publishing.")
    print(f"  Sensors: {profile.sensors}")
    print(f"  Rate:    {profile.rate_per_sensor} pkt/s per sensor")
    print(f"  Total:   {profile.sensors * profile.rate_per_sensor} pkt/s")
    print(f"  Duration:{profile.duration_seconds}s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SIGS-RS Load Simulator")
    parser.add_argument(
        "--profile",
        choices=list(PROFILES),
        default="smoke",
        help="Load test profile to run.",
    )
    args = parser.parse_args()
    profile = PROFILES[args.profile]
    asyncio.run(run_profile(profile))
