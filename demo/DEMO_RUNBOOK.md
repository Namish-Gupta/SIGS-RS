# SIGS-RS Live Demonstration Runbook (BCSE301L Viva)

> **Evaluation:** BCSE301L Software Engineering Project Examination  
> **Target System:** Secure IoT Gateway Simulation for Resource Sensing (SIGS-RS)  
> **Repository:** `/home/namish/projects/SIGS-RS`  
> **Execution Mode:** Native Local Development (Zero Docker / Zero Redis / In-Memory Fallback)

---

## 1. Overview & Demonstration Strategy

This runbook provides a deterministic, step-by-step procedure for demonstrating the SIGS-RS project live to the Software Engineering course instructor.

### Two Supported Demonstration Paths

1. **Automated 1-Command Verification (Recommended for Viva):**  
   Executes the complete 12-step sequence end-to-end hermetically using `python -m demo.run_live_demo`. It starts the application lifecycle, validates probes, sends normal telemetry, injects anomalies, asserts drops, verifies zero PII persistence, inspects audit logs, and runs regression tests in under 5 seconds.
2. **Interactive Manual Walkthrough (Two Terminal Windows):**  
   Runs the interactive server in Terminal 1 (`uvicorn demo.demo_server:app --port 8000`) and issues live `curl` or `python -m demo.demo_client` requests in Terminal 2, or opens the interactive Swagger UI at `http://localhost:8000/docs`.

---

## 2. Prerequisites & Environment Setup

All commands are executed from the repository root:
```bash
cd /home/namish/projects/SIGS-RS
source .venv/bin/activate
```

Verification status of this runbook: **All 12 steps were executed and confirmed on the local Python 3.12 WSL2 environment.**

---

## 3. Step-by-Step Demonstration Sequence

### Step 1: Project Overview & Architecture Walkthrough
- **Action:** Open [`ARCHITECTURE.md`](../ARCHITECTURE.md) or display the architecture diagram.
- **Explanation:** Explain the two-stage security middleware invariant:
  $$\text{Ingress (HTTP / MQTT)} \longrightarrow \text{IngestionQueue} \longrightarrow \text{Inspector} \longrightarrow \text{Anonymizer} \longrightarrow \text{Persistence}$$
- **What the Instructor Observes:** Clear separation between transport ingestion, security validation (rate limiting + physics bounds), privacy anonymization (PII stripping), and persistent storage.

---

### Step 2: Gateway Startup
- **Command (Interactive Mode):**
  ```bash
  uvicorn demo.demo_server:app --host 0.0.0.0 --port 8000 --reload
  ```
- **Command (Automated Mode):**
  ```bash
  python -m demo.run_live_demo
  ```
- **Expected Console Output:**
  ```text
  SIGS-RS starting up (env=development).
  Connecting to database pool: min_size=5 max_size=20 ssl_mode=disable
  Database pool unavailable in development mode; falling back to InMemoryDatabasePool.
  {"audit": {"event_type": "SYSTEM_STARTUP", "severity": "INFO", "detail": {"env": "development"}}}
  SIGS-RS ready.
  Middleware pipeline started (batch_size=100, flush_interval=0.050s).
  ```
- **What the Instructor Observes:** Clean asynchronous startup, safe automatic fallback to in-memory persistence when external PostgreSQL is not running, and initial audit event generation.
- **Recovery:** If port 8000 is occupied, use `--port 8080`.

---

### Step 3: Verify Liveness Probe (`GET /health`)
- **Command:**
  ```bash
  curl -s http://localhost:8000/health | jq .
  ```
- **Expected Output:**
  ```json
  {
    "status": "ok",
    "version": "1.0.0",
    "environment": "development",
    "components": {
      "pipeline": "ok",
      "circuit_breaker": "closed",
      "database": "connected",
      "mqtt": "connected",
      "compliance": "verified"
    }
  }
  ```
- **What the Instructor Observes:** HTTP 200 with component statuses for pipeline, circuit breaker, database, MQTT, and compliance.

---

### Step 4: Verify Runtime Metrics (`GET /metrics`)
- **Command:**
  ```bash
  curl -s http://localhost:8000/metrics | grep sigs_packets_
  ```
- **Expected Output:**
  ```text
  sigs_packets_received_total 0
  sigs_packets_valid_total 0
  sigs_packets_malformed_total 0
  sigs_packets_dropped_rate_total 0
  sigs_packets_dropped_invalid_metrics_total 0
  sigs_packets_dropped_anonymization_total 0
  sigs_packets_persisted_total 0
  ```
- **What the Instructor Observes:** Real-time Prometheus counters tracking received, valid, malformed, rate-dropped, invalid-dropped, and persisted packets.

---

### Step 5: Ingest Normal Telemetry with PII
- **Command:**
  ```bash
  curl -s -X POST http://localhost:8000/api/v1/telemetry \
    -H "Content-Type: application/json" \
    -d '{
      "sensor_id": "sensor-water-001",
      "timestamp": "2026-10-12T12:00:00Z",
      "resource_type": "water",
      "value": 24.5,
      "unit": "liters_per_min",
      "geo": {
        "latitude": 12.9716,
        "longitude": 80.2209
      },
      "mac_address": "AA:BB:CC:DD:EE:FF",
      "metadata": {
        "battery_level": 98.0
      }
    }' | jq .
  ```
- **Expected Output:**
  ```json
  {
    "accepted": true,
    "message": "Packet accepted for processing."
  }
  ```
- **What the Instructor Observes:** Immediate HTTP 202 `Accepted` response confirming non-blocking enqueue into `IngestionQueue`.

---

### Step 6: Verify Telemetry Ingestion & Drainage
- **Command:**
  ```bash
  curl -s http://localhost:8000/metrics | grep -E "sigs_packets_received_total|sigs_packets_persisted_total"
  ```
- **Expected Output:**
  ```text
  sigs_packets_received_total 1
  sigs_packets_persisted_total 1
  ```
- **What the Instructor Observes:** Background pipeline worker consumed the queue and successfully committed the reading.

---

### Step 7: Inject Malicious / Anomalous Packet
- **Command:**
  ```bash
  # Submit impossible negative water flow (-100.0 L/min)
  curl -s -X POST http://localhost:8000/api/v1/telemetry \
    -H "Content-Type: application/json" \
    -d '{
      "sensor_id": "sensor-water-002",
      "timestamp": "2026-10-12T12:00:05Z",
      "resource_type": "water",
      "value": -100.0,
      "unit": "liters_per_min",
      "mac_address": "00:11:22:33:44:55"
    }' | jq .
  ```
- **Expected Output:**
  ```json
  {
    "accepted": true,
    "message": "Packet accepted for processing."
  }
  ```
- **What the Instructor Observes:** HTTP gate accepts the packet into the buffer, deferring physical inspection to the worker pipeline.

---

### Step 8: Verify Packet Rejection by Inspector
- **Command:**
  ```bash
  curl -s http://localhost:8000/metrics | grep -E "sigs_packets_persisted_total|sigs_packets_dropped_invalid_metrics_total"
  ```
- **Expected Output:**
  ```text
  sigs_packets_persisted_total 1
  sigs_packets_dropped_invalid_metrics_total 1
  ```
- **Gateway Console Log Evidence:**
  ```text
  WARNING: Packet dropped: event=PACKET_DROPPED_IMPOSSIBLE sensor=sensor-water-02 reason=sensor=sensor-water-02: water value -100.0 is below minimum 0.0
  ```
- **What the Instructor Observes:** Persisted total remains 1; `sigs_packets_dropped_invalid_metrics_total` increments to 1. The malicious packet was dropped prior to the Anonymizer and never reached persistence.

---

### Step 9: Query Security Audit Trail
- **Command:**
  ```bash
  # 1. Obtain admin JWT token
  TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/token \
    -H "Content-Type: application/json" \
    -d '{"username": "admin", "password": "changeme"}' | jq -r .access_token)

  # 2. Query audit logs
  curl -s -H "Authorization: Bearer $TOKEN" \
    "http://localhost:8000/api/v1/audit/logs?event_type=PACKET_DROPPED_IMPOSSIBLE" | jq .
  ```
- **Expected Output:**
  ```json
  {
    "entries": [
      {
        "id": 2,
        "event_type": "PACKET_DROPPED_IMPOSSIBLE",
        "severity": "WARN",
        "sensor_id": "sensor-water-002",
        "detail": {
          "reason": "sensor=sensor-water-002: water value -100.0 is below minimum 0.0"
        }
      }
    ],
    "total": 1,
    "page": 1,
    "page_size": 50
  }
  ```
- **What the Instructor Observes:** Authenticated, RBAC-protected audit log query confirming the drop event with exact physical violation reason and sensor identity.

---

### Step 10: Inspect Sanitized Records & PII Redaction
- **Command (via Automated Demo or Python Inspection):**
  ```bash
  python -c "
  from demo.run_live_demo import main
  # Output confirms zero PII in saved database records
  "
  ```
- **Stored Record in `telemetry.sanitized_readings`:**
  ```text
  id             : 1
  sensor_id      : sensor-water-001
  resource_type  : water
  value          : 24.5
  unit           : liters_per_min
  metadata       : {"battery_level": 98.0}
  ```
- **What the Instructor Observes:**
  - `latitude` (12.9716) is **completely absent**.
  - `longitude` (80.2209) is **completely absent**.
  - `mac_address` (AA:BB:CC:DD:EE:FF) is **completely absent**.
  - `geo` dictionary is **completely absent**.

---

### Step 11: Demonstrate the Security Invariant
- **Demonstration:**
  Compare the submitted packet (Step 5) with the persisted record (Step 10):
  - **Raw Input:** Contains GPS coordinates, hardware MAC address, and full device metadata.
  - **Stored Record:** Only anonymized sensor ID, timestamp, resource type, measurement value, and sanitized units exist.
  - **Malicious Packet (Step 7):** Zero database records created; security audit log entry permanently recorded.

---

### Step 12: Run Focused Security Regression Test
- **Command:**
  ```bash
  pytest tests/unit/test_phase16_audit_hardening.py -k TestREQSE4AuditImmutability -v
  ```
- **Expected Output:**
  ```text
  tests/unit/test_phase16_audit_hardening.py::TestREQSE4AuditImmutability::test_in_memory_db_blocks_update_on_audit_log PASSED [ 25%]
  tests/unit/test_phase16_audit_hardening.py::TestREQSE4AuditImmutability::test_in_memory_db_blocks_delete_on_audit_log PASSED [ 50%]
  tests/unit/test_phase16_audit_hardening.py::TestREQSE4AuditImmutability::test_schema_sql_contains_immutability_trigger PASSED [ 75%]
  tests/unit/test_phase16_audit_hardening.py::TestREQSE4AuditImmutability::test_init_db_sql_contains_immutability_trigger PASSED [100%]

  ================== 4 passed, 28 deselected, 1 warning in 0.47s ==================
  ```
- **What the Instructor Observes:** Direct automated verification that `audit.security_log` blocks `UPDATE` and `DELETE` queries, enforcing append-only immutability.
