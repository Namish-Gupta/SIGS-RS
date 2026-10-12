"""
SIGS-RS — Automated Live Demonstration Script (Phase 18A).

Executes the complete 12-step viva demonstration sequence hermetically:
1. Show project overview & architecture.
2. Gateway startup & component readiness.
3. Verify /health probe response.
4. Verify /metrics Prometheus counters.
5. Ingest normal telemetry (with GPS coordinates and MAC address).
6. Verify successful ingestion & batch drainage.
7. Inject impossible-value physical anomaly (-100 L/min flow).
8. Verify anomaly detection and packet drop.
9. Verify security audit logging of the dropped packet.
10. Inspect in-memory database records (telemetry.sanitized_readings).
11. Prove the Inspector -> Anonymizer -> Persistence invariant.
12. Execute focused security regression test suite.

Run with:
    python -m demo.run_live_demo
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

# Ensure hermetic execution without requiring live external Redis
import sigs_rs.main
from tests.mock_sliding_window import MockAsyncSlidingWindow
sigs_rs.main.SlidingWindow = MockAsyncSlidingWindow

from fastapi.testclient import TestClient
from sigs_rs.main import create_app


def print_step(num: int, title: str) -> None:
    print(f"\n{'='*75}")
    print(f"STEP {num}: {title}")
    print(f"{'='*75}")


def main() -> int:
    print("*" * 75)
    print("  SIGS-RS: Secure IoT Gateway Simulation for Resource Sensing")
    print("  Live Demonstration & Examination Viva Execution Suite (Phase 18)")
    print("*" * 75)

    # ──────────────────────────────────────────────────────────────────────────
    # Step 1: Show project overview and architecture
    # ──────────────────────────────────────────────────────────────────────────
    print_step(1, "Project Architecture & Invariant Overview")
    print("SIGS-RS enforces a two-stage sequential security middleware:")
    print("  Ingress (HTTP/MQTT) -> IngestionQueue -> Inspector -> Anonymizer -> Circuit Breaker -> Persistence")
    print("Core Invariants:")
    print("  [1] Anomalous Packet Inspector rejects impossible physics & rate limit bursts.")
    print("  [2] Dropped packets NEVER enter the Privacy Anonymizer or database.")
    print("  [3] Privacy Anonymizer strips GPS coords (lat/lon) & MAC addresses before DB persistence.")

    # ──────────────────────────────────────────────────────────────────────────
    # Step 2: Start the gateway
    # ──────────────────────────────────────────────────────────────────────────
    print_step(2, "Gateway Startup & Lifespan Initialization")
    app = create_app()
    client = TestClient(app)

    with client:
        print("  -> Application lifespan entered successfully.")
        print("  -> Database pool initialized:", type(app.state.db).__name__)
        print("  -> Security pipeline worker active:", app.state.pipeline._running)
        print("  -> Sliding window backend:", type(app.state.inspector._window).__name__)

        # ──────────────────────────────────────────────────────────────────────
        # Step 3: Verify /health
        # ──────────────────────────────────────────────────────────────────────
        print_step(3, "Verify Liveness Probe (GET /health)")
        res = client.get("/health")
        print(f"  HTTP Status: {res.status_code}")
        print("  Response Payload:")
        print(json.dumps(res.json(), indent=4))
        assert res.status_code == 200, "Health probe failed"
        assert res.json()["status"] == "ok", "System status is not ok"

        # ──────────────────────────────────────────────────────────────────────
        # Step 4: Verify /metrics
        # ──────────────────────────────────────────────────────────────────────
        print_step(4, "Verify Runtime Metrics (GET /metrics)")
        res = client.get("/metrics")
        print(f"  HTTP Status: {res.status_code}")
        metrics_lines = [line for line in res.text.splitlines() if line.startswith("sigs_packets_")]
        print("  Prometheus Telemetry Counters:")
        for line in metrics_lines[:6]:
            print("   ", line)
        assert res.status_code == 200, "Metrics endpoint failed"

        # ──────────────────────────────────────────────────────────────────────
        # Step 5: Ingest normal telemetry with PII
        # ──────────────────────────────────────────────────────────────────────
        print_step(5, "Ingest Normal Telemetry with PII (POST /api/v1/telemetry)")
        raw_water_packet = {
            "sensor_id": "sensor-water-001",
            "timestamp": "2026-10-12T12:00:00Z",
            "resource_type": "water",
            "value": 24.5,
            "unit": "liters_per_min",
            "geo": {
                "latitude": 12.9716,
                "longitude": 80.2209,
            },
            "mac_address": "AA:BB:CC:DD:EE:FF",
            "metadata": {
                "hardware_rev": "v2.1",
                "battery_level": 98.0,
            },
        }
        print("  Submitting Packet with Sensitive PII (lat=12.9716, lon=80.2209, MAC=AA:BB:CC:DD:EE:FF):")
        print(json.dumps(raw_water_packet, indent=4))

        res = client.post("/api/v1/telemetry", json=raw_water_packet)
        print(f"  HTTP Status: {res.status_code}")
        print("  Response:", res.json())
        assert res.status_code == 202, "Packet ingestion rejected"

        # Allow pipeline worker to drain queue and execute middleware
        time.sleep(0.15)

        # ──────────────────────────────────────────────────────────────────────
        # Step 6: Show accepted telemetry or observable processing evidence
        # ──────────────────────────────────────────────────────────────────────
        print_step(6, "Observable Ingestion & Pipeline Drainage Evidence")
        readings = app.state.db._tables["telemetry.sanitized_readings"]
        print(f"  Ingestion Queue Depth: {app.state.queue.depth}")
        print(f"  Total Persisted Readings in Database: {len(readings)}")
        assert len(readings) == 1, "Expected 1 reading to be persisted"

        # ──────────────────────────────────────────────────────────────────────
        # Step 7: Trigger anomalous traffic (impossible physical value)
        # ──────────────────────────────────────────────────────────────────────
        print_step(7, "Inject Malicious / Anomalous Packet (Impossible Physical Value)")
        impossible_packet = {
            "sensor_id": "sensor-water-002",
            "timestamp": "2026-10-12T12:00:05Z",
            "resource_type": "water",
            "value": -100.0,  # Negative water flow violates thermodynamic physical bounds
            "unit": "liters_per_min",
            "mac_address": "00:11:22:33:44:55",
        }
        print("  Submitting Malicious Reading (-100.0 L/min negative flow):")
        print(json.dumps(impossible_packet, indent=4))

        res = client.post("/api/v1/telemetry", json=impossible_packet)
        print(f"  Ingestion Gate Acceptance: HTTP {res.status_code} (Enqueued for Inspection)")
        time.sleep(0.15)

        # ──────────────────────────────────────────────────────────────────────
        # Step 8: Show evidence that malicious packet was dropped
        # ──────────────────────────────────────────────────────────────────────
        print_step(8, "Verification Evidence: Anomaly Dropped by Inspector")
        print(f"  Persisted Readings Count in Database: {len(readings)}")
        assert len(readings) == 1, "Malicious packet illegally reached persistent storage!"
        print("  [CONFIRMED] Malicious reading was dropped by the Inspector and DID NOT reach persistence.")

        # ──────────────────────────────────────────────────────────────────────
        # Step 9: Show relevant audit / logging evidence
        # ──────────────────────────────────────────────────────────────────────
        print_step(9, "Security Audit Log Evidence (audit.security_log)")
        audit_logs = app.state.db._tables["audit.security_log"]
        print(f"  Total Immutable Audit Log Records: {len(audit_logs)}")
        for entry in audit_logs:
            print(f"  -> [{entry.get('severity')}] {entry.get('event_type')}: {entry.get('detail')} (sensor={entry.get('sensor_id')})")

        anomaly_events = [e for e in audit_logs if e.get("event_type") == "PACKET_DROPPED_IMPOSSIBLE"]
        assert len(anomaly_events) > 0, "No audit entry created for anomalous packet drop!"
        print("  [CONFIRMED] Anomaly drop was durably logged to audit trail.")

        # ──────────────────────────────────────────────────────────────────────
        # Step 10: Inspect sanitized records in database
        # ──────────────────────────────────────────────────────────────────────
        print_step(10, "Inspect Sanitized Database Record (telemetry.sanitized_readings)")
        saved_reading = readings[0]
        print("  Stored Record Fields:")
        for k, v in saved_reading.items():
            print(f"    {k:15}: {v}")

        # ──────────────────────────────────────────────────────────────────────
        # Step 11: Demonstrate the Inspector -> Anonymizer -> Persistence Invariant
        # ──────────────────────────────────────────────────────────────────────
        print_step(11, "Security Invariant Verification (PII Redaction Proof)")
        raw_repr = str(saved_reading)
        has_lat = "latitude" in raw_repr or "12.9716" in raw_repr
        has_lon = "longitude" in raw_repr or "80.2209" in raw_repr
        has_mac = "AA:BB:CC:DD:EE:FF" in raw_repr or "mac_address" in raw_repr
        has_geo = "geo" in saved_reading.get("metadata", {})

        print(f"  Checking for 'latitude' or coordinates: {'VIOLATION (Found)' if has_lat else 'VERIFIED (Zero PII)'}")
        print(f"  Checking for 'longitude' or coordinates: {'VIOLATION (Found)' if has_lon else 'VERIFIED (Zero PII)'}")
        print(f"  Checking for hardware 'mac_address':     {'VIOLATION (Found)' if has_mac else 'VERIFIED (Zero PII)'}")
        assert not has_lat and not has_lon and not has_mac, "PII leak detected in persisted database record!"
        print("  [CONFIRMED] Anonymizer completely stripped all spatial and device identifiers.")

    # ──────────────────────────────────────────────────────────────────────────
    # Step 12: Show a focused regression test passing
    # ──────────────────────────────────────────────────────────────────────────
    print_step(12, "Focused Security Regression Test Execution")
    import subprocess
    cmd = [sys.executable, "-m", "pytest", "tests/unit/test_phase16_audit_hardening.py", "-k", "TestREQSE4AuditImmutability", "-v"]
    print("  Running command:", " ".join(cmd))
    res = subprocess.run(cmd, capture_output=True, text=True)
    print("  Pytest Output:\n" + res.stdout.strip())
    assert res.returncode == 0, "Regression test failed!"
    print("  [CONFIRMED] Focused security regression suite passed cleanly.")

    print("\n" + "*" * 75)
    print("  ALL 12 DEMONSTRATION STEPS EXECUTED & VERIFIED SUCCESSFULLY!")
    print("*" * 75)
    return 0


if __name__ == "__main__":
    sys.exit(main())
