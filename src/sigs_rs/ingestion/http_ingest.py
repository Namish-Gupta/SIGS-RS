"""
HTTP telemetry ingestion endpoints.

SRS §4.1: the system shall accept telemetry via MQTT and HTTP REST.
These FastAPI routes form the HTTP side of the ingestion layer.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request, status

from sigs_rs.common.exceptions import (
    MalformedPayloadError,
    PayloadTooLargeError,
    QueueFullError,
)
from sigs_rs.config import Settings, get_settings
from sigs_rs.schemas import (
    BatchIngestRequest,
    BatchIngestResponse,
    IngestResponse,
    TelemetryPayload,
)

router = APIRouter(prefix="/api/v1/telemetry", tags=["telemetry"])


async def _get_queue(request: Request):
    """Dependency that retrieves the shared IngestionQueue from app state."""
    return request.app.state.queue


@router.post(
    "",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest a single telemetry packet",
)
async def ingest_packet(
    request: Request,
    payload: TelemetryPayload,
    settings: Settings = Depends(get_settings),
    queue=Depends(_get_queue),
) -> IngestResponse:
    """
    Accept a single JSON telemetry packet and enqueue it for processing.

    - Validates Content-Length against the 512 KB limit (SRS REQ-TI-3).
    - Validates JSON schema via Pydantic (SRS REQ-TI-2).
    - Enqueues for Anomalous Packet Inspector (SRS REQ-TI-4).
    """
    # Size check (Content-Length header and body bytes)
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > settings.max_payload_size_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Payload exceeds {settings.max_payload_size_bytes} bytes limit.",
        )
    body_bytes = await request.body()
    if len(body_bytes) > settings.max_payload_size_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Payload exceeds {settings.max_payload_size_bytes} bytes limit.",
        )

    try:
        await queue.put(payload)
    except QueueFullError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ingestion queue is full. Please retry later.",
        )

    return IngestResponse(accepted=True, message="Packet accepted for processing.")


@router.post(
    "/batch",
    response_model=BatchIngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest a batch of telemetry packets",
)
async def ingest_batch(
    request: Request,
    body: BatchIngestRequest,
    settings: Settings = Depends(get_settings),
    queue=Depends(_get_queue),
) -> BatchIngestResponse:
    """Accept multiple telemetry packets in one request."""
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > settings.max_payload_size_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Payload exceeds {settings.max_payload_size_bytes} bytes limit.",
        )
    body_bytes = await request.body()
    if len(body_bytes) > settings.max_payload_size_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Payload exceeds {settings.max_payload_size_bytes} bytes limit.",
        )

    accepted = 0
    rejected = 0

    for packet in body.packets:
        try:
            await queue.put(packet)
            accepted += 1
        except QueueFullError:
            rejected += 1

    return BatchIngestResponse(
        accepted=accepted,
        rejected=rejected,
        message=f"Processed batch: {accepted} accepted, {rejected} rejected.",
    )
