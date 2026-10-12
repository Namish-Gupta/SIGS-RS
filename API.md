# SIGS-RS REST API Reference

## 1. Overview

The **SIGS-RS Gateway** exposes a RESTful HTTP interface powered by FastAPI. This interface provides telemetry ingestion endpoints, runtime health probes, Prometheus metrics exposition, role-based configuration endpoints, compliance management, and security audit querying.

- **Base URL (Local):** `http://localhost:8000`
- **Interactive OpenAPI Documentation:** `http://localhost:8000/docs` (Swagger UI)
- **ReDoc Specification:** `http://localhost:8000/redoc`
- **OpenAPI JSON Schema:** `http://localhost:8000/openapi.json`

---

## 2. Authentication & Authorization

All management, configuration, and audit endpoints require an HTTP Bearer JWT token in the `Authorization` header:

```http
Authorization: Bearer <jwt_token>
```

Tokens are issued via `POST /api/v1/auth/token` (SRS REQ-SE-2). Access is governed by Role-Based Access Control (RBAC):

| Role Identifier | Description | Permitted Endpoints |
| :--- | :--- | :--- |
| `security_engineer` | Lead security operator | Full administrative access: modify Inspector thresholds, update PII lists, revise compliance guidelines, audit signoff, read status & audit logs. |
| `compliance_auditor` | Independent compliance auditor | Revise compliance guidelines, perform audit signoff (SRS BR-3), read compliance status & audit logs. |
| `qa_tester` | Quality assurance engineer | Read-only access to Inspector/Anonymizer configs, middleware status, and audit logs. |
| `backend_developer` | Platform engineer | Read-only access to Inspector/Anonymizer configs, middleware status, and audit logs. |
| `devops` | Infrastructure operator | Read-only access to `/api/v1/config/status`. |

> [!NOTE]
> Public endpoints (`/health`, `/ready`, `/metrics`, `POST /api/v1/auth/token`, and telemetry ingestion `POST /api/v1/telemetry`) do not require Bearer tokens.

---

## 3. Route Summary Catalog

| HTTP Method | Route Path | Auth / Role Requirement | Category | Source Module |
| :--- | :--- | :--- | :--- | :--- |
| `GET` | `/health` | None (Public) | Health Probe | `src/sigs_rs/management/health.py` |
| `GET` | `/ready` | None (Public) | Readiness Probe | `src/sigs_rs/management/health.py` |
| `GET` | `/metrics` | None (Public) | Observability | `src/sigs_rs/management/health.py` |
| `POST` | `/api/v1/auth/token` | None (Credentials Body) | Authentication | `src/sigs_rs/auth/auth_router.py` |
| `POST` | `/api/v1/telemetry` | None (Payload Size Limited) | Telemetry Ingestion | `src/sigs_rs/ingestion/http_ingest.py` |
| `POST` | `/api/v1/telemetry/batch` | None (Payload Size Limited) | Telemetry Ingestion | `src/sigs_rs/ingestion/http_ingest.py` |
| `GET` | `/api/v1/config/inspector` | JWT (`security_engineer`, `compliance_auditor`, `qa_tester`, `backend_developer`) | Configuration | `src/sigs_rs/management/config_api.py` |
| `PUT` | `/api/v1/config/inspector` | JWT (`security_engineer`) | Configuration | `src/sigs_rs/management/config_api.py` |
| `GET` | `/api/v1/config/anonymizer` | JWT (`security_engineer`, `compliance_auditor`, `qa_tester`, `backend_developer`) | Configuration | `src/sigs_rs/management/config_api.py` |
| `PUT` | `/api/v1/config/anonymizer` | JWT (`security_engineer`) | Configuration | `src/sigs_rs/management/config_api.py` |
| `GET` | `/api/v1/config/status` | JWT (`security_engineer`, `compliance_auditor`, `qa_tester`, `backend_developer`, `devops`) | Configuration | `src/sigs_rs/management/config_api.py` |
| `GET` | `/api/v1/config/compliance/status` | JWT (`security_engineer`, `compliance_auditor`, `qa_tester`, `backend_developer`) | Compliance | `src/sigs_rs/management/config_api.py` |
| `PUT` | `/api/v1/config/compliance/guideline` | JWT (`security_engineer`, `compliance_auditor`) | Compliance | `src/sigs_rs/management/config_api.py` |
| `POST` | `/api/v1/config/compliance/audit-signoff`| JWT (`security_engineer`, `compliance_auditor`) | Compliance | `src/sigs_rs/management/config_api.py` |
| `GET` | `/api/v1/audit/logs` | JWT (`security_engineer`, `compliance_auditor`, `qa_tester`, `backend_developer`) | Audit Logs | `src/sigs_rs/management/audit_api.py` |

---

## 4. Health & Observability Endpoints

### 4.1 Liveness Probe: `GET /health`
- **Purpose:** Kubernetes liveness probe (SRS REQ-QA-1). Returns overall service health.
- **Auth:** None.
- **Responses:**
  - `200 OK`: All subsystems operational, or degraded (e.g., circuit breaker temporarily open).
  - `503 Service Unavailable`: System in `fail_closed` state (middleware crash or unhandled failure).
- **Example Response (`200 OK`):**
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

### 4.2 Readiness Probe: `GET /ready`
- **Purpose:** Kubernetes readiness probe (SRS REQ-QA-3). Confirms gateway is ready to receive traffic.
- **Auth:** None.
- **Responses:**
  - `200 OK`: Gateway ready for incoming telemetry.
  - `503 Service Unavailable`: Gateway unhealthy or failing closed.

### 4.3 Runtime Metrics: `GET /metrics`
- **Purpose:** Prometheus metrics scraping endpoint (SRS §3.3).
- **Auth:** None.
- **Content Negotiation:**
  - Request with `Accept: text/plain` or no accept header: returns standard Prometheus text exposition format (v0.0.4).
  - Request with `Accept: application/json` or query param `?format=json`: returns structured JSON metrics (`MetricsResponse`).
- **Example Response (Prometheus Format):**
  ```text
  # HELP sigs_packets_received_total Total telemetry packets received
  # TYPE sigs_packets_received_total counter
  sigs_packets_received_total 12450
  # HELP sigs_packets_persisted_total Total telemetry packets persisted
  # TYPE sigs_packets_persisted_total counter
  sigs_packets_persisted_total 12100
  # HELP sigs_queue_depth Current packets buffered in ingestion queue
  # TYPE sigs_queue_depth gauge
  sigs_queue_depth 0
  ```

---

## 5. Authentication Endpoint

### 5.1 Issue Access Token: `POST /api/v1/auth/token`
- **Purpose:** Authenticates enterprise credentials and issues signed JWT Bearer token (SRS REQ-SE-2).
- **Auth:** None.
- **Request Body (`TokenRequest`):**
  ```json
  {
    "username": "admin",
    "password": "changeme"
  }
  ```
- **Success Response (`200 OK`):**
  ```json
  {
    "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
    "token_type": "bearer",
    "expires_in": 3600
  }
  ```
- **Error Responses:**
  - `401 Unauthorized`: Invalid username or password.

---

## 6. Telemetry Ingestion Endpoints

### 6.1 Ingest Single Packet: `POST /api/v1/telemetry`
- **Purpose:** HTTP ingestion of a single IoT telemetry packet (SRS §4.1).
- **Auth:** None (enforces size limit and schema validation).
- **Constraints:** Payload size must not exceed 512 KB (SRS REQ-TI-3).
- **Request Body (`TelemetryPayload`):**
  ```json
  {
    "sensor_id": "sensor-042",
    "timestamp": "2026-10-11T12:00:00Z",
    "resource_type": "water",
    "value": 42.5,
    "unit": "liters_per_min",
    "geo": {
      "latitude": 12.9716,
      "longitude": 80.2209
    },
    "mac_address": "AA:BB:CC:DD:EE:FF",
    "water_ext": {
      "flow_rate": 42.5,
      "ph_level": 7.2
    }
  }
  ```
- **Success Response (`202 Accepted`):**
  ```json
  {
    "accepted": true,
    "message": "Packet accepted for processing."
  }
  ```
- **Error Responses:**
  - `413 Payload Too Large`: Payload exceeds 524,288 bytes.
  - `422 Unprocessable Content`: Schema validation failed (e.g., negative physical value, invalid resource type, missing required fields).
  - `503 Service Unavailable`: Ingestion suspended due to circuit breaker trip, queue full, or compliance lockout (SRS BR-3).

### 6.2 Ingest Packet Batch: `POST /api/v1/telemetry/batch`
- **Purpose:** Ingest up to 1,000 packets in a single HTTP request.
- **Request Body (`BatchIngestRequest`):**
  ```json
  {
    "packets": [
      {
        "sensor_id": "sensor-001",
        "timestamp": "2026-10-11T12:00:00Z",
        "resource_type": "energy",
        "value": 3.4,
        "unit": "kWh"
      }
    ]
  }
  ```
- **Success Response (`202 Accepted`):**
  ```json
  {
    "accepted": 1,
    "rejected": 0,
    "message": "Batch processed."
  }
  ```

---

## 7. Configuration & Compliance Endpoints

### 7.1 Get Inspector Thresholds: `GET /api/v1/config/inspector`
- **Purpose:** Retrieve active sliding-window configuration.
- **Auth:** JWT (`security_engineer`, `compliance_auditor`, `qa_tester`, `backend_developer`).
- **Response (`200 OK`):**
  ```json
  {
    "window_seconds": 10,
    "max_packets_per_window": 50,
    "cleanup_interval": 60
  }
  ```

### 7.2 Update Inspector Thresholds: `PUT /api/v1/config/inspector`
- **Purpose:** Update sliding-window rate limit thresholds (SRS BR-1).
- **Auth:** JWT (`security_engineer` only).
- **Request Body (`InspectorConfig`):**
  ```json
  {
    "window_seconds": 10,
    "max_packets_per_window": 100,
    "cleanup_interval": 60
  }
  ```
- **Response (`200 OK`):**
  ```json
  {
    "updated": true,
    "message": "Inspector thresholds updated successfully.",
    "updated_by": "admin",
    "updated_at": "2026-10-11T16:00:00Z"
  }
  ```

### 7.3 Get Anonymizer PII Fields: `GET /api/v1/config/anonymizer`
- **Purpose:** Retrieve configured list of stripped PII fields.
- **Auth:** JWT (`security_engineer`, `compliance_auditor`, `qa_tester`, `backend_developer`).
- **Response (`200 OK`):**
  ```json
  {
    "pii_fields": ["latitude", "longitude", "lat", "lon", "mac_address", "device_id", "geo"]
  }
  ```

### 7.4 Update Anonymizer PII Fields: `PUT /api/v1/config/anonymizer`
- **Purpose:** Modify the list of stripped PII fields (SRS BR-1).
- **Auth:** JWT (`security_engineer` only).
- **Request Body (`AnonymizerConfig`):**
  ```json
  {
    "pii_fields": ["latitude", "longitude", "lat", "lon", "mac_address", "device_id", "geo", "ipv4_address"]
  }
  ```
- **Response (`200 OK`):**
  ```json
  {
    "updated": true,
    "message": "Anonymizer PII fields updated successfully.",
    "updated_by": "admin",
    "updated_at": "2026-10-11T16:05:00Z"
  }
  ```

### 7.5 Get Middleware Component Status: `GET /api/v1/config/status`
- **Purpose:** Detailed operational statistics of all middleware components.
- **Auth:** JWT (`security_engineer`, `compliance_auditor`, `qa_tester`, `backend_developer`, `devops`).
- **Response (`200 OK`):**
  ```json
  {
    "environment": "development",
    "inspector": { "window_seconds": 10, "max_packets": 50, "backend": "in_memory" },
    "anonymizer": { "pii_fields_count": 7 },
    "circuit_breaker": { "state": "CLOSED", "failure_threshold_ms": 2000 },
    "pipeline": { "running": true, "batch_size": 100 },
    "queue": { "size": 0, "maxsize": 100000 }
  }
  ```

### 7.6 Get Compliance Status: `GET /api/v1/config/compliance/status`
- **Purpose:** Check compliance guideline version and lockout state (SRS REQ-PA-2, BR-3).
- **Auth:** JWT (`security_engineer`, `compliance_auditor`, `qa_tester`, `backend_developer`).
- **Response (`200 OK`):**
  ```json
  {
    "guideline_version": "1.0.0",
    "lockout_active": false,
    "audited": true,
    "message": "Compliance guidelines verified and active."
  }
  ```

### 7.7 Revise Compliance Guidelines: `PUT /api/v1/config/compliance/guideline`
- **Purpose:** Revise compliance guidelines. **Immediately suspends all simulation testing (lockout)** until formal audit signoff (SRS BR-3).
- **Auth:** JWT (`security_engineer`, `compliance_auditor`).
- **Request Body (`ComplianceGuidelineRevisionRequest`):**
  ```json
  {
    "guideline_version": "2.0.0",
    "revision_reason": "Updated municipal privacy regulation compliance requirements."
  }
  ```
- **Response (`200 OK`):**
  ```json
  {
    "updated": true,
    "message": "Compliance guidelines revised to 2.0.0. Simulation testing SUSPENDED pending audit signoff (SRS BR-3).",
    "updated_by": "admin",
    "updated_at": "2026-10-11T16:10:00Z"
  }
  ```

### 7.8 Compliance Audit Signoff: `POST /api/v1/config/compliance/audit-signoff`
- **Purpose:** Formal audit signoff that lifts BR-3 lockout and resumes simulation testing.
- **Auth:** JWT (`compliance_auditor`, `security_engineer`).
- **Request Body (`ComplianceAuditRequest`):**
  ```json
  {
    "guideline_version": "2.0.0",
    "audit_notes": "Formally audited Anonymizer pipeline against 2.0.0 guidelines; sanitization verified."
  }
  ```
- **Response (`200 OK`):**
  ```json
  {
    "updated": true,
    "message": "Compliance audit signoff recorded for version 2.0.0. Simulation testing RESUMED (SRS BR-3).",
    "updated_by": "auditor",
    "updated_at": "2026-10-11T16:15:00Z"
  }
  ```

---

## 8. Audit Log Endpoints

### 8.1 Query Audit Logs: `GET /api/v1/audit/logs`
- **Purpose:** Query paginated, filtered, PII-sanitized security audit logs (SRS REQ-SE-4).
- **Auth:** JWT (`security_engineer`, `compliance_auditor`, `qa_tester`, `backend_developer`).
- **Query Parameters:**
  - `page`: int (default: 1, min: 1)
  - `page_size`: int (default: 50, min: 1, max: 500)
  - `event_type`: string optional (e.g. `RATE_LIMIT_EXCEEDED`, `ANOMALOUS_PAYLOAD`, `AUTH_FAILURE`, `COMPLIANCE_LOCKOUT`)
  - `severity`: string optional (`INFO`, `WARN`, `ERROR`, `CRITICAL`)
  - `sensor_id`: string optional
- **Response (`200 OK`):**
  ```json
  {
    "entries": [
      {
        "id": 142,
        "ts": "2026-10-11T16:10:00.123456Z",
        "event_type": "COMPLIANCE_LOCKOUT",
        "severity": "CRITICAL",
        "sensor_id": null,
        "user_id": "admin",
        "detail": {
          "guideline_version": "2.0.0",
          "reason": "Guidelines revised without compliance audit approval (SRS BR-3)."
        },
        "ip_address": "127.0.0.1"
      }
    ],
    "total": 1,
    "page": 1,
    "page_size": 50
  }
  ```

---

## 9. Global Error Codes & Schema

| HTTP Status | Exception Class | Description / Trigger |
| :--- | :--- | :--- |
| `400 Bad Request` | `HTTPException` | Malformed parameters or invalid request syntax. |
| `401 Unauthorized` | `InvalidTokenError` | Missing, expired, or invalid JWT Bearer token. |
| `403 Forbidden` | `InsufficientRoleError` | Authenticated user lacks required role for the endpoint. |
| `413 Payload Too Large`| `PayloadSizeLimitMiddleware` | Incoming request exceeds 512 KB (524,288 bytes) limit. |
| `422 Unprocessable` | `RequestValidationError` | JSON payload fails Pydantic schema validation. |
| `503 Unavailable` | `CircuitBreakerOpenError` | Circuit breaker tripped open due to database latency > 2,000 ms. |
| `503 Unavailable` | `QueueFullError` | Ingestion queue capacity exhausted. |
| `503 Unavailable` | `ComplianceLockout` | Telemetry suspended pending compliance audit signoff (SRS BR-3). |
| `503 Unavailable` | `FailClosed` | Security middleware entered fail-closed state. |
