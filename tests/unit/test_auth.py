"""Unit tests for JWT handler and role utilities."""
from __future__ import annotations

import time

import pytest

from sigs_rs.auth.jwt_handler import (
    create_access_token,
    decode_token,
    hash_password,
    verify_password,
)
from sigs_rs.auth.roles import is_security_engineer, require_role
from sigs_rs.common.constants import Role
from sigs_rs.common.exceptions import InsufficientRoleError, InvalidTokenError
from sigs_rs.schemas import UserInfo

SECRET = "test-secret-key-for-jwt-tests"
ALGO = "HS256"


class TestPasswordHashing:
    def test_hash_and_verify(self):
        hashed = hash_password("mypassword")
        assert verify_password("mypassword", hashed) is True

    def test_wrong_password(self):
        hashed = hash_password("correct")
        assert verify_password("wrong", hashed) is False

    def test_hash_is_not_plaintext(self):
        hashed = hash_password("secret")
        assert hashed != "secret"


class TestJWTHandler:
    def test_create_and_decode(self):
        token = create_access_token("alice", Role.SECURITY_ENGINEER, SECRET, ALGO, 60)
        user = decode_token(token, SECRET, ALGO)
        assert user.sub == "alice"
        assert user.role == Role.SECURITY_ENGINEER

    def test_wrong_secret_raises(self):
        token = create_access_token("alice", Role.DEVELOPER, SECRET, ALGO, 60)
        with pytest.raises(InvalidTokenError):
            decode_token(token, "wrong-secret", ALGO)

    def test_missing_claims_raises(self):
        """A token with no sub/role claims is rejected."""
        from jose import jwt
        bad_token = jwt.encode({"foo": "bar"}, SECRET, algorithm=ALGO)
        with pytest.raises(InvalidTokenError, match="missing"):
            decode_token(bad_token, SECRET, ALGO)

    def test_expired_token_raises(self):
        token = create_access_token("alice", Role.QA_TESTER, SECRET, ALGO, expires_minutes=0)
        # Token with 0-minute expiry should be expired (or just about to expire)
        # Sleep 1 second to ensure expiry
        time.sleep(1)
        with pytest.raises(InvalidTokenError):
            decode_token(token, SECRET, ALGO)


class TestRoles:
    def test_require_role_passes(self):
        user = UserInfo(sub="alice", role=Role.SECURITY_ENGINEER)
        require_role(user, Role.SECURITY_ENGINEER)  # should not raise

    def test_require_role_raises(self):
        user = UserInfo(sub="bob", role=Role.QA_TESTER)
        with pytest.raises(InsufficientRoleError):
            require_role(user, Role.SECURITY_ENGINEER)

    def test_is_security_engineer(self):
        u = UserInfo(sub="alice", role=Role.SECURITY_ENGINEER)
        assert is_security_engineer(u) is True

    def test_is_not_security_engineer(self):
        u = UserInfo(sub="bob", role=Role.QA_TESTER)
        assert is_security_engineer(u) is False
