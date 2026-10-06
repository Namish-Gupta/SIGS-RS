"""
Auth token endpoint.

SRS REQ-SE-2: issue JWT tokens for authenticated management API access.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from sigs_rs.auth.jwt_handler import create_access_token, verify_password
from sigs_rs.config import Settings, get_settings
from sigs_rs.schemas import TokenRequest, TokenResponse

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/token", response_model=TokenResponse, summary="Issue a JWT access token")
async def issue_token(
    body: TokenRequest,
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    """
    Authenticate with username/password and receive a JWT Bearer token.

    In production, this is replaced by a real OAuth 2.0 IdP (SRS TBD-3).
    For local dev, credentials are validated against the seeded admin user.
    """
    # Simple seeded-user check (Phase 6 adds a full user store)
    if body.username != settings.admin_username or not verify_password(
        body.password, _get_admin_hash(settings)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
        )

    token = create_access_token(
        sub=body.username,
        role=settings.admin_role,
        secret=settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
        expires_minutes=settings.jwt_access_token_expire_minutes,
    )
    return TokenResponse(
        access_token=token,
        expires_in=settings.jwt_access_token_expire_minutes * 60,
    )


def _get_admin_hash(settings: Settings) -> str:
    """
    Return a bcrypt hash of the configured admin password.
    Computed once and cached on the module for the session.
    """
    global _cached_hash, _cached_plain
    if _cached_plain != settings.admin_password:
        from sigs_rs.auth.jwt_handler import hash_password
        _cached_hash = hash_password(settings.admin_password)
        _cached_plain = settings.admin_password
    return _cached_hash


_cached_hash: str = ""
_cached_plain: str = ""
