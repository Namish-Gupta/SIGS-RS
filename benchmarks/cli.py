"""
CLI entry point for running SIGS-RS performance benchmarks.

Usage:
  python -m benchmarks.cli --stages 0,1,2 --output docs/phase15/benchmark-report.md
  python -m benchmarks.cli --suite inspector --stage 1
  python -m benchmarks.cli --stage 3 --allow-large-scale
  python -m benchmarks.cli --stage 4 --allow-srs-max
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

from benchmarks.reporter import generate_markdown_report
from benchmarks.safety import SafetyWatchdog
from benchmarks.stages import STAGES, get_stage
from benchmarks.suites.anonymizer_bench import run_anonymizer_benchmark
from benchmarks.suites.e2e_pipeline_bench import run_e2e_pipeline_benchmark
from benchmarks.suites.inspector_bench import run_inspector_benchmark
from benchmarks.suites.middleware_bench import run_middleware_benchmark


async def run_benchmark_matrix(
    stages_to_run: list[int],
    suites_to_run: list[str],
    allow_large_scale: bool = False,
    allow_srs_max: bool = False,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    watchdog = SafetyWatchdog(max_rss_mib=1536.0, min_available_mib=512.0)

    for stage_id in stages_to_run:
        stage = get_stage(stage_id, allow_large_scale=allow_large_scale, allow_srs_max=allow_srs_max)
        print(f"\n{'='*70}")
        print(f"Executing Stage {stage.stage_id} [{stage.name.upper()}]: "
              f"{stage.sensor_count} sensors, {stage.offered_rate_pps:,} pkts/s offered, {stage.duration_seconds}s duration")
        print(f"{'='*70}")

        # 1. Inspector In-Memory
        if "all" in suites_to_run or "inspector" in suites_to_run:
            print("  -> Running Inspector (in_memory)...")
            res = await run_inspector_benchmark(
                sensor_count=stage.sensor_count,
                offered_rate_pps=stage.offered_rate_pps,
                duration_seconds=stage.duration_seconds,
                backend="in_memory",
                watchdog=watchdog,
            )
            data = res.to_dict()
            results.append(data)
            print(f"     Achieved: {data['achieved_throughput_pps']:,.1f} pkts/s | "
                  f"Avg: {data['latencies_ms'].get('inspector', {}).get('avg_ms', 0):.4f} ms | "
                  f"p99: {data['latencies_ms'].get('inspector', {}).get('p99_ms', 0):.4f} ms")

        # 2. Inspector Fakeredis (Lua in-memory emulation)
        if ("all" in suites_to_run or "inspector" in suites_to_run) and stage_id <= 2:
            print("  -> Running Inspector (fakeredis Lua emulation)...")
            res = await run_inspector_benchmark(
                sensor_count=stage.sensor_count,
                offered_rate_pps=stage.offered_rate_pps,
                duration_seconds=stage.duration_seconds,
                backend="fakeredis",
                watchdog=watchdog,
            )
            data = res.to_dict()
            results.append(data)
            if data.get("skipped"):
                print(f"     [SKIPPED] {data.get('skip_reason', '')}")
            else:
                print(f"     Achieved: {data['achieved_throughput_pps']:,.1f} pkts/s | "
                      f"Avg: {data['latencies_ms'].get('inspector', {}).get('avg_ms', 0):.4f} ms | "
                      f"p99: {data['latencies_ms'].get('inspector', {}).get('p99_ms', 0):.4f} ms")

        # 3. Anonymizer Only
        if "all" in suites_to_run or "anonymizer" in suites_to_run:
            print("  -> Running Privacy Anonymizer...")
            res = run_anonymizer_benchmark(
                sensor_count=stage.sensor_count,
                offered_rate_pps=stage.offered_rate_pps,
                duration_seconds=stage.duration_seconds,
                watchdog=watchdog,
            )
            data = res.to_dict()
            results.append(data)
            print(f"     Achieved: {data['achieved_throughput_pps']:,.1f} pkts/s | "
                  f"Avg: {data['latencies_ms'].get('anonymizer', {}).get('avg_ms', 0):.4f} ms | "
                  f"p99: {data['latencies_ms'].get('anonymizer', {}).get('p99_ms', 0):.4f} ms")

        # 4. Middleware Combined
        if "all" in suites_to_run or "middleware" in suites_to_run:
            print("  -> Running Combined Middleware (Inspector -> Anonymizer)...")
            res = await run_middleware_benchmark(
                sensor_count=stage.sensor_count,
                offered_rate_pps=stage.offered_rate_pps,
                duration_seconds=stage.duration_seconds,
                backend="in_memory",
                watchdog=watchdog,
            )
            data = res.to_dict()
            results.append(data)
            print(f"     Achieved: {data['achieved_throughput_pps']:,.1f} pkts/s | "
                  f"Avg: {data['latencies_ms'].get('middleware', {}).get('avg_ms', 0):.4f} ms | "
                  f"p99: {data['latencies_ms'].get('middleware', {}).get('p99_ms', 0):.4f} ms")

        # 5. Full End-to-End Pipeline
        if "all" in suites_to_run or "e2e" in suites_to_run:
            print("  -> Running Full E2E Pipeline (Rate-Controlled Queue -> Middleware -> In-Memory DB)...")
            res = await run_e2e_pipeline_benchmark(
                sensor_count=stage.sensor_count,
                offered_rate_pps=stage.offered_rate_pps,
                duration_seconds=stage.duration_seconds,
                watchdog=watchdog,
            )
            data = res.to_dict()
            results.append(data)
            p_stats = data["packets"]
            persisted_pps = round(p_stats["persisted"] / data["duration_seconds"], 1)
            e2e_lat = data["latencies_ms"].get("e2e_persisted", {})
            db_lat = data["latencies_ms"].get("database_batch", {})
            print(f"     Target: {stage.offered_rate_pps:,} pkts/s | "
                  f"Achieved Persisted: {persisted_pps:,} pkts/s | "
                  f"E2E Avg: {e2e_lat.get('avg_ms', 0):.4f} ms | "
                  f"E2E p99: {e2e_lat.get('p99_ms', 0):.4f} ms | "
                  f"DB Batch Avg: {db_lat.get('avg_ms', 0):.4f} ms | "
                  f"Max Queue: {data['queue_depth'].get('max', 0)}")

    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SIGS-RS Performance Benchmarking Suite")
    parser.add_argument(
        "--stages",
        type=str,
        default="0,1,2",
        help="Comma-separated stage IDs to execute (e.g. 0,1,2). Default: 0,1,2",
    )
    parser.add_argument(
        "--suites",
        type=str,
        default="all",
        help="Comma-separated suites: inspector, anonymizer, middleware, e2e, or all. Default: all",
    )
    parser.add_argument(
        "--allow-large-scale",
        action="store_true",
        help="Explicit opt-in to run Stage 3 (5,000 sensors / 10,000 pkts/s)",
    )
    parser.add_argument(
        "--allow-srs-max",
        action="store_true",
        help="Explicit opt-in to run Stage 4 (10,000 sensors / 50,000 pkts/s)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="docs/phase15/benchmark-report.md",
        help="Path to generate Markdown performance report.",
    )

    args = parser.parse_args(argv)
    try:
        stage_ids = [int(s.strip()) for s in args.stages.split(",") if s.strip()]
        suites = [s.strip().lower() for s in args.suites.split(",") if s.strip()]
    except Exception as exc:
        print(f"Error parsing arguments: {exc}", file=sys.stderr)
        return 1

    print("=" * 70)
    print("SIGS-RS Performance Benchmarking Engine")
    print(f"Configured Stages: {stage_ids} | Suites: {suites}")
    print("=" * 70)

    try:
        results = asyncio.run(
            run_benchmark_matrix(
                stages_to_run=stage_ids,
                suites_to_run=suites,
                allow_large_scale=args.allow_large_scale,
                allow_srs_max=args.allow_srs_max,
            )
        )
    except PermissionError as exc:
        print(f"\n[SAFETY ERROR] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"\n[BENCHMARK ERROR] Execution failed: {exc}", file=sys.stderr)
        return 1

    # Generate Markdown report
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    report_md = generate_markdown_report(results, output_path=args.output)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(report_md)

    print(f"\n[SUCCESS] Benchmarking complete. Report generated at: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
