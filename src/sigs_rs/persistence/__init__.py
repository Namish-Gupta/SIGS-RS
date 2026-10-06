"""
Persistence package — database pool, models, repository, and circuit breaker.
"""
from __future__ import annotations

from sigs_rs.persistence.circuit_breaker import CBState, CircuitBreaker
from sigs_rs.persistence.database import (
    DatabasePool,
    InMemoryDatabasePool,
    create_ssl_context,
    normalize_dsn,
)
from sigs_rs.persistence.models import (
    Base,
    MiddlewareSetting,
    SanitizedReading,
    SecurityAuditLog,
)
from sigs_rs.persistence.repository import TelemetryRepository

__all__ = [
    "Base",
    "CBState",
    "CircuitBreaker",
    "DatabasePool",
    "InMemoryDatabasePool",
    "MiddlewareSetting",
    "SanitizedReading",
    "SecurityAuditLog",
    "TelemetryRepository",
    "create_ssl_context",
    "normalize_dsn",
]
