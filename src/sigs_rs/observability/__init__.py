"""
Observability package for SIGS-RS.

Provides:
  - Prometheus-compatible metrics tracking (counters, gauges, latency summaries)
  - Structured JSON logging with PII/sensitive-data redaction
  - Correlation ID tracking across HTTP and background pipelines
"""
from __future__ import annotations

from sigs_rs.observability.logging import (
    configure_logging,
    get_correlation_id,
    get_logger,
    redact_sensitive_data,
    set_correlation_id,
)
from sigs_rs.observability.metrics import MetricsCollector, get_metrics
from sigs_rs.observability.middleware import CorrelationIdMiddleware

__all__ = [
    "CorrelationIdMiddleware",
    "MetricsCollector",
    "configure_logging",
    "get_correlation_id",
    "get_logger",
    "get_metrics",
    "redact_sensitive_data",
    "set_correlation_id",
]
