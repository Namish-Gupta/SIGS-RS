# SIGS-RS project status

Assessment date: 2026-09-11. This report is based on the checked-in Python, test, Docker, and requirements artifacts; the README's phase table was not treated as proof of implementation.

## SRS and use-case intent

SIGS-RS is a backend-only simulation gateway. It must accept telemetry from a virtual sensor grid over MQTT and HTTP, parse and queue it, then enforce this order before storage: **Anomalous Packet Inspector -> Privacy Anonymizer -> PostgreSQL/TimescaleDB**. The Inspector must keep a per-sensor rolling 10-second window, allow at most 50 packets in that window, reject impossible primary values, and silently drop malicious packets. The Anonymizer must remove geolocation and device identifiers and fail secure if it cannot do so. The SRS additionally requires TLS/X.509 MQTT on 8883, OAuth2/JWT-protected management APIs, immutable audit records, secure DB connectivity, a 2-second database circuit breaker, Docker, Kubernetes/HPA, health/metrics, and 50,000 packets/second for 10,000 sensors.

The use-case diagram depicts four actors: Security Engineer, Virtual Sensor Grid, QA Tester, and External Persistent Database. Its primary path is **Transmit Telemetry -> Evaluate Packet Anomalies -> Anonymize Payload -> Persist Sanitized Logs**. It also assigns security configuration and audit-log review to the Security Engineer, and traffic-load simulation to QA. It is a high-level behavioral diagram; it does not describe queues, breaker behavior, or deployment implementation.

## Current architecture (what actually runs)

The project is a Python 3.12/FastAPI package with Pydantic v2 models and an intended asynchronous, in-process architecture:

`HTTP or MQTT -> IngestionQueue -> Inspector -> Anonymizer -> CircuitBreaker -> Repository -> TimescaleDB`

Only the first HTTP queue hand-off is live today. `POST /api/v1/telemetry` lets FastAPI parse a `RawTelemetryPacket`, checks only the `Content-Length` header, and puts the packet in the bounded `asyncio.Queue` (default 10,000). `MiddlewarePipeline.run()` is a sleep loop, so queued packets are not inspected, anonymized, or persisted. The MQTT subscriber starts and immediately returns after logging that it is a stub; its private `_parse()` helper is not called by a subscription loop.

The Inspector and Anonymizer are complete as isolated modules. The `DatabasePool`, `TelemetryRepository`, and circuit-breaker transition logic are interfaces/stubs, so there is no operational PostgreSQL/TimescaleDB path. Docker Compose provides app, Mosquitto, and TimescaleDB service definitions, but the development broker enables only anonymous plaintext MQTT on 1883; its TLS listener on 8883 is commented out. `k8s/` contains only `.gitkeep`.

## Components that exist

- **Telemetry domain:** `RawTelemetryPacket`, `SanitizedTelemetryPacket`, resource-specific extensions, PII-aware `GeoLocation`, and `TelemetryValidator`. Raw packets allow PII; sanitized packets have no `geo`, coordinates, or MAC fields by design.
- **Ingestion:** `IngestionQueue`, HTTP single/batch endpoints, and the MQTT subscriber skeleton.
- **Inspector:** `SlidingWindow`, `PayloadValidator`, `AnomalousPacketInspector`, and `VerificationResult`. The window uses per-sensor `deque`s and `time.monotonic()`; exactly 50 packets pass and the 51st within the 10-second window is rejected. Rate checking precedes metric checking, so an invalid metric still consumes a rate-window slot once it passes the rate check.
- **Privacy:** `PrivacyAnonymizer` recursively removes configured exact key names (`latitude`, `longitude`, `lat`, `lon`, `mac_address`, `geo`) and verifies none remain before constructing a sanitized model.
- **Persistence/resilience:** `DatabasePool`, `TelemetryRepository`, `CircuitBreaker`, and `CBState` declarations. No pool creation, SQL, schema/migration, hypertable, real audit insert, latency measurement, trip/reset, or recovery behavior exists.
- **Access and management:** local bcrypt-backed seeded-admin login, HS256 JWT creation/validation, exact-role checks, config/audit endpoint shells, structured stdout audit logging, and `/health`, `/ready`, `/metrics`.
- **Simulation/deployment:** load-profile definitions and packet generation helper (but no publisher), Dockerfiles, Compose, and Mosquitto configuration.

## Completed phases

There is no end-to-end completed delivery phase. The following phase-level work is substantively implemented in isolation:

- **MQTT Ingestion / Phase 6:** `MQTTSubscriber` with TLS 1.2+, mTLS client certificate authentication, size gate (512 KB), JSON/schema validation via `TelemetryValidator`, exponential backoff reconnect, silent drops, and full pipeline integration.
- **Persistence / Phase 5:** `DatabasePool` (with asyncpg pooling, SSL verification, transactions, health checks), `TelemetryRepository` (with strict type and PII enforcement), SQLAlchemy ORM models, TimescaleDB hypertable setup, init scripts, and integration tests.
- **Privacy / Phase 4:** `PrivacyAnonymizer` and `MiddlewarePipeline` routing (Inspector -> Anonymizer -> Breaker/Repo).

## In-progress / Next phase

**Circuit Breaker resilience & Configuration/Audit API:**
1. Circuit Breaker latency tracking (> 2000 ms), state transitions (CLOSED -> OPEN -> HALF_OPEN), cooldown timer, and fail-closed gate (SRS REQ-SF-2, REQ-SF-3).
2. Configuration & Audit APIs (SRS REQ-SE-4, BR-1, BR-4) with RBAC enforcement.
3. Load generator and simulator validation (SRS REQ-PF-1).

## Important implementation decisions already made

- Resource types are limited to `water`, `energy`, and `air_quality`; units and several extension bounds are enforced by Pydantic before inspection. Unknown payload fields are deliberately ignored for forward compatibility.
- Primary impossible-value rules are water/energy >= 0 and AQI in 0..500. The Inspector is single-event-loop state; separate workers would each have independent rate windows.
- PII stripping is recursive but is key-name based, not JSON-path based. The configured `ANONYMIZER_PII_FIELDS` value is therefore not currently interpreted as dotted JSON paths.
- The configured JWT service is a local seeded-account substitute for the SRS's still-TBD OAuth2 IdP.
- Audit events are emitted as JSON to stdout even without a database. This is not an immutable persisted audit log.

## SRS discrepancies and unverified requirements

| Area | Actual status |
|---|---|
| Telemetry flow | **Major gap.** HTTP packets stop in the queue; MQTT receives no traffic; nothing reaches Inspector, Anonymizer, or database. |
| MQTT/TLS/X.509 (REQ-TI-1, REQ-SE-1) | **Not implemented.** Subscriber is a stub; Compose uses plaintext 1883; 8883 TLS/X.509 setup is commented out. |
| Payload validation/512 KB (REQ-TI-2/3) | **Partial.** `TelemetryValidator` correctly validates raw bytes but is unused by HTTP and live MQTT. HTTP trusts `Content-Length` after body parsing; batch ingestion has no size check. Models ignore extra fields, which is less strict than the SRS wording. |
| 10-second / 50-packet Inspector (REQ-API-1/2) | **Implemented and unit tested in isolation.** Global enforcement across horizontally scaled workers is not solved. |
| Impossible metrics (REQ-API-3) | **Implemented for the primary `value`** including NaN/infinity and AQI > 500; no end-to-end routing test exists. |
| Privacy and fail secure (REQ-PA-1/4) | **Implemented in isolation.** The type boundary prevents known PII fields in sanitized output, but no live pipeline drops/logs anonymizer failures. Compliance Guideline v3.0 is not in the repository, so full policy conformance cannot be confirmed. |
| PostgreSQL/TimescaleDB/hypertables (REQ-PA-3, §6.1) | **Not implemented.** No connection pool, SQL, migration, table, or hypertable exists. |
| Circuit breaker and fail closed (REQ-SF-2/3) | **Not implemented.** State names and health reporting exist, but nothing trips the breaker or sets/enforces `fail_closed`. |
| DB staging guard / DB security (REQ-SF-1, REQ-SE-3) | **Partial/non-compliant.** The URL check rejects hosts containing `prod`, `production`, or `live`, but does not enforce `ALLOWED_DB_HOSTS`; SSL mode is unused and Compose sets it to `disable`. |
| Authentication/RBAC (REQ-SE-2, BR-1) | **Partial.** Config/audit routes require a JWT and Security Engineer role, but authentication is local HS256 JWT rather than OAuth2, telemetry routes are not protected, and unauthorized attempts are not audit logged. |
| Audit trail (REQ-SE-4, BR-4) | **Partial.** Inspector can emit stdout events; database persistence, immutable grants, auth-failure events, configuration-change events, and log queries are absent/stubbed. |
| Docker/Kubernetes/health/metrics | **Partial.** Docker scaffolding and basic endpoints exist; no Kubernetes manifests/HPA/self-healing configuration and no verified deployment. |
| Performance requirements | **Unverified.** Inspector-only benchmarks assert algorithm performance, but no runnable full-pipeline 50,000 pps/10,000-sensor test or memory/latency validation exists. |

## Tests and verification

The repository contains 264 declared test functions: 242 unit tests across models, raw validation/fixtures, anonymizer, JWT/RBAC, config, sliding window, payload validation, and Inspector; 14 HTTP API integration tests; and 8 Inspector microbenchmarks. `tests/e2e/` is only a placeholder. Coverage is strong for isolated Inspector, validator, anonymizer, schema, and auth utilities, but absent for database, MQTT, pipeline, circuit breaker, fail-closed operation, configuration mutation, audit persistence, TLS, Docker, Kubernetes, load publishing, and E2E flow.

Test execution was not verified in this assessment: the system has no `python`/`py` launcher and the bundled Python runtime does not include `pytest`. No dependencies were installed because this task was analysis-only.

## Recommended next step

Begin Phase 4 by implementing and testing the live queue worker and MQTT subscriber **against a defined persistence contract**. The first implementation decision should be whether to complete the missing Phase 2 database schema/repository beforehand or supply an explicitly safe persistence adapter for Phase 4; either way, retain the mandatory Inspector -> Anonymizer -> persistence order and make anonymizer/circuit-breaker failures stop routing.

Assessment limitation: this workspace is not an accessible Git checkout (`git status` reports no repository), so commit history and working-tree provenance could not be assessed.
