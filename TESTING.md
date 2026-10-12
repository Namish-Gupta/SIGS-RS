# SIGS-RS Test Suite & Verification Guide

## 1. Overview & Test Framework

The **SIGS-RS** test suite is built on **pytest** and **pytest-asyncio**, with test client integration via **httpx** and test coverage analysis via **pytest-cov**.

### 1.1 Test Suite Accounting & Inventory

As of Phase 16/17 verification, the test suite contains **580 unique collected tests** organized across three primary directories:

| Test Directory | Unique Tests | Passed | Skipped | Failed | Infrastructure Dependencies |
| :--- | :---: | :---: | :---: | :---: | :--- |
| [`tests/unit/`](file:///home/namish/projects/SIGS-RS/tests/unit) | 463 | 463 | 0 | 0 | Pure in-memory (no I/O, no network, no external daemons). |
| [`tests/integration/`](file:///home/namish/projects/SIGS-RS/tests/integration) | 96 | 95 | 1 | 0 | FastAPI `TestClient`, `InMemoryDatabasePool`, simulated transports. |
| [`tests/benchmarks/`](file:///home/namish/projects/SIGS-RS/tests/benchmarks) | 21 | 21 | 0 | 0 | In-memory microbenchmark smoke and safety assertion tests. |
| **Total Test Inventory** | **580** | **579** | **1** | **0** | **99.8% Pass Rate** |

> [!NOTE]
> **Documented Skip Reason:**  
> Exactly 1 test is skipped in offline local execution: `test_real_redis_atomicity` in [`tests/integration/test_sliding_window_real.py`](file:///home/namish/projects/SIGS-RS/tests/integration/test_sliding_window_real.py#L31). It checks for an active Redis instance on `127.0.0.1:6379` and gracefully skips when Redis is not running. All other 579 tests execute and pass without requiring external services.

---

## 2. Directory Layout & Module Breakdown

```
tests/
├── conftest.py                             # Global fixtures, test app factory, mock stores
├── unit/                                   # 463 tests: Pure unit tests without network I/O
│   ├── test_anonymizer.py                  # PII stripping, fail-secure anonymization
│   ├── test_audit_api.py                   # Audit log retrieval and PII redaction
│   ├── test_auth.py                        # JWT generation, validation, expiry, password hashing
│   ├── test_circuit_breaker.py             # Latency thresholds, state transitions (CLOSED/OPEN/HALF-OPEN)
│   ├── test_compliance_lockout.py          # BR-3 guideline revision and audit signoff logic
│   ├── test_config_api.py                  # RBAC authorization for Inspector and Anonymizer configs
│   ├── test_config.py                      # Pydantic Settings parsing and validation
│   ├── test_health.py                      # /health, /ready, component states, fail-closed statuses
│   ├── test_http_ingest.py                 # HTTP payload size limits, queue dispatch, error handling
│   ├── test_inspector.py                   # Sliding-window and impossible-value detection
│   ├── test_metrics.py                     # Prometheus text exposition and JSON metric generation
│   ├── test_mqtt_subscription.py           # MQTT topic parsing, TLS context construction, size checks
│   ├── test_payload_validator.py           # Physical bounds validator (water, energy, air quality)
│   ├── test_phase14_security_audit.py      # Hostile security regression tests
│   ├── test_phase16_audit_hardening.py     # Lockout persistence, concurrency, and audit immutability
│   ├── test_pipeline.py                    # Pipeline orchestration and batch drainage
│   ├── test_repository.py                  # TelemetryRepository SQL generation and mapping
│   ├── test_roles.py                       # RBAC role requirement helpers
│   ├── test_schemas.py                     # TelemetryPayload Pydantic validation
│   ├── test_simulator.py                   # Virtual sensor generator, modes, and CLI argument parsing
│   ├── test_sliding_window.py              # In-memory sliding window deque and TTL expiration
│   ├── test_telemetry_models.py            # Unit domain models, conversions, and metadata
│   └── test_telemetry_validator.py         # Payload size gating and JSON malformed handling
├── integration/                            # 96 tests: Subsystem integration and failure tests
│   ├── test_api.py                         # Full HTTP REST route integration
│   ├── test_failures.py                    # Fail-closed behavior on database/queue failure
│   ├── test_mqtt_ingestion.py              # MQTT message reception and pipeline handoff
│   ├── test_persistence.py                 # Hypertable insert and query integration
│   └── test_sliding_window_real.py         # Real Redis sliding-window integration (1 skipped if offline)
└── benchmarks/                             # 21 tests: Benchmark harness smoke verification
    ├── test_bench_inspector.py             # Inspector latency and rate limit microbenchmarks
    └── test_benchmark_smoke.py             # Benchmark stage validation and safety watchdog checks
```

---

## 3. Test Execution Commands

All commands assume the Python virtual environment is activated (`source .venv/bin/activate`).

### 3.1 Running the Entire Test Suite

```bash
# Run all 580 tests quietly
pytest -q

# Run all tests with verbose output and test names
pytest -v
```

### 3.2 Running Specific Test Suites

```bash
# Run unit tests only (463 tests)
pytest tests/unit/ -v

# Run integration tests only (96 tests)
pytest tests/integration/ -v

# Run benchmark smoke and microbenchmark tests (21 tests)
pytest tests/benchmarks/ -v
```

### 3.3 Running Focused Security Regression Suites

```bash
# Run Phase 16 audit hardening suite (32 tests: BR-3 lockout, immutability, MQTT audit)
pytest tests/unit/test_phase16_audit_hardening.py -v

# Run Phase 14 hostile QA security audit suite (15 tests: injection, spoofing, edge cases)
pytest tests/unit/test_phase14_security_audit.py -v
```

### 3.4 Running Individual Test Modules & Functions

```bash
# Test privacy anonymizer logic
pytest tests/unit/test_anonymizer.py -v

# Test circuit breaker state transitions
pytest tests/unit/test_circuit_breaker.py -v

# Run a single test function
pytest tests/unit/test_inspector.py -k "test_sliding_window_rate_limit" -v
```

### 3.5 Generating Test Coverage Reports

```bash
# Terminal summary report
pytest --cov=sigs_rs --cov-report=term-missing

# Detailed HTML coverage report (outputs to htmlcov/index.html)
pytest --cov=sigs_rs --cov-report=html
```

---

## 4. Test Infrastructure: Mocks vs. Live Dependencies

The test architecture is designed for deterministic, hermetic execution without requiring live external daemons:

| Component Under Test | Test Isolation Strategy | Production Equivalent |
| :--- | :--- | :--- |
| **Database Persistence** | Tested against `InMemoryDatabasePool` or mock asyncpg pools that emulate schema tables and hypertable inserts in memory. | PostgreSQL 16 + TimescaleDB. |
| **MQTT Ingestion** | Ingestion logic and TLS context building are tested using mock `aiomqtt.Client` message streams and byte payloads. | Eclipse Mosquitto or EMQX broker. |
| **Sliding-Window Rate Limiter** | Tested against the in-memory timestamp `collections.deque` implementation. | Distributed Redis cluster. |
| **Audit Logger** | In-memory audit sink asserting that events and severities match SRS specifications. | Append-only `audit.security_log` table with `trg_audit_immutable`. |
