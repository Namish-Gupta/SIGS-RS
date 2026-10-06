"""
Role definitions and role-checking helpers.

SRS BR-1: only 'security_engineer' role may modify thresholds/PII rules.
"""
from __future__ import annotations

from sigs_rs.common.constants import Role
from sigs_rs.common.exceptions import InsufficientRoleError
from sigs_rs.schemas import UserInfo


def require_role(user: UserInfo, required: str) -> None:
    """
    Assert that `user` has the required role.

    Raises:
        InsufficientRoleError: if the user's role is insufficient.
    """
    if user.role != required:
        raise InsufficientRoleError(
            f"Role '{required}' required, but user '{user.sub}' has role '{user.role}'."
        )


def is_security_engineer(user: UserInfo) -> bool:
    return user.role == Role.SECURITY_ENGINEER


def is_backend_developer(user: UserInfo) -> bool:
    return user.role == Role.BACKEND_DEVELOPER


def is_qa_tester(user: UserInfo) -> bool:
    return user.role == Role.QA_TESTER


def is_devops(user: UserInfo) -> bool:
    return user.role == Role.DEVOPS
