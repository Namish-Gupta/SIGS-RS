"""
Configuration management API.

SRS BR-1: only Security Engineers may modify thresholds or PII rules.
SRS BR-4: all changes are audit-logged with user identity.
SRS REQ-SE-4: immutable audit logging of configuration modifications.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status

from sigs_rs.auth.dependencies import require_read_access, require_security_engineer
from sigs_rs.management.config_service import ConfigService
from sigs_rs.schemas import (
    AnonymizerConfig,
    ConfigUpdateResponse,
    InspectorConfig,
    MiddlewareStatusResponse,
    UserInfo,
)

router = APIRouter(prefix="/api/v1/config", tags=["config"])


@router.get(
    "/inspector",
    response_model=InspectorConfig,
    status_code=status.HTTP_200_OK,
    summary="Get Inspector thresholds",
    description="Retrieve current sliding-window and rate-limiting thresholds. Accessible by Security Engineers, QA Testers, and Developers.",
)
async def get_inspector_config(
    request: Request,
    user: UserInfo = Depends(require_read_access),
) -> InspectorConfig:
    """Return current sliding-window thresholds."""
    return await ConfigService.get_inspector_config(request)


@router.put(
    "/inspector",
    response_model=ConfigUpdateResponse,
    status_code=status.HTTP_200_OK,
    summary="Update Inspector thresholds",
    description="Update sliding-window thresholds. Restricted strictly to Security Engineers (SRS BR-1).",
)
async def update_inspector_config(
    body: InspectorConfig,
    request: Request,
    user: UserInfo = Depends(require_security_engineer),
) -> ConfigUpdateResponse:
    """Update sliding-window thresholds in-memory and in DB, recording audit log."""
    return await ConfigService.update_inspector_config(request, body, user)


@router.get(
    "/anonymizer",
    response_model=AnonymizerConfig,
    status_code=status.HTTP_200_OK,
    summary="Get Anonymizer PII field list",
    description="Retrieve the configured list of PII fields stripped by the Privacy Anonymizer.",
)
async def get_anonymizer_config(
    request: Request,
    user: UserInfo = Depends(require_read_access),
) -> AnonymizerConfig:
    """Return current PII field list."""
    return await ConfigService.get_anonymizer_config(request)


@router.put(
    "/anonymizer",
    response_model=ConfigUpdateResponse,
    status_code=status.HTTP_200_OK,
    summary="Update Anonymizer PII field list",
    description="Update restricted PII field list. Restricted strictly to Security Engineers (SRS BR-1).",
)
async def update_anonymizer_config(
    body: AnonymizerConfig,
    request: Request,
    user: UserInfo = Depends(require_security_engineer),
) -> ConfigUpdateResponse:
    """Update PII field list in-memory and in DB, recording audit log."""
    return await ConfigService.update_anonymizer_config(request, body, user)


@router.get(
    "/status",
    response_model=MiddlewareStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Get middleware component status",
    description="Retrieve active operational status and statistics for all gateway subsystems.",
)
async def get_middleware_status(
    request: Request,
    user: UserInfo = Depends(require_read_access),
) -> MiddlewareStatusResponse:
    """Return live status of inspector, anonymizer, queue, circuit breaker, and pipeline."""
    return await ConfigService.get_middleware_status(request)
