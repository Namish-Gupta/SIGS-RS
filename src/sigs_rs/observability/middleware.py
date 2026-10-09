"""
HTTP Observability Middleware for FastAPI.

- Extracts or generates correlation IDs (X-Correlation-ID / X-Request-ID).
- Propagates correlation ID into response headers.
- Automatically instruments structured request access logging.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Callable
import uuid

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from sigs_rs.observability.logging import (
    get_correlation_id,
    set_correlation_id,
)

logger = logging.getLogger("sigs_rs.access")


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """
    Middleware that manages X-Correlation-ID and logs incoming HTTP requests
    in structured JSON without sensitive credentials or payloads.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Extract or generate correlation ID
        correlation_id = request.headers.get("X-Correlation-ID") or request.headers.get("X-Request-ID")
        if not correlation_id or len(correlation_id) > 64 or not re.fullmatch(r'^[A-Za-z0-9_-]+$', correlation_id):
            correlation_id = f"req-{uuid.uuid4().hex[:12]}"

        token = set_correlation_id(correlation_id)

        start_time = time.monotonic()
        client_host = request.client.host if request.client else "unknown"

        try:
            response: Response = await call_next(request)
            duration_ms = round((time.monotonic() - start_time) * 1000.0, 2)

            # Inject correlation ID header into HTTP response
            response.headers["X-Correlation-ID"] = correlation_id

            # Skip spamming access logs on Prometheus scraping / health checks if desired,
            # but keep structured log with clean info
            logger.info(
                "HTTP %s %s status=%d duration=%.2fms client=%s",
                request.method,
                request.url.path,
                response.status_code,
                duration_ms,
                client_host,
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": duration_ms,
                    "client_ip": client_host,
                },
            )
            return response

        except Exception as exc:
            duration_ms = round((time.monotonic() - start_time) * 1000.0, 2)
            logger.error(
                "HTTP %s %s error=%s duration=%.2fms",
                request.method,
                request.url.path,
                str(exc),
                duration_ms,
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "error": str(exc),
                    "duration_ms": duration_ms,
                },
                exc_info=True,
            )
            raise
        finally:
            from sigs_rs.observability.logging import _correlation_id_ctx
            _correlation_id_ctx.reset(token)
