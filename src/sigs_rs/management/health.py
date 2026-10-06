"""
Health and metrics endpoints.

SRS §3.3: expose /health and /metrics for Kubernetes probes and monitoring.
SRS REQ-QA-1: liveness probe used to trigger K8s self-healing restart.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse

from sigs_rs.observability.metrics import get_metrics
from sigs_rs.schemas import HealthResponse

router = APIRouter(tags=["health"])

_start_time = time.monotonic()


@router.get(
    "/health",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    responses={503: {"model": HealthResponse, "description": "Unhealthy or fail-closed state"}},
    summary="Liveness probe",
)
async def health(request: Request, response: Response) -> HealthResponse:
    """
    Returns the liveness status of the gateway.

    - 'ok'       → all components healthy (HTTP 200).
    - 'degraded' → circuit breaker open or partial failure (HTTP 200).
    - 'unhealthy'→ fail-closed state (HTTP 503); K8s should restart the pod.

    SRS REQ-QA-1: K8s restart < 30 s on failure.
    """
    app = request.app
    fail_closed: bool = getattr(app.state, "fail_closed", False)
    cb_open: bool = False

    # Check circuit breaker state if available
    cb = getattr(app.state, "circuit_breaker", None)
    if cb is not None:
        cb_open = cb.is_open

    if fail_closed:
        status_val = "unhealthy"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif cb_open:
        status_val = "degraded"
    else:
        status_val = "ok"

    return HealthResponse(
        status=status_val,
        version="1.0.0",
        environment=getattr(app.state, "environment", "development"),
        components={
            "pipeline": "fail_closed" if fail_closed else "ok",
            "circuit_breaker": "open" if cb_open else "closed",
            "database": "disconnected" if not _db_connected(app) else "connected",
            "mqtt": "disconnected" if not _mqtt_connected(app) else "connected",
        },
    )


@router.get(
    "/ready",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    responses={503: {"model": HealthResponse, "description": "Gateway not ready"}},
    summary="Readiness probe",
)
async def readiness(request: Request, response: Response) -> HealthResponse:
    """
    Returns readiness status.
    The app is 'ready' when the components are healthy and accepting traffic.
    """
    res = await health(request, response)
    if res.status == "unhealthy":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return res


@router.get("/metrics", summary="Runtime metrics")
async def metrics(request: Request) -> Response:
    """
    Expose runtime metrics for Prometheus scraping / K8s HPA or JSON clients.

    SRS §3.3: Expose Prometheus-compatible metrics through GET /metrics.
    - Returns Prometheus text exposition format (v0.0.4) by default or when
      requested via Accept: text/plain, openmetrics, or ?format=prometheus.
    - Returns JSON structure when requested via Accept: application/json or ?format=json.
    """
    app = request.app
    collector = getattr(app.state, "metrics", None) or get_metrics()

    accept = request.headers.get("accept", "")
    req_format = request.query_params.get("format", "").lower()

    if "application/json" in accept or req_format == "json":
        data = collector.to_dict(app_state=getattr(app, "state", None))
        return JSONResponse(content=data)

    prom_text = collector.to_prometheus_text(app_state=getattr(app, "state", None))
    return Response(
        content=prom_text,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _db_connected(app) -> bool:
    db = getattr(app.state, "db", None)
    return db is not None and getattr(db, "is_connected", False)


def _mqtt_connected(app) -> bool:
    mqtt = getattr(app.state, "mqtt", None)
    if mqtt is None:
        return False
    return getattr(mqtt, "is_connected", False) or getattr(mqtt, "_running", False)
