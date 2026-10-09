# SIGS-RS — Secure IoT Gateway Simulation for Resource Sensing

> Version 1.0.0 | Python 3.12 | FastAPI | PostgreSQL/TimescaleDB

A backend simulation engine that models a community sensor grid and processes high-volume
telemetry data through a two-stage security middleware:

1. **Anomalous Packet Inspector** — sliding-window rate limiting + impossible-value detection
2. **Privacy Anonymizer** — PII stripping (latitude, longitude, MAC address) before DB persistence

---

## Architecture Overview

```
Virtual Sensor Grid (MQTT / HTTP)
        │
        ▼
┌─────────────────────────────────────┐
│  SIGS-RS Gateway                    │
│                                     │
│  [Ingestion Layer]                  │
│    MQTT Subscriber (port 8883)      │
│    HTTP POST /api/v1/telemetry      │
│         │                           │
│         ▼ asyncio.Queue             │
│  [Anomalous Packet Inspector]       │
│    Sliding-window (50 pkt/10s)     │
│    Impossible-value checks          │
│         │ verified only             │
│  [Privacy Anonymizer]               │
│    Strip: latitude, longitude,      │
│           mac_address, geo          │
│         │ sanitized                 │
│  [Circuit Breaker] → [DB Pool]      │
└─────────────────────────────────────┘
        │
        ▼
PostgreSQL + TimescaleDB
```

---

## Quick Start (Local Development)

### Prerequisites

- Python 3.12+
- Docker & Docker Compose (for full stack)

### 1. Clone and install

```bash
git clone <repo-url>
cd sigs-rs

# Create virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/macOS

# Install with dev extras
pip install -e ".[dev]"
```

### 2. Configure environment

```bash
copy .env.example .env
# Edit .env as needed (defaults work for local dev)
```

### 3. Run the application (dev mode, no DB required)

```bash
uvicorn sigs_rs.main:app --reload --host 0.0.0.0 --port 8000
```

Visit: http://localhost:8000/docs (OpenAPI UI)

### 4. Run the full stack with Docker Compose (Phase 12)

A fresh clone can start the complete production-like stack with two commands:

```bash
cp .env.example .env
docker compose up -d --build
```

#### Services Started

| Service | Container Name | Port(s) | Health Check | Description |
|---|---|---|---|---|
| **gateway** | `sigs-gateway` | `8000:8000` | HTTP `GET /health` | FastAPI ingestion gateway, API docs, and metrics |
| **timescaledb** | `sigs-timescaledb` | `5432:5432` | `pg_isready` | PostgreSQL 16 + TimescaleDB with hypertable partitioning |
| **mosquitto** | `sigs-mosquitto` | `1883:1883`, `8883:8883` | MQTT ping (`mosquitto_pub`) | Eclipse Mosquitto broker (plain & TLS) |
| **simulator** | `sigs-simulator` | internal | Depends on gateway & broker | Virtual IoT sensor grid generating live telemetry |
| **prometheus** | `sigs-prometheus` | `9090:9090` | HTTP `GET /-/healthy` | Prometheus server scraping gateway runtime `/metrics` |

#### Verify System Operation

```bash
# 1. Check all container statuses and health states
docker compose ps

# 2. Verify gateway health and component readiness
curl http://localhost:8000/health
curl http://localhost:8000/ready

# 3. View live Prometheus metrics scraped from the gateway
curl http://localhost:8000/metrics

# 4. View persisted telemetry in TimescaleDB
docker exec sigs-timescaledb psql -U sigs_user -d sigs_db -c \
  "SELECT COUNT(*), resource_type FROM telemetry.sanitized_readings GROUP BY resource_type;"

# 5. Open monitoring dashboards in browser:
#    - Gateway Swagger Docs: http://localhost:8000/docs
#    - Prometheus Dashboard: http://localhost:9090
```

#### Development Mode (Live Code Reloading)

To run with live host code volume mounting (`./src` and `./simulator`) and uvicorn auto-reload:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up
```

#### Running Custom Simulator Traffic Profiles

You can run targeted simulation workloads on-demand using the containerized simulator:

```bash
# Run a rate-attack simulation against the running gateway
docker compose run --rm simulator python -m simulator --mode RATE_ATTACK --rate 50 --duration 10

# Run a high-volume stress profile
docker compose run --rm simulator python -m simulator --profile stress --duration 30
```

#### Stop and Teardown

```bash
# Stop all services
docker compose down

# Stop all services and wipe persistent database & broker volumes
docker compose down -v
```
---

## API Reference

The full interactive API docs are available at `/docs` (Swagger UI) or `/redoc`.

### Health & Metrics

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/health` | GET | None | Liveness probe (K8s) |
| `/ready` | GET | None | Readiness probe |
| `/metrics` | GET | None | Runtime metrics |

### Authentication

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/api/v1/auth/token` | POST | None | Issue JWT token |

**Example:**
```bash
curl -X POST http://localhost:8000/api/v1/auth/token \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "changeme"}'
```

### Telemetry Ingestion

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/api/v1/telemetry` | POST | JWT | Ingest single packet |
| `/api/v1/telemetry/batch` | POST | JWT | Ingest batch |

**Example payload:**
```json
{
  "sensor_id": "sensor-042",
  "timestamp": "2026-07-17T10:30:00Z",
  "resource_type": "water",
  "value": 42.7,
  "unit": "liters_per_min",
  "geo": { "latitude": 12.9716, "longitude": 80.2209 },
  "mac_address": "AA:BB:CC:DD:EE:FF"
}
```

### Configuration (Security Engineer only)

| Endpoint | Method | Auth | Description |
|---|---|---|---|
| `/api/v1/config/inspector` | GET/PUT | JWT (security_engineer) | Sliding-window thresholds |
| `/api/v1/config/anonymizer` | GET/PUT | JWT (security_engineer) | PII field list |
| `/api/v1/audit/logs` | GET | JWT (security_engineer) | Query audit log |

---

## Running Tests

```bash
# All tests
pytest

# Unit tests only (no Docker required)
pytest tests/unit/ -v

# With coverage
pytest --cov=sigs_rs --cov-report=html

# Specific test file
pytest tests/unit/test_anonymizer.py -v
```

---

## Load Simulator

```bash
# Run smoke profile (10 sensors, 30s) — Phase 9 full implementation
python -m loadtest.sensor_simulator --profile smoke

# Available profiles: smoke, normal, stress, benchmark, dos
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://sigs_user:sigs_pass@localhost:5432/sigs_db` | TimescaleDB connection string |
| `MQTT_BROKER_HOST` | `localhost` | MQTT broker hostname |
| `MQTT_BROKER_PORT` | `8883` | MQTT port (TLS) |
| `MQTT_USE_TLS` | `false` | Enable TLS for MQTT |
| `INSPECTOR_WINDOW_SECONDS` | `10` | Sliding window duration |
| `INSPECTOR_MAX_PACKETS_PER_WINDOW` | `50` | Rate limit per sensor |
| `ANONYMIZER_PII_FIELDS` | `latitude,longitude,lat,lon,mac_address,geo` | Fields to strip |
| `JWT_SECRET_KEY` | *(must set)* | JWT signing secret |
| `ADMIN_USERNAME` | `admin` | Seeded admin username |
| `ADMIN_PASSWORD` | `changeme` | Seeded admin password |
| `CIRCUIT_BREAKER_FAILURE_THRESHOLD_MS` | `2000` | DB latency trip threshold |
| `MAX_PAYLOAD_SIZE_BYTES` | `524288` | Max packet size (512 KB) |

See `.env.example` for the full list.

---

## Project Structure

```
sigs-rs/
├── src/sigs_rs/
│   ├── main.py              # Application entrypoint
│   ├── config.py            # Pydantic Settings
│   ├── schemas.py           # Pydantic models
│   ├── common/
│   │   ├── constants.py     # SRS-derived constants
│   │   └── exceptions.py    # Exception hierarchy
│   ├── ingestion/
│   │   ├── buffer.py        # asyncio.Queue wrapper
│   │   ├── http_ingest.py   # FastAPI telemetry endpoints
│   │   └── mqtt_sub.py      # MQTT subscriber (Phase 4)
│   ├── middleware/
│   │   ├── sliding_window.py   # Sliding-window algorithm (Phase 3)
│   │   ├── payload_validator.py # Impossible-value checks (Phase 3)
│   │   ├── inspector.py        # Anomalous Packet Inspector (Phase 3)
│   │   ├── anonymizer.py       # Privacy Anonymizer ✓ implemented
│   │   └── pipeline.py         # Orchestrator (Phase 4)
│   ├── persistence/
│   │   ├── database.py      # asyncpg pool (Phase 2)
│   │   ├── repository.py    # INSERT operations (Phase 2)
│   │   └── circuit_breaker.py # Circuit breaker (Phase 5)
│   ├── auth/
│   │   ├── jwt_handler.py   # JWT utils ✓ implemented
│   │   ├── roles.py         # Role checks ✓ implemented
│   │   ├── dependencies.py  # FastAPI Depends() guards ✓
│   │   └── auth_router.py   # /auth/token endpoint ✓
│   └── management/
│       ├── audit_logger.py  # Dual audit logging ✓ implemented
│       ├── audit_api.py     # /audit/logs (Phase 7)
│       ├── config_api.py    # /config/* endpoints ✓ stub
│       └── health.py        # /health /metrics ✓ implemented
├── tests/
│   ├── conftest.py          # Shared fixtures
│   ├── unit/                # No I/O dependencies
│   └── integration/         # FastAPI test client
├── loadtest/
│   └── sensor_simulator.py  # Load profiles (Phase 9)
├── docker/
│   ├── Dockerfile           # Multi-stage build
│   ├── Dockerfile.mosquitto
│   └── mosquitto.conf
├── docker-compose.yml
├── pyproject.toml
└── .env.example
```

---

## Development Phases

| Phase | Status | Description |
|---|---|---|
| **1** | ✅ Complete | Project skeleton, schemas, anonymizer, auth, health |
| **2** | ✅ Complete | Database layer, TimescaleDB schema, migrations |
| **3** | ✅ Complete | Full Inspector: sliding-window + impossible-value algorithms |
| **4** | ✅ Complete | MQTT subscriber, pipeline wiring |
| **5** | ✅ Complete | Circuit breaker implementation, fail-closed |
| **6** | ✅ Complete | Full auth: user store, OAuth 2.0 |
| **7** | ✅ Complete | Config API persistence, audit log queries |
| **8** | ✅ Complete | Docker finalization, CI |
| **9** | ✅ Complete | Load simulator full implementation |
| **10** | ✅ Complete | Observability, Prometheus metrics, structured logging |
| **11** | ✅ Complete | Safety, circuit breaker recovery & fail-closed behavior |
| **12** | ✅ Complete | Complete local Docker environment (Docker Compose, TimescaleDB, Mosquitto, Simulator, Prometheus) |
| **13** | ✅ Complete | Kubernetes deployment manifests, HPA, probes, PDB |

---

## Phase 13 — Kubernetes Deployment

Manifests live in [`k8s/`](k8s/).

| File | Contents |
|---|---|
| [`namespace.yaml`](k8s/namespace.yaml) | `sigs-rs` namespace |
| [`configmap.yaml`](k8s/configmap.yaml) | All non-secret configuration |
| [`secret-template.yaml`](k8s/secret-template.yaml) | Secret template — **fill before applying** |
| [`deployment.yaml`](k8s/deployment.yaml) | Gateway Deployment with readiness/liveness/startup probes |
| [`service.yaml`](k8s/service.yaml) | ClusterIP Service for the gateway |
| [`hpa.yaml`](k8s/hpa.yaml) | HorizontalPodAutoscaler (75% CPU, ~2 min stabilization) |
| [`timescaledb.yaml`](k8s/timescaledb.yaml) | Single-node TimescaleDB StatefulSet + PVC |
| [`mosquitto.yaml`](k8s/mosquitto.yaml) | Mosquitto Deployment, ConfigMap, and Service |
| [`pdb.yaml`](k8s/pdb.yaml) | PodDisruptionBudget (minAvailable: 1) |
| [`kustomization.yaml`](k8s/kustomization.yaml) | Kustomize entry point |



### Quickstart — minikube

```bash
# 1. Install minikube
#    https://minikube.sigs.k8s.io/docs/start/

# 2. Start a cluster
minikube start --cpus=4 --memory=6144

# 3. Enable required add-ons
minikube addons enable metrics-server   # required by HPA
minikube addons enable ingress          # optional: expose gateway externally

# 4. Build the gateway image inside minikube's Docker daemon
eval $(minikube docker-env)
docker build -f docker/Dockerfile -t sigs-rs-gateway:1.0.0 .

# 5. Create the namespace
kubectl apply -f k8s/namespace.yaml

# 6. Create real secrets (NEVER commit real values to git)
kubectl create secret generic sigs-rs-secret \
  --namespace=sigs-rs \
  --from-literal=DATABASE_URL="postgresql+asyncpg://sigs_user:YOURPASSWORD@timescaledb:5432/sigs_db" \
  --from-literal=POSTGRES_PASSWORD="YOURPASSWORD" \
  --from-literal=JWT_SECRET_KEY="$(openssl rand -hex 64)" \
  --from-literal=ADMIN_USERNAME="admin" \
  --from-literal=ADMIN_PASSWORD="$(openssl rand -hex 16)"

# 7. Apply remaining manifests (skip secret-template.yaml — real secret already applied)
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/timescaledb.yaml
kubectl apply -f k8s/mosquitto.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/hpa.yaml
kubectl apply -f k8s/pdb.yaml

# 8. Wait for all pods to be ready
kubectl rollout status deployment/sigs-rs-gateway -n sigs-rs
kubectl get pods -n sigs-rs

# 9. Access the gateway
minikube service sigs-rs-gateway -n sigs-rs --url
# Or port-forward:
kubectl port-forward svc/sigs-rs-gateway 8080:80 -n sigs-rs &
curl http://localhost:8080/health
curl http://localhost:8080/metrics
```

### Quickstart — kind

```bash
# 1. Create a cluster
kind create cluster --name sigs-rs

# 2. Load the gateway image into kind
docker build -f docker/Dockerfile -t sigs-rs-gateway:1.0.0 .
kind load docker-image sigs-rs-gateway:1.0.0 --name sigs-rs

# 3. Follow steps 5–9 from the minikube instructions above.

# 4. Port-forward (kind has no minikube tunnel):
kubectl port-forward svc/sigs-rs-gateway 8080:80 -n sigs-rs &
curl http://localhost:8080/health
```

### HPA Verification

```bash
# Watch HPA decisions in real time
kubectl get hpa sigs-rs-gateway-hpa -n sigs-rs -w

# Generate CPU load to trigger scaling (separate terminal)
kubectl run -it --rm load --image=busybox --restart=Never -n sigs-rs -- \
  sh -c "while true; do wget -qO- http://sigs-rs-gateway/health; done"

# Note: stabilizationWindowSeconds: 120 for scaleUp evaluates the highest recommendation
# over the past 2 minutes to prevent rapid fluctuation, but Kubernetes HPA does NOT strictly
# guarantee load remains above 75% for 120s continuously before it scales up.
# Scale down waits 300s to avoid thrashing.
```

### Observability

```bash
# Health (liveness / fail-closed state)
curl http://localhost:8080/health

# Readiness
curl http://localhost:8080/ready

# Prometheus text exposition format
curl http://localhost:8080/metrics

# JSON metrics
curl -H "Accept: application/json" http://localhost:8080/metrics

# Describe HPA events and decisions
kubectl describe hpa sigs-rs-gateway-hpa -n sigs-rs

# Structured logs from all gateway pods
kubectl logs -l app.kubernetes.io/component=gateway -n sigs-rs --follow
```

### Production Considerations

| Concern | Current (local/demo) | Production recommendation |
|---|---|---|
| **Sliding-window state** | ✅ Redis-backed shared `SlidingWindow` | Redis cluster with persistence and eviction |
| **MQTT duplicate delivery** | ✅ MQTT shared subscriptions (`$share/sigs-rs/telemetry/#`) | EMQX cluster or AWS IoT Core |
| **Circuit breaker state** | Per-pod, independent (Intentional) | Shared state is generally an anti-pattern. If one pod loses DB connectivity, only that pod should trip. If the DB is globally down, all pods will independently trip. |
| **TimescaleDB** | Single-node StatefulSet | Managed cloud DB or TimescaleDB Operator with replication |
| **Mosquitto** | Single-pod, no clustering | EMQX cluster / managed MQTT (AWS IoT Core, HiveMQ Cloud) |
| **TLS** | Disabled (plaintext 1883) | mTLS on port 8883 (SRS REQ-TI-1, REQ-SE-1) |
| **Secrets** | `kubectl create secret` | HashiCorp Vault, AWS Secrets Manager, Sealed Secrets, SOPS |
| **Image** | `IfNotPresent` / local build | Pinned digest + private registry + image scanning |
| **Ingress** | Port-forward | NGINX/Traefik Ingress + TLS cert-manager |
| **Monitoring** | `/metrics` text scrape | Prometheus Operator + Grafana dashboards + alerting |

---

## SRS Compliance

| Requirement | Status |
|---|---|
| REQ-TI-1: MQTT on port 8883 with TLS | Phase 4 |
| REQ-TI-2: JSON schema validation | ✅ Pydantic in Phase 1 |
| REQ-TI-3: 512 KB payload limit | ✅ Enforced in http_ingest.py |
| REQ-TI-4: In-memory ingestion queue | ✅ IngestionQueue Phase 1 |
| REQ-API-1/2: Sliding-window algorithm | Phase 3 |
| REQ-API-3: Impossible-value detection | Phase 3 |
| REQ-PA-1/4: PII stripping + fail-secure | ✅ anonymizer.py Phase 1 |
| REQ-SE-2: JWT authentication | ✅ Phase 1 |
| REQ-SE-4: Audit logging | ✅ AuditLogger Phase 1 |
| REQ-SF-1: Production DB guard | ✅ config.py Phase 1 |
| REQ-SF-2: Circuit breaker | Phase 5 |
| REQ-QA-1: K8s liveness restart < 30 s | ✅ Phase 13 (5 s period × 3 = 15 s) |
| REQ-QA-3: Kubernetes readiness | ✅ Phase 13 |
| REQ-QA-4: Independently testable middleware | ✅ Phase 1 |

---

## License

MIT — see [SRS references](docs/SRS.pdf) for compliance documentation.
