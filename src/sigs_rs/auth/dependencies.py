"""
FastAPI dependency injection for authentication and authorization.

SRS REQ-SE-2: unauthenticated requests rejected with HTTP 401.
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from sigs_rs.auth.jwt_handler import decode_token
from sigs_rs.auth.roles import require_role
from sigs_rs.common.constants import Role
from sigs_rs.common.exceptions import AuthError, InsufficientRoleError, InvalidTokenError
from sigs_rs.config import Settings, get_settings
from sigs_rs.schemas import UserInfo

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> UserInfo:
    """
    FastAPI dependency: extract and validate the JWT Bearer token.

    Returns the decoded UserInfo on success.
    Raises HTTP 401 on missing or invalid token.
    """
    if not credentials:
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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_security_engineer(
    user: UserInfo = Depends(get_current_user),
) -> UserInfo:
    """Dependency: assert the caller is a Security Engineer (SRS BR-1)."""
    try:
        require_role(user, Role.SECURITY_ENGINEER)
    except InsufficientRoleError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    return user


def require_read_access(
    user: UserInfo = Depends(get_current_user),
) -> UserInfo:
    """
    Dependency: allows Security Engineer, QA Tester, or Developer to view
    configuration and audit logs (read-only access).
    """
    allowed_roles = {Role.SECURITY_ENGINEER, Role.QA_TESTER, Role.DEVELOPER}
    if user.role not in allowed_roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{user.role}' does not have read access. Allowed: {sorted(allowed_roles)}",
        )
    return user
