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

### 4. Run the full stack with Docker Compose

```bash
docker-compose up --build
```

This starts:
- **sigs-rs** on port 8000
- **Mosquitto** MQTT broker on port 1883 (TLS: 8883)
- **TimescaleDB** on port 5432

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
| 2 | ⏳ Next | Database layer, TimescaleDB schema, migrations |
| 3 | Pending | Full Inspector: sliding-window + impossible-value algorithms |
| 4 | Pending | MQTT subscriber, pipeline wiring |
| 5 | Pending | Circuit breaker implementation, fail-closed |
| 6 | Pending | Full auth: user store, OAuth 2.0 |
| 7 | Pending | Config API persistence, audit log queries |
| 8 | Pending | Docker finalization, CI |
| 9 | Pending | Load simulator full implementation |
| 10 | Pending | Kubernetes manifests + HPA |
| 11 | Pending | Documentation, final E2E validation |

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
| REQ-QA-4: Independently testable middleware | ✅ Phase 1 |

---

## License

MIT — see [SRS references](docs/SRS.pdf) for compliance documentation.
