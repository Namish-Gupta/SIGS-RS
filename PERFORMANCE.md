# SIGS-RS Performance Benchmarking & Capacity Analysis

## 1. Executive Summary & Authoritative Targets

This document synthesizes the performance benchmarking methodology, empirical results, and capacity boundaries for the **SIGS-RS Gateway**. Measurements are drawn directly from the baseline performance audit ([`docs/phase15/benchmark-report.md`](file:///home/namish/projects/SIGS-RS/docs/phase15/benchmark-report.md)) executed by the automated benchmarking engine ([`benchmarks/`](file:///home/namish/projects/SIGS-RS/benchmarks)).

### 1.1 SRS Target Comparison & Verification Status

| SRS Requirement | Target Specification | Locally Measured Result | Compliance Status | Audit Evidence & Production Requirements |
| :--- | :--- | :--- | :---: | :--- |
| **REQ-PF-1** (Throughput) | Sustained 50,000 pkts/s across 10,000 sensors | Inspector capacity: ~106k–150k pkts/s; Anonymizer capacity: ~10k–11k pkts/s; Combined middleware: ~6.3k–8.8k pkts/s; E2E persisted: up to 3,820 pkts/s | **NOT VERIFIED (Production Scale) / REPORTED (Local Capacity)** | Single Python process capacity varies by pipeline boundary. Sustained 50,000 pkts/s across 10,000 concurrent sensors was NOT VERIFIED locally and remains an architectural multi-node cluster requirement (16 vCPUs, 32 GB RAM per SRS §3.2). |
| **REQ-PF-2** (Middleware Latency) | Combined Inspector + Anonymizer in < 15.0 ms | p99 is **< 0.50 ms** across all local test stages | **ACHIEVED (Locally Verified)** | Evaluated strictly at the middleware processing boundary (Inspector validation + PII stripping). In-memory execution achieves sub-millisecond p99 latency per packet, well within the 15 ms SRS budget. |
| **REQ-PF-3** (Memory Efficiency) | Stateful tracking < 24 GB active RAM | Peak process RSS < **150 MiB** during Stages 0–2 | **NOT VERIFIED (Production Scale) / BOUNDED (Local Stages 0–2)** | Process RSS remained strictly bounded without runaway growth during short Stage 0–2 executions. Production-scale memory under 10,000 sensors (<24 GB) and long-term memory leak freedom require long-duration cluster soak testing. |
| **REQ-API-4** (Inspector Latency) | Sliding-window and bounds check in < 15.0 ms | p99 is **< 0.02 ms** for in-memory deque; Redis Lua skipped | **ACHIEVED (In-Memory Deque) / UNAVAILABLE (Redis Lua)** | In-memory sliding-window inspection achieves sub-0.02 ms latency. Redis Lua rate-limiting was not verified locally because fakeredis requires the optional 'lupa' C-extension, which is not installed. |

---

## 2. Benchmark Environment

All benchmarks were executed on the following inspected host environment:

| System Attribute | Measured Host Specification |
| :--- | :--- |
| **Operating System** | `Linux 6.6.87.2-microsoft-standard-WSL2 (x86_64)` |
| **CPU Model** | `11th Gen Intel(R) Core(TM) i5-1135G7 @ 2.40GHz` (4 cores / 8 vCPUs) |
| **Host Memory** | `3,814.2 MiB Total RAM` (~3.0 GiB available) |
| **Python Runtime** | `Python 3.12.3` (`.venv/bin/python`) |

---

## 3. Methodology: Microbenchmarks vs. End-to-End Pipeline

The benchmarking suite employs two complementary methodologies to evaluate performance:

1. **Component Saturation Microbenchmarks (Categories 1–3):**
   - Measures raw unpaced single-core throughput of isolated components:
     - `inspector_in_memory`: In-memory sliding window deque + physical bounds checks.
     - `anonymizer_only`: PII stripping and validation.
     - `middleware_combined_in_memory`: Sequential Inspector → Anonymizer orchestration.
   - Initial 20-packet warm-up is excluded from measurement intervals.
2. **Rate-Controlled End-to-End Pipeline Load Tests (Category 4):**
   - Simulates a production ingestion pipeline: concurrent producers inject telemetry into `IngestionQueue` at target offered rates, while a background consumer task dequeues batches (`batch_size=100`), executes the Inspector, strips PII via the Anonymizer, and writes to `InMemoryDatabasePool`.
   - Per-packet end-to-end latency is recorded individually from queue entry to successful database write.

---

## 4. Empirical Benchmark Results (Stages 0–2)

### 4.1 Component Saturation Microbenchmarks

| Component Suite | Stage (Population) | Packets Tested | Saturation Capacity (pkts/s) | Avg Latency (ms) | p50 (ms) | p95 (ms) | p99 (ms) | Peak RSS (MiB) | Single Core CPU (%) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `inspector_in_memory` | `10_sensors_100_pps` | 200 | 106,615.7 | 0.0065 | 0.0059 | 0.0079 | 0.0292 | 62.9 | 99.8% |
| `anonymizer_only` | `10_sensors_100_pps` | 200 | 11,052.6 | 0.0876 | 0.0750 | 0.1309 | 0.1898 | 66.1 | 100.0% |
| `middleware_combined` | `10_sensors_100_pps` | 200 | 8,831.2 | 0.1096 | 0.1065 | 0.1596 | 0.2809 | 66.1 | 100.0% |
| `inspector_in_memory` | `100_sensors_1000_pps` | 3,000 | 113,129.4 | 0.0075 | 0.0071 | 0.0104 | 0.0187 | 75.8 | 100.0% |
| `anonymizer_only` | `100_sensors_1000_pps` | 3,000 | 10,138.9 | 0.0969 | 0.0883 | 0.1651 | 0.2506 | 76.2 | 100.0% |
| `middleware_combined` | `100_sensors_1000_pps` | 3,000 | 6,368.1 | 0.1539 | 0.1249 | 0.3117 | 0.4314 | 76.3 | 100.0% |
| `inspector_in_memory` | `1000_sensors_5000_pps` | 15,000 | 149,761.4 | 0.0058 | 0.0047 | 0.0098 | 0.0175 | 119.2 | 100.0% |
| `anonymizer_only` | `1000_sensors_5000_pps` | 15,000 | 11,020.9 | 0.0893 | 0.0741 | 0.1649 | 0.2420 | 118.3 | 99.9% |
| `middleware_combined` | `1000_sensors_5000_pps` | 15,000 | 7,538.2 | 0.1299 | 0.1159 | 0.2298 | 0.3722 | 120.8 | 100.0% |

### 4.2 Rate-Controlled End-to-End Pipeline Results

| Stage | Sensors | Target Offered Rate | Actual Submitted Rate | Achieved Persisted Rate | Per-Packet E2E Avg (ms) | E2E p50 (ms) | E2E p95 (ms) | E2E p99 (ms) | DB Batch Write Avg (ms) | Max Queue Depth |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `10_sensors_100_pps` | 10 | 100 pkts/s | 100.3 pkts/s | 95.2 pkts/s | 0.6340 | 0.6069 | 0.9418 | 1.0492 | 0.1762 | 1 |
| `100_sensors_1000_pps` | 100 | 1,000 pkts/s | 1,000.3 pkts/s | 950.3 pkts/s | 1.0129 | 0.9086 | 1.7662 | 2.4871 | 0.2345 | 5 |
| `1000_sensors_5000_pps` | 1,000 | 5,000 pkts/s | 4,021.4 pkts/s | 3,820.4 pkts/s | 5.7005 | 5.2971 | 9.5179 | 11.4553 | 1.2160 | 25 |

*Note: Achieved persisted rates are lower than submitted rates by exactly the 5% anomaly injection ratio specified by the workload model (10 of 200 packets dropped in Stage 0; 150 of 3,000 in Stage 1; 750 of 15,000 in Stage 2).*

---

## 5. Bottleneck Attribution Analysis

1. **Middleware Latency Distribution:**  
   In Stage 2 measurements, average per-packet latency was:
   - Standalone Inspector: **0.0058 ms** (~4.5% of combined middleware latency)
   - Standalone Anonymizer: **0.0893 ms** (~68.7% of combined middleware latency)
   - Combined Middleware Pipeline: **0.1299 ms**  
   The remaining ~26.8% (0.0348 ms) reflects pipeline orchestration overhead, intermediate data structures, and conditional branch handling for anomalies. Anonymization is the primary computational consumer in the middleware due to deep dictionary copy and key deletion operations.
2. **Throughput Scaling Dynamics:**  
   Throughput capacity is boundary-dependent:
   - Standalone Inspector scales up to **~150,000 pkts/s** per core.
   - Standalone Anonymizer saturates at **~11,000 pkts/s** per core.
   - Combined middleware achieves **~7,500 pkts/s** per core.
   - E2E persisted pipeline achieved **3,820 pkts/s** under a 4,021 pkts/s submitted load on the test machine.
3. **Queue Dynamics & Persistence Impact:**  
   At offered rates up to 1,000 pkts/s, queue depth remained minimal (max depth: 5). At 5,000 pkts/s offered, queue depth peaked at 25 packets, absorbing burst ingestion smoothly without dropping packets.

---

## 6. Running the Benchmark Suite

The benchmark harness is executed via `benchmarks.cli`:

```bash
# Execute safe baseline Stages 0, 1, and 2 across all suites
python -m benchmarks.cli --stages 0,1,2 --suites all --output docs/phase15/benchmark-report.md

# Run only the isolated Inspector benchmark for Stage 1 (100 sensors)
python -m benchmarks.cli --stages 1 --suites inspector

# Run End-to-End pipeline benchmark for Stage 0
python -m benchmarks.cli --stages 0 --suites e2e
```

### Safety Guardrails
To prevent accidental host memory or CPU exhaustion, Stages 3 (5,000 sensors) and Stage 4 (10,000 sensors / 50,000 pkts/s) require explicit safety flags:
```bash
# Stage 3 requires explicit flag
python -m benchmarks.cli --stages 3 --allow-large-scale

# Stage 4 requires explicit flag
python -m benchmarks.cli --stages 4 --allow-srs-max
```
The internal `SafetyWatchdog` automatically aborts execution if process RSS exceeds 1,536 MiB or available host memory drops below 512 MiB.
