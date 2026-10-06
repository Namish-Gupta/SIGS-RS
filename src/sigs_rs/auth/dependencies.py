"""
FastAPI dependency injection for authentication and authorization.

SRS REQ-SE-2: unauthenticated requests rejected with HTTP 401.
SRS REQ-SE-4: unauthorized access attempts are audit-logged.
"""
from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from sigs_rs.auth.jwt_handler import decode_token
from sigs_rs.auth.roles import require_role
from sigs_rs.common.constants import AuditEvent, Role
from sigs_rs.common.exceptions import AuthError, InsufficientRoleError, InvalidTokenError
from sigs_rs.config import Settings, get_settings
from sigs_rs.schemas import UserInfo

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)


def _get_client_ip(request: Request) -> str:
    """Extract client IP from request, never raising."""
    try:
        if request.client:
            return request.client.host
    except Exception:
        pass
    return "unknown"


async def _audit_auth_failure(
    request: Request,
    *,
    reason: str,
    user_id: str | None = None,
) -> None:
    """
    Fire-and-forget audit log for an authentication/authorization failure.
    Never logs passwords, JWTs, raw telemetry, coordinates, MACs, or PII.
    Never allows an internal audit-logger error to propagate and break auth.
    """
    try:
        audit_logger = getattr(request.app.state, "audit_logger", None)
        if audit_logger is None:
            return
        await audit_logger.log(
            event_type=AuditEvent.AUTH_FAILURE,
            severity="WARN",
            user_id=user_id,
            ip_address=_get_client_ip(request),
            detail={
                "reason": reason,
                "path": str(request.url.path),
                "method": request.method,
            },
        )
    except Exception:
        # REQ-SE-4 compliance: audit failure must never break authentication
        logger.debug("Audit logging failed for auth event; suppressed.", exc_info=True)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> UserInfo:
    """
    FastAPI dependency: extract and validate the JWT Bearer token.

    Returns the decoded UserInfo on success.
    Raises HTTP 401 on missing or invalid token.
    Audit-logs every failure (SRS REQ-SE-4).
    """
    if not isinstance(settings, Settings):
        settings = get_settings()
    if not credentials:
        await _audit_auth_failure(request, reason="Missing Authorization header")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        user = decode_token(
            credentials.credentials,
            settings.jwt_secret_key,
            settings.jwt_algorithm,
        )
        request.state.user = user
        return user
    except InvalidTokenError as exc:
        # Log the error type but NEVER the token itself
        await _audit_auth_failure(request, reason=f"Invalid token: {exc}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        )


async def require_security_engineer(
    request: Request,
    user: UserInfo = Depends(get_current_user),
) -> UserInfo:
    """Dependency: assert the caller is a Security Engineer (SRS BR-1)."""
    try:
        require_role(user, Role.SECURITY_ENGINEER)
    except InsufficientRoleError as exc:
        await _audit_auth_failure(
            request,
            reason=f"Insufficient role: required={Role.SECURITY_ENGINEER}, actual={user.role}",
            user_id=user.sub,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    return user


async def require_read_access(
    request: Request,
    user: UserInfo = Depends(get_current_user),
) -> UserInfo:
    """
    Dependency: allows Security Engineer, QA Tester, or Developer to view
    configuration and audit logs (read-only access).
    """
    allowed_roles = {Role.SECURITY_ENGINEER, Role.QA_TESTER, Role.DEVELOPER}
    if user.role not in allowed_roles:
        await _audit_auth_failure(
            request,
            reason=f"Role '{user.role}' does not have read access",
            user_id=user.sub,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{user.role}' does not have read access. Allowed: {sorted(allowed_roles)}",
        )
    return user
