"""
SIGS-RS — Interactive Demonstration Client (Phase 18A).

Sends sample telemetry to an actively running gateway instance (e.g. uvicorn demo.demo_server:app).

Usage:
    python -m demo.demo_client --mode normal
    python -m demo.demo_client --mode anomaly
    python -m demo.demo_client --mode burst
    python -m demo.demo_client --mode health
    python -m demo.demo_client --mode audit
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def http_req(url: str, method: str = "GET", data: dict | None = None, headers: dict | None = None) -> tuple[int, dict | str]:
    headers = headers or {}
    encoded_data = None
    if data is not None:
        encoded_data = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    
    req = urllib.request.Request(url, data=encoded_data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(body)
            except Exception:
                return resp.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body
    except Exception as exc:
        print(f"[ERROR] Connection to {url} failed: {exc}", file=sys.stderr)
        return 0, str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="SIGS-RS Demonstration Client")
    parser.add_argument("--url", default="http://localhost:8000", help="Base URL of the running gateway")
    parser.add_argument("--mode", choices=["normal", "anomaly", "burst", "health", "audit"], default="normal", help="Action to execute")
    args = parser.parse_args()

    base_url = args.url.rstrip("/")

    if args.mode == "health":
        print(f"Querying {base_url}/health...")
        status, data = http_req(f"{base_url}/health")
        print(f"HTTP Status: {status}")
        print(json.dumps(data, indent=2) if isinstance(data, dict) else data)
        return 0 if status == 200 else 1

    elif args.mode == "normal":
        packet = {
            "sensor_id": "sensor-water-001",
            "timestamp": "2026-10-12T12:00:00Z",
            "resource_type": "water",
            "value": 24.5,
            "unit": "liters_per_min",
            "geo": {"latitude": 12.9716, "longitude": 80.2209},
            "mac_address": "AA:BB:CC:DD:EE:FF",
            "metadata": {"battery_level": 98.0}
        }
        print(f"Sending normal telemetry with PII to {base_url}/api/v1/telemetry...")
        status, data = http_req(f"{base_url}/api/v1/telemetry", method="POST", data=packet)
        print(f"HTTP Status: {status}")
        print(json.dumps(data, indent=2) if isinstance(data, dict) else data)
        return 0 if status in (200, 202) else 1

    elif args.mode == "anomaly":
        packet = {
            "sensor_id": "sensor-water-002",
            "timestamp": "2026-10-12T12:00:01Z",
            "resource_type": "water",
            "value": -100.0,  # Negative water flow
            "unit": "liters_per_min",
            "mac_address": "00:11:22:33:44:55"
        }
        print(f"Sending impossible physical value (-100.0 L/min) to {base_url}/api/v1/telemetry...")
        status, data = http_req(f"{base_url}/api/v1/telemetry", method="POST", data=packet)
        print(f"HTTP Status: {status} (Accepted at HTTP gate, dropped in Inspector)")
        print(json.dumps(data, indent=2) if isinstance(data, dict) else data)
        return 0

    elif args.mode == "burst":
        print(f"Sending 55 burst packets from 'sensor-burst-01' to trigger rate limiter...")
        accepted = 0
        for i in range(55):
            packet = {
                "sensor_id": "sensor-burst-01",
                "timestamp": "2026-10-12T12:00:00Z",
                "resource_type": "energy",
                "value": 1.0 + i * 0.1,
                "unit": "kWh"
            }
            status, _ = http_req(f"{base_url}/api/v1/telemetry", method="POST", data=packet)
            if status in (200, 202):
                accepted += 1
        print(f"Submitted 55 packets to ingestion queue: {accepted} enqueued.")
        print("Note: The Inspector sliding window will accept the first 50 and drop packets 51-55.")
        return 0

    elif args.mode == "audit":
        print("Obtaining JWT admin token...")
        token_status, token_data = http_req(
            f"{base_url}/api/v1/auth/token",
            method="POST",
            data={"username": "admin", "password": "changeme"}
        )
        if token_status != 200 or not isinstance(token_data, dict):
            print(f"[ERROR] Failed to obtain token: {token_data}", file=sys.stderr)
            return 1
        token = token_data.get("access_token")
        print("Querying security audit logs...")
        status, data = http_req(
            f"{base_url}/api/v1/audit/logs",
            headers={"Authorization": f"Bearer {token}"}
        )
        print(f"HTTP Status: {status}")
        print(json.dumps(data, indent=2) if isinstance(data, dict) else data)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
