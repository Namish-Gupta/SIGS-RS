"""
JWT creation and verification.

SRS REQ-SE-2: HTTP management endpoints authenticated via OAuth 2.0 / JWT.
SRS BR-4: all config changes tied to individual identity in audit log.

Implementation decision: local JWT auth (stand-in for the production OAuth 2.0
IdP, which is TBD-3 in SRS Appendix C).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from jose import JWTError, jwt

from sigs_rs.common.exceptions import InvalidTokenError
from sigs_rs.schemas import UserInfo


def hash_password(plain: str) -> str:
    """Hash a plaintext password using bcrypt."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def create_access_token(
    sub: str,
    role: str,
    secret: str,
    algorithm: str,
    expires_minutes: int,
) -> str:
    """
    Create a signed JWT with `sub` and `role` claims.

    Returns the encoded token string.
    """
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": sub,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=expires_minutes),
    }
    return jwt.encode(payload, secret, algorithm=algorithm)


def decode_token(token: str, secret: str, algorithm: str) -> UserInfo:
    """
    Decode and validate a JWT.

    Raises:
        InvalidTokenError: if the token is missing, expired, or has bad signature.
    """
    try:
        payload = jwt.decode(token, secret, algorithms=[algorithm])
        sub: str = payload.get("sub", "")
        role: str = payload.get("role", "")
        if not sub or not role:
            raise InvalidTokenError("Token missing 'sub' or 'role' claim.")
        return UserInfo(sub=sub, role=role)
    except JWTError as exc:
        raise InvalidTokenError(f"JWT validation failed: {exc}") from exc
