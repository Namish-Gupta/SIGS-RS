# SIGS-RS Deployment & Operations Guide

## 1. Overview

This document provides instructions for provisioning, configuring, and operating the **SIGS-RS Gateway** across local development, containerized Docker Compose stacks, and scalable Kubernetes environments.

> [!IMPORTANT]
> **Host Environment Constraints & Verification Disclosure:**  
> In accordance with Phase 16/17 host constraints, no external daemon services (Docker, Kubernetes, PostgreSQL, Redis, or Mosquitto) were launched during the active verification phase. Local development commands using Python 3.12 and `.venv` have been verified directly against repository entry points. Docker and Kubernetes instructions represent static configurations verified against `docker-compose.yml`, `docker/Dockerfile`, and `k8s/` manifests.

---

## 2. Diagram 4: Deployment Architecture

The following diagram illustrates the Kubernetes production-style deployment architecture defined in [`k8s/`](file:///home/namish/projects/SIGS-RS/k8s).

```mermaid
flowchart TD
    subgraph ClientAccess ["Client Ingress & Observability"]
        Sensors["External IoT Sensor Fleet"]
        Operator["Security Engineers & QA"]
        Prometheus["Prometheus Monitoring"]
    end

    subgraph K8sNamespace ["Kubernetes Namespace: sigs-rs"]
        subgraph IngressRouting ["Ingress & Services"]
            GatewaySvc["ClusterIP Service: sigs-rs-gateway<br/>Port 80 -> 8000"]
            MosquittoSvc["ClusterIP Service: mosquitto<br/>Ports 1883 / 8883"]
            RedisSvc["ClusterIP Service: redis<br/>Port 6379"]
            TimescaleSvc["ClusterIP Service: timescaledb<br/>Port 5432"]
        end

        subgraph GatewayWorkload ["Stateless Gateway Tier"]
            HPA["Horizontal Pod Autoscaler<br/>(Min: 2, Max: 10, Target: 75% CPU)"]
            PDB["PodDisruptionBudget<br/>(minAvailable: 1)"]
            Pod1["Gateway Pod 1<br/>• FastAPI Uvicorn<br/>• IngestionQueue<br/>• Pipeline Worker"]
            Pod2["Gateway Pod 2<br/>• FastAPI Uvicorn<br/>• IngestionQueue<br/>• Pipeline Worker"]
        end

        subgraph StatefulTier ["Stateful & Storage Tier"]
            TimescalePod["TimescaleDB StatefulSet<br/>PostgreSQL 16 + TimescaleDB<br/>Mounted PVC: 10Gi"]
            MosquittoPod["Mosquitto Deployment<br/>Eclipse Mosquitto 2.0"]
            RedisPod["Redis Deployment<br/>Redis 7.2 Alpine"]
        end

        subgraph ConfigAndSecrets ["Config & Secret Management"]
            ConfigMap["ConfigMap: sigs-rs-config<br/>Thresholds, intervals, SSL mode"]
            Secret["Secret: sigs-rs-secret<br/>DB credentials, JWT signing key"]
        end
    end

    Sensors -->|MQTTS Port 8883| MosquittoSvc
    Sensors -->|HTTP Ingest Port 80| GatewaySvc
    Operator -->|Management API Port 80| GatewaySvc
    Prometheus -->|Scrape /metrics Port 80| GatewaySvc

    GatewaySvc --> Pod1
    GatewaySvc --> Pod2
    HPA -.->|Scales Replicas| GatewayWorkload

    Pod1 --> MosquittoSvc
    Pod2 --> MosquittoSvc
    MosquittoSvc --> MosquittoPod

    Pod1 --> RedisSvc
    Pod2 --> RedisSvc
    RedisSvc --> RedisPod

    Pod1 --> TimescaleSvc
    Pod2 --> TimescaleSvc
    TimescaleSvc --> TimescalePod

    ConfigMap -.-> Pod1
    ConfigMap -.-> Pod2
    Secret -.-> Pod1
    Secret -.-> Pod2
```

---

## 3. Local Development Setup (Virtual Environment)

### 3.1 Prerequisites
- Python 3.12+ (tested with Python 3.12.3)
- `pip` and `virtualenv`
- OpenSSL (for generating test JWT signing secrets)

### 3.2 Installation Steps

```bash
# 1. Clone repository and navigate to root
cd /home/namish/projects/SIGS-RS

# 2. Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies in editable mode with development extras
pip install -e ".[dev]"

# 4. Initialize local configuration
cp .env.example .env
```

### 3.3 Configuration Parameters (`.env`)

Key environment variables parsed by `src/sigs_rs/config.py`:

| Variable | Default Value | Description |
| :--- | :--- | :--- |
| `APP_ENV` | `development` | Operating environment (`development`, `test`, `production`). In `development`, fallback to `InMemoryDatabasePool` engages if DB is unavailable. |
| `APP_HOST` | `0.0.0.0` | Gateway bind address. |
| `APP_PORT` | `8000` | Gateway HTTP listen port. |
| `DATABASE_URL` | `postgresql+asyncpg://sigs_user:sigs_pass@localhost:5432/sigs_db` | Connection URI for PostgreSQL / TimescaleDB. |
| `DATABASE_SSL_MODE` | `prefer` | SSL negotiation (`disable`, `prefer`, `require`). In production, `require` is enforced. |
| `MQTT_BROKER_HOST` | `localhost` | MQTT broker hostname or IP. |
| `MQTT_BROKER_PORT` | `8883` | MQTT port (1883 plaintext, 8883 TLS). |
| `MQTT_USE_TLS` | `false` | Enable TLS 1.2+ for MQTT subscriber (SRS REQ-TI-1). |
| `INSPECTOR_WINDOW_SECONDS` | `10` | Sliding-window evaluation duration. |
| `INSPECTOR_MAX_PACKETS_PER_WINDOW` | `50` | Maximum packets permitted per sensor within window. |
| `JWT_SECRET_KEY` | *(insecure default)* | Cryptographic HMAC secret for signing access tokens. Must be overridden in production. |
| `ADMIN_USERNAME` | `admin` | Seeded administrative user identity. |
| `ADMIN_PASSWORD` | `changeme` | Seeded administrative password. |
| `CIRCUIT_BREAKER_FAILURE_THRESHOLD_MS` | `2000` | Database write latency trip threshold in milliseconds. |

### 3.4 Database Initialization (Optional Local PostgreSQL)

If a local PostgreSQL instance is running:
```bash
# Execute schema definition script
psql -h localhost -U sigs_user -d sigs_db -f src/sigs_rs/persistence/schema.sql
```
*Note: If no database is running, launching the gateway in `APP_ENV=development` automatically uses `InMemoryDatabasePool`.*

### 3.5 Running the Gateway Locally

```bash
# Launch Uvicorn ASGI server with live reloading
uvicorn sigs_rs.main:app --host 0.0.0.0 --port 8000 --reload
```

Verify service liveness:
```bash
curl http://localhost:8000/health
curl http://localhost:8000/metrics
```

### 3.6 Running the Virtual Sensor Simulator Locally

```bash
# Dry-run telemetry generation (no MQTT broker required)
python -m simulator --profile smoke --dry-run

# Run targeted rate-attack simulation against local MQTT broker (if running)
python -m simulator --mode RATE_ATTACK --rate 50 --duration 10 --broker-host localhost --broker-port 1883
```

---

## 4. Docker Compose Deployment (Multi-Container Stack)

The project includes a complete Docker Compose specification (`docker-compose.yml`) defining 5 isolated containers connected via bridge network `sigs-net`:

1. **`timescaledb`:** TimescaleDB 16 (`timescale/timescaledb:latest-pg16`) with persistent volume `pgdata` and automated initialization from `docker/init-db.sql`.
2. **`mosquitto`:** Eclipse Mosquitto MQTT broker built from `docker/Dockerfile.mosquitto`.
3. **`gateway`:** SIGS-RS FastAPI application built from `docker/Dockerfile`.
4. **`simulator`:** Containerized sensor grid simulating live MQTT traffic.
5. **`prometheus`:** Prometheus server scraping `gateway:8000/metrics`.

### 4.1 Launching the Stack

```bash
# Build images and start services in detached mode
docker compose up -d --build

# Verify container statuses and health probes
docker compose ps
```

### 4.2 Verifying Operations Under Docker

```bash
# Check gateway health
curl http://localhost:8000/health

# Inspect persisted telemetry count in TimescaleDB
docker exec sigs-timescaledb psql -U sigs_user -d sigs_db -c \
  "SELECT resource_type, COUNT(*) FROM telemetry.sanitized_readings GROUP BY resource_type;"

# Check Prometheus metrics
curl http://localhost:9090/-/healthy
```

### 4.3 Teardown

```bash
# Stop containers
docker compose down

# Stop containers and remove persistent database and volume storage
docker compose down -v
```

---

## 5. Kubernetes Deployment (`k8s/`)

Manifests in [`k8s/`](file:///home/namish/projects/SIGS-RS/k8s) provide a production-ready, highly available cluster deployment.

### 5.1 Manifest Inventory

| File | Resource Kind | Description |
| :--- | :--- | :--- |
| [`namespace.yaml`](file:///home/namish/projects/SIGS-RS/k8s/namespace.yaml) | `Namespace` | Defines isolated `sigs-rs` namespace. |
| [`configmap.yaml`](file:///home/namish/projects/SIGS-RS/k8s/configmap.yaml) | `ConfigMap` | Non-sensitive runtime settings and thresholds. |
| [`secret-template.yaml`](file:///home/namish/projects/SIGS-RS/k8s/secret-template.yaml) | `Secret` | Template for DB passwords and JWT secret keys. |
| [`redis.yaml`](file:///home/namish/projects/SIGS-RS/k8s/redis.yaml) | `Deployment`, `Service` | Shared Redis cache for distributed rate-limiting. |
| [`timescaledb.yaml`](file:///home/namish/projects/SIGS-RS/k8s/timescaledb.yaml) | `StatefulSet`, `Service` | Single-replica TimescaleDB with 10Gi PVC. |
| [`mosquitto.yaml`](file:///home/namish/projects/SIGS-RS/k8s/mosquitto.yaml) | `Deployment`, `Service` | Mosquitto MQTT broker with custom config. |
| [`deployment.yaml`](file:///home/namish/projects/SIGS-RS/k8s/deployment.yaml) | `Deployment` | 2-replica Gateway with liveness, readiness, and startup probes. |
| [`service.yaml`](file:///home/namish/projects/SIGS-RS/k8s/service.yaml) | `Service` | ClusterIP service exposing gateway on port 80. |
| [`hpa.yaml`](file:///home/namish/projects/SIGS-RS/k8s/hpa.yaml) | `HorizontalPodAutoscaler` | Scales gateway 2–10 pods at 75% target CPU. |
| [`pdb.yaml`](file:///home/namish/projects/SIGS-RS/k8s/pdb.yaml) | `PodDisruptionBudget` | Guarantees minimum 1 pod available during maintenance. |
| [`kustomization.yaml`](file:///home/namish/projects/SIGS-RS/k8s/kustomization.yaml) | `Kustomization` | Bundles all resources for declarative deployment. |

### 5.2 Step-by-Step Deployment Instructions

```bash
# 1. Create the dedicated namespace
kubectl apply -f k8s/namespace.yaml

# 2. Provision production secrets (NEVER commit secrets to source control)
kubectl create secret generic sigs-rs-secret \
  --namespace=sigs-rs \
  --from-literal=DATABASE_URL="postgresql+asyncpg://sigs_user:StrongPasswordHere@timescaledb:5432/sigs_db" \
  --from-literal=POSTGRES_PASSWORD="StrongPasswordHere" \
  --from-literal=JWT_SECRET_KEY="$(openssl rand -hex 64)" \
  --from-literal=ADMIN_USERNAME="admin" \
  --from-literal=ADMIN_PASSWORD="$(openssl rand -hex 16)"

# 3. Apply infrastructure and supporting services
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/redis.yaml
kubectl apply -f k8s/timescaledb.yaml
kubectl apply -f k8s/mosquitto.yaml

# 4. Apply gateway deployment, services, and autoscaling policies
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
kubectl apply -f k8s/hpa.yaml
kubectl apply -f k8s/pdb.yaml

# 5. Wait for deployment rollout to stabilize
kubectl rollout status deployment/sigs-rs-gateway -n sigs-rs
```

### 5.3 Verifying Cluster Operation

```bash
# Port-forward gateway service to localhost
kubectl port-forward svc/sigs-rs-gateway 8080:80 -n sigs-rs &

# Test liveness probe
curl http://localhost:8080/health

# Test metrics
curl http://localhost:8080/metrics

# Inspect pod logs
kubectl logs -l app.kubernetes.io/component=gateway -n sigs-rs --tail=50
```

### 5.4 Cluster Teardown

```bash
# Delete all resources in namespace
kubectl delete namespace sigs-rs
```
