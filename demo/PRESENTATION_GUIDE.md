# SIGS-RS Presentation & Examination Viva Guide

> **Course:** BCSE301L — Software Engineering  
> **Project:** Secure IoT Gateway Simulation for Resource Sensing (SIGS-RS)  
> **Student Author:** Namish Gupta (and Project Partner)  
> **Submission Date:** October 2026

---

## 1. Structure of the 5–10 Minute Viva Presentation

| Time Window | Segment | Focus & Key Talking Points | Visual / Artifact |
| :---: | :--- | :--- | :--- |
| **0:00 – 1:30** | **Problem Statement & Objectives** | • Municipal IoT grids monitor critical infrastructure (water, electric power, air quality).<br>• Public edge sensors are vulnerable to packet flooding, physical value spoofing, and citizen privacy leaks (GPS coordinates, MAC addresses).<br>• SIGS-RS provides a hardened, resilient edge gateway simulation enforcing strict security invariants. | Title Slide / Problem Overview |
| **1:30 – 3:30** | **System Architecture & Telemetry Pipeline** | • Decoupled architecture: Ingestion (HTTP REST & MQTT 8883) $\rightarrow$ IngestionQueue buffer.<br>• **Core Invariant:** $\text{Inspector} \rightarrow \text{Anonymizer} \rightarrow \text{Persistence}$.<br>• Stage 1 (Inspector) drops rate attacks & physical anomalies before PII stripping.<br>• Stage 2 (Anonymizer) strips location & device IDs fail-securely.<br>• Persistence Guard: Circuit breaker stops cascade failures if DB write latency $> 2,000$ ms. | [`ARCHITECTURE.md §2`](../ARCHITECTURE.md#2-diagram-1-system-architecture) (Diagram 1 & 2) |
| **3:30 – 6:30** | **Live Demonstration** | • Launch automated viva test suite: `python -m demo.run_live_demo`.<br>• Verify `/health` and Prometheus `/metrics`.<br>• Ingest normal telemetry with GPS and MAC address.<br>• Inject anomalous packet ($-100.0\text{ L/min}$ negative flow).<br>• Show drop event logged in `audit.security_log` with zero DB persistence.<br>• Inspect saved database record proving complete removal of `latitude`, `longitude`, and `mac_address`. | Terminal Running Demo |
| **6:30 – 8:00** | **Security & Reliability Engineering** | • **BR-3 Compliance Lockout:** Guideline revisions trigger immediate simulation suspension across pods until formal audit signoff.<br>• **Fail-Closed Semantics:** Pipeline crashes or unhandled faults set `/health` to 503.<br>• **Audit Immutability:** Trigger `trg_audit_immutable` blocks SQL `UPDATE` and `DELETE`. | [`SECURITY.md §2`](../SECURITY.md#2-diagram-3-security-pipeline) (Diagram 3) |
| **8:00 – 9:30** | **Verification & Benchmarking Results** | • **580 Unique Tests:** 579 passed, 1 skipped (`test_real_redis_atomicity` due to offline Redis).<br>• **Microbenchmarks:** Inspector achieves $\sim 150,000$ pkts/s; Anonymizer $\sim 11,000$ pkts/s.<br>• **REQ-PF-2 Achieved:** Combined middleware latency p99 $< 0.50$ ms (well below 15 ms SRS budget).<br>• **REQ-PF-1 Disclosed:** 50,000 pkts/s production scale is unverified on local 4-core machine and requires multi-node cluster. | [`PERFORMANCE.md`](../PERFORMANCE.md) / `FINAL_AUDIT.md` |
| **9:30 – 10:00** | **Conclusion & Q&A Transition** | • Summary of achieved software engineering outcomes, design modularity, and readiness for questions. | Summary Slide |

---

## 2. Demonstration Step-by-Step Narration

When demonstrating the system to the instructor, use the following explanations for what is occurring under the hood:

### Narration: Step 2 — Gateway Startup & Lifespan
> *"When the gateway starts up, FastAPI enters its asynchronous lifespan context. It initializes the async connection pool, falling back safely to `InMemoryDatabasePool` in local development mode so we do not require a live PostgreSQL container. It starts the background queue worker task, the MQTT subscriber task with an exponential backoff reconnect loop, and the compliance synchronization loop."*

### Narration: Step 5 — Ingesting Normal Telemetry
> *"Here we submit a telemetry packet containing sensitive customer data: exact GPS coordinates `12.9716, 80.2209` and a hardware MAC address `AA:BB:CC:DD:EE:FF`. The HTTP ingestion endpoint validates the content size against the 512 KB limit and immediately enqueues the packet into an in-memory `asyncio.Queue`, returning HTTP 202 Accepted within 4 milliseconds. This decouples socket I/O from middleware processing."*

### Narration: Step 7 & 8 — Injecting Anomalies & Proving Rejection
> *"Next, we simulate a compromised sensor attempting to inject impossible physical readings: a water flow rate of $-100\text{ L/min}$. In nature, water flow cannot be negative. The packet is enqueued, but when the background worker passes it to the Anomalous Packet Inspector, the Physical Bounds Validator catches the violation. The packet is dropped immediately. Notice that the database reading count remains 1. The malicious packet was rejected before it ever reached the Privacy Anonymizer."*

### Narration: Step 10 & 11 — Demonstrating the Security Invariant
> *"Now we inspect the persisted record in the database. As required by SRS REQ-PA-1, the Privacy Anonymizer stripped the latitude, longitude, and MAC address. We query the object and verify that zero PII exists in the storage layer. Furthermore, the anomalous packet generated an immutable security audit event in `audit.security_log`."*

---

## 3. High-Probability Viva Examination Q&A

### Q1: Why did you build a two-stage sequential middleware instead of combining inspection and anonymization into a single function?
**Answer:**  
Separating the **Anomalous Packet Inspector** and **Privacy Anonymizer** enforces the principle of Single Responsibility and optimizes resource usage:
1. **Security & Drop Efficiency:** Malicious, impossible, or rate-flooding packets are dropped at Stage 1. They never enter Stage 2, saving the CPU cost of deep dictionary copying and PII redaction.
2. **Audit Accuracy:** When an anomaly is detected, we log the drop event with the original sensor ID and violation details. If we anonymized first, the forensic sensor ID might be masked or lost.
3. **Independent Testability (SRS REQ-QA-4):** Each middleware component has dedicated unit tests and can be updated, benchmarked, or scaled independently.

---

### Q2: Why is the IngestionQueue necessary? Why not process packets directly in the FastAPI route handler?
**Answer:**  
Direct processing would tightly couple HTTP socket throughput to database write latency. If the database experiences a momentary latency spike, HTTP connections would stall, consuming socket descriptors and worker threads.  
The `IngestionQueue` (bounded `asyncio.Queue` per SRS REQ-TI-4) acts as a shock absorber. The ingestion handler accepts and enqueues valid payloads in $< 5\text{ ms}$, while the background worker drains batches (`batch_size=100`) to maximize PostgreSQL batch insert throughput.

---

### Q3: How does the sliding-window rate limiter work?
**Answer:**  
It enforces a rolling window of 50 packets per 10 seconds per virtual sensor (SRS REQ-API-1).  
- **In-Memory Backend:** Employs a timestamp `collections.deque` per sensor. Timestamps older than `now - 10s` are popped. If the queue length reaches 50, subsequent packets are dropped.
- **Production Redis Backend:** Uses a Redis Sorted Set (`ZSET`) with an atomic Lua script (`ZREMRANGEBYSCORE`, `ZCARD`, `ZADD`). This enables horizontal scaling across multiple gateway pods without rate-limit state divergence.

---

### Q4: How does the circuit breaker prevent cascade failures (SRS REQ-SF-2)?
**Answer:**  
The `CircuitBreaker` wraps the database repository pool:
1. If database write latency exceeds 2,000 ms, or if database operations fail consecutively, the breaker transitions from `CLOSED` to `OPEN`.
2. While `OPEN`, all new telemetry ingestion is suspended immediately (HTTP 503 Service Unavailable), preventing memory exhaustion and queue overflow.
3. After a 30-second cooldown, it enters `HALF-OPEN`, executing probe queries before allowing traffic to resume.

---

### Q5: What is the SRS BR-3 Compliance Lockout, and how does it work across multiple instances?
**Answer:**  
Business Rule 3 mandates that if data privacy compliance guidelines are revised, all simulation testing must be immediately suspended until a formal compliance audit signoff occurs.
- On the local instance, updating guidelines (`PUT /api/v1/config/compliance/guideline`) immediately sets `compliance_lockout = True`, suspending telemetry.
- Across a distributed cluster, the lockout state is written to `config.middleware_settings`. All gateway instances run a 1.0-second background polling task that synchronizes the lockout state from the database. Testing can only resume when an authorized `compliance_auditor` or `security_engineer` posts a formal audit signoff (`POST /api/v1/config/compliance/audit-signoff`).

---

### Q6: Does your PostgreSQL trigger provide Write-Once-Read-Many (WORM) audit storage?
**Answer:**  
*Honest technical answer:* It provides **application-level and database-level immutability**, but **not hardware-level WORM storage**.  
The trigger `trg_audit_immutable` intercepts all `UPDATE` and `DELETE` queries on `audit.security_log` and raises a PostgreSQL exception. However, a database superuser with root access could drop the trigger. True regulatory WORM storage requires cloud-managed immutable object storage (such as AWS S3 Object Lock in Compliance Mode), as documented in our `FINAL_AUDIT.md`.

---

### Q7: Did you achieve the SRS performance target of 50,000 packets/second across 10,000 sensors (REQ-PF-1)?
**Answer:**  
*Honest technical answer:* **No, that is marked as NOT VERIFIED AT PRODUCTION SCALE.**  
On our local test machine (4 physical cores, 3.8 GiB RAM), a single Python process achieved:
- Inspector capacity: $\sim 150,000$ pkts/s.
- Anonymizer capacity: $\sim 11,000$ pkts/s.
- End-to-end persisted throughput: $3,820$ pkts/s.  
Sustaining 50,000 pkts/s across 10,000 sensors requires the multi-replica Kubernetes cluster specified in SRS §3.2 (16 vCPUs, 32 GB RAM, TimescaleDB cluster, and EMQX broker). We do not claim local compliance for production-scale targets.

---

## 4. Backup & Fallback Demonstration Plan

| Potential Live Failure | Root Cause | Fallback Strategy |
| :--- | :--- | :--- |
| **Port 8000 already in use** | A dangling process is holding the port. | Run on alternative port: `uvicorn demo.demo_server:app --port 8080`, or use the hermetic in-process script `python -m demo.run_live_demo`. |
| **Terminal / curl command typos** | Manual copy-paste error during live examination. | Execute the automated script `python -m demo.run_live_demo`, which runs all 12 steps deterministically and outputs colorized assertions. |
| **External Redis or Mosquitto down** | External daemons are not running. | The demo server is explicitly configured with `MockAsyncSlidingWindow` and `InMemoryDatabasePool`, requiring zero external services. |
| **Instructor asks to view Swagger UI** | Wants interactive web browser verification. | Open `http://localhost:8000/docs`. Execute `POST /api/v1/telemetry` directly through the Swagger interactive UI. |
