"""
Progressive workload stage definitions and safety controls.

Stages:
  Stage 0: Smoke (10 sensors, 100 pkts/s)
  Stage 1: Small (100 sensors, 1,000 pkts/s)
  Stage 2: Medium (1,000 sensors, 5,000 pkts/s)
  Stage 3: Advanced (5,000 sensors, 10,000 pkts/s) - Requires explicit opt-in
  Stage 4: Target Max (10,000 sensors, 50,000 pkts/s) - Requires explicit opt-in
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkloadStage:
    stage_id: int
    name: str
    sensor_count: int
    offered_rate_pps: int
    duration_seconds: float
    requires_opt_in: bool = False
    opt_in_flag: str = ""
    description: str = ""


STAGES: dict[int, WorkloadStage] = {
    0: WorkloadStage(
        stage_id=0,
        name="smoke",
        sensor_count=10,
        offered_rate_pps=100,
        duration_seconds=2.0,
        requires_opt_in=False,
        description="Fast smoke verification of benchmark harness mechanics",
    ),
    1: WorkloadStage(
        stage_id=1,
        name="small",
        sensor_count=100,
        offered_rate_pps=1000,
        duration_seconds=3.0,
        requires_opt_in=False,
        description="Small local baseline evaluating latency and throughput",
    ),
    2: WorkloadStage(
        stage_id=2,
        name="medium",
        sensor_count=1000,
        offered_rate_pps=5000,
        duration_seconds=3.0,
        requires_opt_in=False,
        description="Medium workload scaling to 1,000 concurrent sensor IDs",
    ),
    3: WorkloadStage(
        stage_id=3,
        name="advanced",
        sensor_count=5000,
        offered_rate_pps=10000,
        duration_seconds=3.0,
        requires_opt_in=True,
        opt_in_flag="--allow-large-scale",
        description="High-concurrency workload with 5,000 sensors (opt-in required)",
    ),
    4: WorkloadStage(
        stage_id=4,
        name="target_max",
        sensor_count=10000,
        offered_rate_pps=50000,
        duration_seconds=3.0,
        requires_opt_in=True,
        opt_in_flag="--allow-srs-max",
        description="Full SRS target workload: 10,000 sensors / 50,000 pkts/s (opt-in required)",
    ),
}


def get_stage(
    stage_id: int,
    allow_large_scale: bool = False,
    allow_srs_max: bool = False,
) -> WorkloadStage:
    """Retrieve stage and enforce safety opt-in permissions."""
    if stage_id not in STAGES:
        raise ValueError(f"Unknown stage {stage_id}. Available stages: {list(STAGES.keys())}")

    stage = STAGES[stage_id]
    if stage.stage_id == 3 and not allow_large_scale:
        raise PermissionError(
            f"Stage 3 ({stage.name}: {stage.sensor_count} sensors) requires explicit opt-in: {stage.opt_in_flag}. "
            "Host memory/CPU protection prevents automatic execution."
        )
    if stage.stage_id == 4 and not allow_srs_max:
        raise PermissionError(
            f"Stage 4 ({stage.name}: {stage.sensor_count} sensors, {stage.offered_rate_pps} pkts/s) "
            f"requires explicit opt-in: {stage.opt_in_flag}. Host memory protection prevents automatic execution."
        )
    return stage
