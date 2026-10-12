"""
Pydantic schemas for API requests/responses and internal data transfer objects.

Telemetry domain models (RawTelemetryPacket, SanitizedTelemetryPacket, and
their sub-models) live in sigs_rs.telemetry.models and are re-exported here
as their legacy names for backward compatibility with all existing imports.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# ── Re-export telemetry domain types (backward-compat aliases) ────────────────
from sigs_rs.telemetry.models import (  # noqa: F401  (re-exported)
    AirQualityExtended,
    EnergyExtended,
    GeoLocation,
    RawTelemetryPacket,
    RawTelemetryPacket as TelemetryPayload,       # legacy alias
    SanitizedTelemetryPacket,
    SanitizedTelemetryPacket as SanitizedTelemetry,  # legacy alias
    SensorMetadata,
    WaterExtended,
)


# ─── Ingestion API ────────────────────────────────────────────────────────────

class IngestResponse(BaseModel):
    """Response body for POST /api/v1/telemetry."""
    accepted: bool
    message: str


class BatchIngestRequest(BaseModel):
    """Request body for POST /api/v1/telemetry/batch."""
    packets: list[TelemetryPayload] = Field(
        ..., min_length=1, max_length=1000
    )


class BatchIngestResponse(BaseModel):
    accepted: int
    rejected: int
    message: str



# ─── Config API ───────────────────────────────────────────────────────────────

class InspectorConfig(BaseModel):
    """Current Anomalous Packet Inspector threshold settings."""
    window_seconds: int = Field(..., ge=1, le=300)
    max_packets_per_window: int = Field(..., ge=1, le=10_000)
    cleanup_interval: int = Field(..., ge=10, le=3600)


class AnonymizerConfig(BaseModel):
    """Current Privacy Anonymizer PII field list."""
    pii_fields: list[str] = Field(..., min_length=1)

    @field_validator("pii_fields")
    @classmethod
    def _validate_fields(cls, v: list[str]) -> list[str]:
        cleaned = [f.strip() for f in v if isinstance(f, str) and f.strip()]
        if not cleaned or len(cleaned) != len(v):
            raise ValueError("All pii_fields must be non-empty strings.")
        return cleaned


class ConfigUpdateResponse(BaseModel):
    updated: bool
    message: str
    updated_by: str | None = None
    updated_at: datetime | None = None


class ComplianceGuidelineRevisionRequest(BaseModel):
    """Compliance guideline revision request (SRS BR-3)."""
    guideline_version: str = Field(..., min_length=1, max_length=32, description="Revised guideline version.")
    revision_reason: str = Field(..., min_length=1, max_length=1000, description="Reason for revising guidelines.")


class ComplianceAuditRequest(BaseModel):
    """Compliance audit signoff request (SRS BR-3)."""
    guideline_version: str = Field(..., min_length=1, description="Audited guidelines version.")
    audit_notes: str = Field(..., min_length=1, description="Audit verification rationale.")


class ComplianceStatusResponse(BaseModel):
    """Current compliance guideline and lockout status (SRS REQ-PA-2, BR-3)."""
    guideline_version: str
    lockout_active: bool
    audited: bool
    message: str


# ─── Audit Log API ────────────────────────────────────────────────────────────

class AuditLogEntry(BaseModel):
    """Represents a single audit log record."""
    id: int
    ts: datetime | str
    event_type: str
    severity: str
    sensor_id: str | None = None
    user_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    ip_address: str | None = None


class AuditLogResponse(BaseModel):
    entries: list[AuditLogEntry]
    total: int
    page: int
    page_size: int


# ─── Auth ─────────────────────────────────────────────────────────────────────

class TokenRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserInfo(BaseModel):
    """Decoded JWT payload — attached to request state by auth middleware."""
    sub: str        # enterprise identity (username / email)
    role: str


# ─── Health / Metrics ─────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "unhealthy"]
    version: str
    environment: str
    components: dict[str, str]


class MetricsResponse(BaseModel):
    # Production-style Phase 10 metrics
    packets_received_total: int = 0
    packets_valid_total: int = 0
    packets_malformed_total: int = 0
    packets_dropped_rate_total: int = 0
    packets_dropped_invalid_metrics_total: int = 0
    packets_dropped_anonymization_total: int = 0
    packets_persisted_total: int = 0
    current_queue_size: int = 0
    processing_latency: dict[str, float] = Field(default_factory=dict)
    inspector_latency: dict[str, float] = Field(default_factory=dict)
    anonymizer_latency: dict[str, float] = Field(default_factory=dict)
    database_latency: dict[str, float] = Field(default_factory=dict)
    mqtt_connection_state: int = 0
    active_sensors: int = 0

    # Backward-compatible fields
    packets_ingested_total: int = 0
    packets_dropped_total: int = 0
    packets_sanitized_total: int = 0
    queue_depth: int = 0
    circuit_breaker_state: str = "CLOSED"
    uptime_seconds: float = 0.0


class MiddlewareStatusResponse(BaseModel):
    """Component health and operational metrics."""
    environment: str
    inspector: dict[str, Any]
    anonymizer: dict[str, Any]
    circuit_breaker: dict[str, Any]
    pipeline: dict[str, Any]
    queue: dict[str, Any]
