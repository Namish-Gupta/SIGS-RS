"""
SQLAlchemy ORM models for SIGS-RS persistent storage.

SRS REQ-PA-3: schema compatible with PostgreSQL/TimescaleDB analytical engines.
Security constraint: RAW telemetry containing PII must NEVER be persisted.
The SanitizedReading table has NO columns for latitude, longitude, mac_address,
device_id, or geo.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    Index,
    PrimaryKeyConstraint,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all persistence models."""
    pass


class SanitizedReading(Base):
    """
    Sanitized telemetry readings persisted to TimescaleDB.

    This table is converted to a TimescaleDB hypertable partitioned by `ts`.
    It contains ONLY sanitized measurements; all PII fields (latitude,
    longitude, mac_address, device_id, geo) are strictly omitted.
    """
    __tablename__ = "sanitized_readings"
    __table_args__ = (
        Index("idx_sanitized_sensor_ts", "sensor_id", "ts"),
        Index("idx_sanitized_resource_ts", "resource_type", "ts"),
        {"schema": "telemetry"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True, nullable=False, index=True)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    sensor_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)
    water_ext: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    energy_ext: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    air_quality_ext: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class SecurityAuditLog(Base):
    """
    Immutable security audit trail (SRS REQ-SE-4).
    Records dropped packets, unauthorized access attempts, and configuration changes.
    """
    __tablename__ = "security_log"
    __table_args__ = (
        Index("idx_audit_ts", "ts"),
        Index("idx_audit_event_type", "event_type"),
        Index("idx_audit_sensor_id", "sensor_id"),
        {"schema": "audit"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="INFO")
    sensor_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)


class MiddlewareSetting(Base):
    """
    Dynamic configuration parameters managed by Security Engineers (SRS BR-1, BR-4).
    """
    __tablename__ = "middleware_settings"
    __table_args__ = {"schema": "config"}

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[str] = mapped_column(String(128), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
