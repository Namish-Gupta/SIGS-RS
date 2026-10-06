"""
Audit log query API.

SRS REQ-SE-4: audit logs accessible to authorized personnel (Security Engineers & QA).
Security constraint: no raw PII should be returned unnecessarily.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, status

from sigs_rs.auth.dependencies import require_read_access
from sigs_rs.management.audit_service import AuditService
from sigs_rs.schemas import AuditLogResponse, UserInfo

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


@router.get(
    "/logs",
    response_model=AuditLogResponse,
    status_code=status.HTTP_200_OK,
    summary="Query security audit logs",
    description="Retrieve paginated and filtered security audit logs. Accessible by Security Engineers and QA Testers. All raw PII is redacted.",
)
async def get_audit_logs(
    request: Request,
    page: int = Query(default=1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(default=50, ge=1, le=500, description="Items per page"),
    event_type: str | None = Query(default=None, description="Filter by event type"),
    severity: str | None = Query(default=None, description="Filter by severity (INFO, WARN, ERROR, CRITICAL)"),
    sensor_id: str | None = Query(default=None, description="Filter by originating sensor ID"),
    user: UserInfo = Depends(require_read_access),
) -> AuditLogResponse:
    """Return paginated, filtered, and PII-sanitized audit log records."""
    return await AuditService.get_logs(
        request=request,
        page=page,
        page_size=page_size,
        event_type=event_type,
        severity=severity,
        sensor_id=sensor_id,
    )
