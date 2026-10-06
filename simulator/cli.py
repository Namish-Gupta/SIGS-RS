"""
Command-Line Interface (CLI) for the Virtual Sensor Grid Simulator.

Usage:
    python -m simulator --mode NORMAL --sensors 100 --rate 20 --duration 30
    python -m simulator --mode RATE_ATTACK --dry-run
    python -m simulator --mode INVALID_METRICS --sensors 50 --duration 10
    python -m simulator --mode MALFORMED_PAYLOAD --sensors 50
    python -m simulator --profile smoke --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from simulator.engine import Simulator
from simulator.models import SimulatorConfig, TrafficMode

PROFILES = {
    "smoke": {
        "sensors": 10,
        "packets_per_sec": 1.0,
        "duration_seconds": 30.0,
        "traffic_mode": TrafficMode.NORMAL,
    },
    "normal": {
        "sensors": 100,
        "packets_per_sec": 5.0,
        "duration_seconds": 300.0,
        "traffic_mode": TrafficMode.NORMAL,
    },
    "stress": {
        "sensors": 1000,
        "packets_per_sec": 50.0,
        "duration_seconds": 600.0,
        "traffic_mode": TrafficMode.NORMAL,
    },
    "benchmark": {
        "sensors": 10000,
        "packets_per_sec": 200.0,
        "duration_seconds": 300.0,
        "traffic_mode": TrafficMode.BURST,
    },
    "dos": {
        "sensors": 50,
        "packets_per_sec": 100.0,
        "duration_seconds": 120.0,
        "traffic_mode": TrafficMode.RATE_ATTACK,
    },
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m simulator",
        description="SIGS-RS Virtual Sensor Grid IoT Telemetry Simulator",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # General configuration
    parser.add_argument(
        "--profile",
        choices=list(PROFILES.keys()),
        default=None,
        help="Predefined load testing profile (smoke, normal, stress, benchmark, dos)",
    )
    parser.add_argument(
        "--sensors", "-s",
        type=int,
        default=None,
        help="Number of virtual IoT sensors to simulate (default: 50)",
    )
    parser.add_argument(
        "--rate", "-r",
        dest="packets_per_sec",
        type=float,
        default=None,
        help="Target total telemetry packets per second (default: 10.0)",
    )
    parser.add_argument(
        "--duration", "-d",
        dest="duration_seconds",
        type=float,
        default=None,
        help="Duration of simulation in seconds (0 for indefinite, default: 10.0)",
    )

    # Traffic mode
    parser.add_argument(
        "--mode", "-m",
        dest="traffic_mode",
        type=str,
        default=None,
        choices=[m.value for m in TrafficMode] + [m.value.lower() for m in TrafficMode],
        help="Traffic mode (NORMAL, BURST, RATE_ATTACK, INVALID_METRICS, MALFORMED_PAYLOAD, MIXED, default: NORMAL)",
    )

    # MQTT configuration
    parser.add_argument(
        "--broker-host", "--host",
        dest="broker_host",
        type=str,
        default="localhost",
        help="MQTT broker hostname or IP address",
    )
    parser.add_argument(
        "--broker-port", "--port",
        dest="broker_port",
        type=int,
        default=1883,
        help="MQTT broker port (8883 for TLS, 1883 for plaintext)",
    )
    parser.add_argument(
        "--topic", "-t",
        type=str,
        default="telemetry/sensor-grid",
        help="Target MQTT topic",
    )
    parser.add_argument(
        "--topic-template",
        action="store_true",
        help="Append /{resource_type}/{sensor_id} to the MQTT topic",
    )

    # Security & TLS
    parser.add_argument(
        "--use-tls",
        action="store_true",
        help="Enable TLS encryption (default port 8883)",
    )
    parser.add_argument(
        "--ca-cert",
        dest="ca_cert_path",
        type=str,
        default=None,
        help="Path to CA certificate file for TLS validation",
    )
    parser.add_argument(
        "--client-cert",
        dest="client_cert_path",
        type=str,
        default=None,
        help="Path to client certificate file (mTLS)",
    )
    parser.add_argument(
        "--client-key",
        dest="client_key_path",
        type=str,
        default=None,
        help="Path to client private key file (mTLS)",
    )
    parser.add_argument(
        "--insecure",
        dest="insecure_skip_verify",
        action="store_true",
        help="Skip TLS hostname validation (for development)",
    )

    # Packet characteristics
    parser.add_argument(
        "--anomaly-percent", "--anomaly-rate",
        dest="anomaly_percent",
        type=float,
        default=0.0,
        help="Percentage of anomalous packets (0.0 to 100.0)",
    )
    parser.add_argument(
        "--malformed-percent", "--malformed-rate",
        dest="malformed_percent",
        type=float,
        default=0.0,
        help="Percentage of malformed packets (0.0 to 100.0)",
    )
    parser.add_argument(
        "--resources", "--resource-types",
        dest="resource_types",
        type=str,
        default="water,energy,air_quality",
        help="Comma-separated list of sensor resource types",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Reproducible random number generator seed",
    )

    # Execution flags
    parser.add_argument(
        "--dry-run", "--no-mqtt",
        dest="dry_run",
        action="store_true",
        help="Simulate telemetry generation without publishing to an MQTT broker",
    )
    parser.add_argument(
        "--json-output",
        action="store_true",
        help="Print simulation report as structured JSON",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable detailed debug logging",
    )
    parser.add_argument(
        "--quiet", "-q",
        action="store_true",
        help="Suppress progress output",
    )

    return parser


def parse_args_to_config(args: argparse.Namespace) -> SimulatorConfig:
    config_dict: dict = {
        "sensors": 50,
        "packets_per_sec": 10.0,
        "duration_seconds": 10.0,
        "traffic_mode": TrafficMode.NORMAL,
    }

    # If profile is specified, start with profile baseline
    if args.profile and args.profile in PROFILES:
        config_dict.update(PROFILES[args.profile])

    # Command line args override profile baseline if explicitly specified
    if args.sensors is not None:
        config_dict["sensors"] = args.sensors
    if args.packets_per_sec is not None:
        config_dict["packets_per_sec"] = args.packets_per_sec
    if args.duration_seconds is not None:
        config_dict["duration_seconds"] = args.duration_seconds
    if args.traffic_mode is not None:
        config_dict["traffic_mode"] = TrafficMode.from_str(args.traffic_mode)

    # Parse resources
    resources = [r.strip().lower() for r in args.resource_types.split(",") if r.strip()]

    # Normalize anomaly and malformed rates if passed as fraction <= 1.0 (e.g. 0.05 -> 5.0)
    anomaly_pct = args.anomaly_percent
    if 0.0 < anomaly_pct <= 1.0:
        anomaly_pct *= 100.0

    malformed_pct = args.malformed_percent
    if 0.0 < malformed_pct <= 1.0:
        malformed_pct *= 100.0

    config_dict.update({
        "broker_host": args.broker_host,
        "broker_port": 8883 if (args.use_tls and args.broker_port == 1883) else args.broker_port,
        "topic": args.topic,
        "topic_template": args.topic_template,
        "use_tls": args.use_tls,
        "ca_cert_path": args.ca_cert_path,
        "client_cert_path": args.client_cert_path,
        "client_key_path": args.client_key_path,
        "insecure_skip_verify": args.insecure_skip_verify,
        "anomaly_percent": anomaly_pct,
        "malformed_percent": malformed_pct,
        "resource_types": resources,
        "seed": args.seed,
        "dry_run": args.dry_run,
    })

    return SimulatorConfig(**config_dict)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = parse_args_to_config(args)
    simulator = Simulator(config)

    try:
        stats = asyncio.run(simulator.run())
    except KeyboardInterrupt:
        print("\n[simulator] Simulation interrupted by user.")
        stats = simulator.stats
    except Exception as exc:
        print(f"\n[simulator] Simulation error: {exc}", file=sys.stderr)
        return 1

    if args.json_output:
        print(json.dumps(stats.to_dict(), indent=2))
    else:
        print("\n" + stats.report())

    return 0


if __name__ == "__main__":
    sys.exit(main())
