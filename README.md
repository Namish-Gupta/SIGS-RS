# SIGS-RS — Secure IoT Gateway Simulation for Resource Sensing

> **Version 1.0.0** | Python 3.12 | FastAPI | PostgreSQL & TimescaleDB | MQTT 8883 MQTTS | Docker & Kubernetes

**SIGS-RS** is an asynchronous edge telemetry ingestion gateway and security middleware engine. It models a municipal sensor grid across water distribution, electric power grids, and atmospheric air quality monitoring. The platform processes high-velocity telemetry streams through a strict, sequential two-stage security middleware that eliminates anomalous or malicious readings and strips personally identifiable information (PII) before persistent storage.

---

## 1. Documentation Index

Detailed technical specifications and operational runbooks are maintained in dedicated reference documents:

| Document | Description |
| :--- | :--- |
| **[`ARCHITECTURE.md`](ARCHITECTURE.md)** | Subsystem breakdown, background concurrency tasks, architectural invariants, **Diagram 1 (System Architecture)**, and **Diagram 2 (Telemetry Sequence)**. |
| **[`API.md`](API.md)** | Comprehensive REST catalog of all 15 endpoints, schemas, authentication, RBAC roles, and HTTP status codes. |
| **[`DEPLOYMENT.md`](DEPLOYMENT.md)** | Local Python setup, Docker Compose multi-container stack, Kubernetes manifests, and **Diagram 4 (Deployment Architecture)**. |
| **[`SECURITY.md`](SECURITY.md)** | Threat model, transport security, sliding-window rate limiting, PII sanitization, circuit breaker, audit triggers, and **Diagram 3 (Security Pipeline)**. |
| **[`TESTING.md`](TESTING.md)** | Test inventory (580 unique tests: 579 passed, 1 skipped), unit/integration commands, coverage, and mocking strategy. |
| **[`PERFORMANCE.md`](PERFORMANCE.md)** | Benchmark harness methodology, empirical Stages 0–2 results, queue dynamics, latency breakdowns, and SRS target compliance. |
| **[`FINAL_AUDIT.md`](FINAL_AUDIT.md)** | Final production-style code review, SRS compliance traceability matrix, and security audit log. |

---

## 2. System Architecture & Invariants

```
                                  ┌──────────────────────────────────────────────┐
                                  │               SIGS-RS Gateway                │
┌─────────────────────────────┐   │                                              │   ┌──────────────────────────┐
│ Virtual Sensor Grid         │───┼─► Ingestion Layer (MQTT 8883 / HTTP REST)    │   │ PostgreSQL + TimescaleDB │
│ (Water, Energy, Air Quality)│   │       │                                      │   │                          │
└─────────────────────────────┘   │       ▼ asyncio.Queue                        │   │ telemetry.sanitized_readings
                                  │   Anomalous Packet Inspector                 │   │ (Hypertable partitioned) │
                                  │       │ (Sliding-window + Impossible-values) │   │                          │
                                  │       ▼                                      │   │ audit.security_log       │
                                  │   Privacy Anonymizer                         │───┼─► (Immutable WORM trig.) │
                                  │       │ (PII stripping: geo, MAC, device)    │   │                          │
                                  │       ▼                                      │   │ config.middleware_settings
                                  │   Circuit Breaker ──► Repository Pool        │   └──────────────────────────┘
                                  └──────────────────────────────────────────────┘
```

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full Mermaid system architecture and telemetry sequence diagrams.

### Core Processing Invariant: Inspector → Anonymizer → Persistence
The gateway enforces a non-negotiable architectural invariant:
1. **Raw Telemetry Never Persists:** Telemetry cannot bypass middleware inspection or anonymization.
2. **Sequential Stage Ordering:** Ingress packets are validated by the **Anomalous Packet Inspector** first. Packets violating rate limits (rolling 50 packets / 10 s per sensor) or physical boundary values (negative flows/energy, out-of-range AQI) are **dropped immediately and never reach the Privacy Anonymizer**.
3. **Fail-Secure Anonymization:** Verified packets have all geolocation (`latitude`, `longitude`, `geo`), hardware MAC addresses (`mac_address`), and hardware identifiers (`device_id`) stripped before database insertion.
4. **Resilience & Fail-Closed Guard:** A database write latency circuit breaker (> 2,000 ms) and BR-3 compliance guideline locks suspend incoming telemetry rather than permitting unverified persistence.

---

## 3. Technology Stack

- **Application Framework:** Python 3.12, FastAPI, Uvicorn (ASGI), Pydantic v2.
- **Asynchronous Ingestion:** `aiomqtt` (MQTT 3.1.1 / 5.0 client), `asyncio.Queue` in-memory buffering.
- **Time-Series Persistence:** PostgreSQL 16 with TimescaleDB extension (`telemetry.sanitized_readings` hypertable partitioned by timestamp `ts`).
- **Distributed Cache / Rate-Limiting:** Redis 7.2 (optional distributed sliding-window rate-limiting).
- **Security & Cryptography:** PyJWT (HMAC-SHA256 tokens), Passlib with Bcrypt, Python `ssl` (TLS 1.2+ & mTLS).
- **Observability:** Prometheus client exposition format (`/metrics`), structured JSON logging.
- **Containerization & Orchestration:** Docker multi-stage builds (`docker/Dockerfile`), Docker Compose (`docker-compose.yml`), Kubernetes 1.28+ manifests (`k8s/`).

---

## 4. Quickstart: Running Locally

### 4.1 Prerequisites
- Python 3.12+ (tested with Python 3.12.3)
- `pip` and Python `venv`
- OpenSSL (optional, for token generation)

### 4.2 Clone & Environment Setup

```bash
# Clone the repository
git clone <repo-url>
cd sigs-rs

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies in editable mode
pip install -e ".[dev]"

# Configure environment variables
cp .env.example .env
```

### 4.3 Starting the Gateway

```bash
# Run standalone gateway (in development mode, uses InMemoryDatabasePool if DB is offline)
uvicorn sigs_rs.main:app --host 0.0.0.0 --port 8000 --reload
```

Verify service status:
- **Liveness Probe:** `curl http://localhost:8000/health`
- **Readiness Probe:** `curl http://localhost:8000/ready`
- **Prometheus Metrics:** `curl http://localhost:8000/metrics`
- **Swagger Documentation:** Open `http://localhost:8000/docs` in your browser.

---

## 5. MQTT Ingestion vs. External Broker

The gateway includes an **MQTT Subscriber** (`src/sigs_rs/ingestion/mqtt_sub.py`), which connects to an **external MQTT Broker** (such as Eclipse Mosquitto or EMQX).

- **Plaintext / Local Dev Broker:** Port `1883`
- **Production TLS / MQTTS Broker:** Port `8883` (enforced when `MQTT_USE_TLS=true`)

To run an external Mosquitto broker locally using Docker:
```bash
docker run -d --name mosquitto -p 1883:1883 -p 8883:8883 eclipse-mosquitto:2.0
```

---

## 6. Virtual Sensor Grid Simulator & Attack Scenarios

The simulator CLI (`simulator/cli.py`) generates synthetic IoT sensor traffic and allows operators to inject malicious traffic patterns to test the gateway's security controls.

### 6.1 Basic Simulator Usage

```bash
# Dry-run telemetry generation (10 sensors, 30s duration, no MQTT broker required)
python -m simulator --profile smoke --dry-run

# Run standard background telemetry publishing to local MQTT broker
python -m simulator --mode NORMAL --sensors 25 --rate 5 --duration 60 --broker-host localhost --broker-port 1883
```

Available preconfigured profiles (`--profile`): `smoke`, `normal`, `stress`, `benchmark`, `dos`.

### 6.2 Triggering Attack & Anomaly Traffic Modes

Operators can trigger security drops using dedicated traffic modes:

```bash
# 1. Trigger Rate-Limit Violation (burst exceeding 50 pkts / 10s per sensor)
python -m simulator --mode RATE_ATTACK --sensors 10 --rate 100 --duration 10 --dry-run

# 2. Trigger Impossible-Value Rejection (negative water/energy, impossible AQI)
python -m simulator --mode INVALID_METRICS --sensors 20 --duration 15 --dry-run

# 3. Trigger Malformed Payload Rejection (corrupted JSON, invalid fields)
python -m simulator --mode MALFORMED_PAYLOAD --sensors 20 --duration 15 --dry-run

# 4. Trigger High-Volume Traffic Bursts
python -m simulator --mode BURST --sensors 50 --rate 200 --duration 20 --dry-run

# 5. Inject a specific percentage of anomalies into normal traffic
python -m simulator --mode MIXED --anomaly-percent 15.0 --malformed-percent 5.0 --duration 30 --dry-run
```

---

## 7. Observing Drops, Metrics, and Persisted Records

### 7.1 Observing Dropped Packets via Metrics
View live packet rejection and anomaly counters exposed at `/metrics`:
```bash
curl -s http://localhost:8000/metrics | grep sigs_packets_
```
Key counters:
- `sigs_packets_dropped_rate_total`: Packets dropped by sliding-window rate limiting.
- `sigs_packets_dropped_invalid_metrics_total`: Packets dropped by impossible-value validator.
- `sigs_packets_malformed_total`: Packets rejected due to JSON/schema errors or oversized payloads (> 512 KB).
- `sigs_packets_dropped_anonymization_total`: Packets rejected during PII stripping fail-secure check.

### 7.2 Observing Security Audit Logs
Query the immutable security audit log via the management API (requires JWT Bearer token):

```bash
# 1. Obtain an access token
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/token \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "changeme"}' | jq -r .access_token)

# 2. Query audit logs for dropped packets and security events
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/audit/logs?event_type=RATE_LIMIT_EXCEEDED" | jq .
```

### 7.3 Inspecting Sanitized Database Records
When connected to TimescaleDB, verify that all geolocation and hardware MAC addresses were stripped:

```bash
# Query sanitized readings
docker exec sigs-timescaledb psql -U sigs_user -d sigs_db -c \
  "SELECT id, ts, sensor_id, resource_type, value, unit, metadata FROM telemetry.sanitized_readings LIMIT 5;"

# Verify zero PII in metadata (should return 0 rows)
docker exec sigs-timescaledb psql -U sigs_user -d sigs_db -c \
  "SELECT COUNT(*) FROM telemetry.sanitized_readings WHERE metadata ? 'latitude' OR metadata ? 'mac_address' OR metadata ? 'geo';"
```

---

## 8. Verification & Performance Benchmarking

### 8.1 Running Automated Tests
The repository contains 580 unique collected tests (579 passing, 1 skipped):

```bash
# Run the complete test suite
pytest -q

# Run unit tests only (463 tests)
pytest tests/unit/ -v

# Run integration tests only (96 tests)
pytest tests/integration/ -v

# Run Phase 16 audit hardening regressions (32 tests)
pytest tests/unit/test_phase16_audit_hardening.py -v
```
See [`TESTING.md`](TESTING.md) for module-level commands and skip rationale.

### 8.2 Running Performance Benchmarks
Run the progressive workload benchmarking suite:

```bash
# Execute safe baseline Stages 0, 1, and 2 (10, 100, and 1,000 sensors)
python -m benchmarks.cli --stages 0,1,2 --suites all --output docs/phase15/benchmark-report.md
```
See [`PERFORMANCE.md`](PERFORMANCE.md) for full latency distributions, queue metrics, and bottleneck attribution.

---

## 9. Containerized Deployment (Docker & Kubernetes)

### 9.1 Docker Compose Full Stack
Start the complete 5-container stack (TimescaleDB, Mosquitto, Gateway, Simulator, Prometheus):

```bash
cp .env.example .env
docker compose up -d --build
```
Teardown: `docker compose down -v`.

### 9.2 Kubernetes Deployment
Apply the manifests located in [`k8s/`](k8s/):

```bash
# 1. Create namespace
kubectl apply -f k8s/namespace.yaml

# 2. Create production secrets
kubectl create secret generic sigs-rs-secret -n sigs-rs \
  --from-literal=DATABASE_URL="postgresql+asyncpg://sigs_user:SecretPass@timescaledb:5432/sigs_db" \
  --from-literal=POSTGRES_PASSWORD="SecretPass" \
  --from-literal=JWT_SECRET_KEY="$(openssl rand -hex 64)" \
  --from-literal=ADMIN_USERNAME="admin" \
  --from-literal=ADMIN_PASSWORD="$(openssl rand -hex 16)"

# 3. Apply infrastructure and gateway manifests
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/redis.yaml
kubectl apply -f k8s/timescaledb.yaml
kubectl apply -f k8s/mosquitto.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/hpa.yaml
kubectl apply -f k8s/pdb.yaml
```
See [`DEPLOYMENT.md`](DEPLOYMENT.md) for scaling, monitoring, and teardown runbooks.

---

## 10. Known Limitations & Verification Boundaries

To maintain absolute technical integrity, the following limitations are explicitly documented:

- **REQ-PF-1 (50,000 pkts/s Across 10,000 Sensors):** Marked **NOT VERIFIED AT PRODUCTION SCALE**. Local single-core microbenchmarks verified ~150,000 pkts/s for Inspector alone and up to 3,820 pkts/s persisted end-to-end, but 50k pkts/s across 10,000 sensors requires a distributed Kubernetes cluster meeting the SRS hardware specification (16 vCPUs, 32 GB RAM).
- **REQ-TI-1 / REQ-SE-1 (MQTTS Port 8883):** Local TLS context building and port validation are verified. Live broker handshake in production requires connecting to managed staging broker infrastructure.
- **REQ-SE-4 (Immutable Audit Storage):** Immutability is enforced via PostgreSQL trigger `trg_audit_immutable`, which blocks `UPDATE` and `DELETE` operations. This is database-level enforcement, not hardware WORM storage.
- **SRS BR-3 (Compliance Lockout Immediacy):** Lockout enforcement is immediate on the local instance. Across a distributed cluster, synchronization occurs via database polling with up to a 1.0-second convergence window.
