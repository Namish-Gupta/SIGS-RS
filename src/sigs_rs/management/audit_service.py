"""
Audit Service — business logic for querying and sanitizing security audit logs.

SRS REQ-SE-4: immutable, time-stamped security audit log queries.
Security requirement: no raw PII should be returned unnecessarily.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import Request

from sigs_rs.common.constants import DEFAULT_PII_FIELDS
from sigs_rs.schemas import AuditLogEntry, AuditLogResponse

logger = logging.getLogger(__name__)

FORBIDDEN_PII = frozenset(DEFAULT_PII_FIELDS)


def _sanitize_audit_detail(detail: Any, forbidden_pii: frozenset[str] = FORBIDDEN_PII) -> Any:
    """Recursively strip or mask any accidental PII from audit details before returning via API."""
    if isinstance(detail, dict):
        cleaned: dict[str, Any] = {}
        for k, v in detail.items():
            if k in forbidden_pii:
                cleaned[k] = "[REDACTED]"
            else:
                cleaned[k] = _sanitize_audit_detail(v, forbidden_pii)
        return cleaned
    elif isinstance(detail, list):
        return [_sanitize_audit_detail(item, forbidden_pii) for item in detail]
    return detail


class AuditService:
    """Service providing paginated, filtered, and sanitized audit log queries."""

    @staticmethod
    async def get_logs(
        request: Request,
        page: int = 1,
        page_size: int = 50,
        event_type: str | None = None,
        severity: str | None = None,
        sensor_id: str | None = None,
    ) -> AuditLogResponse:
        """
        Query audit logs from repository or fallback in-memory records.
        Applies filtering, pagination, and PII redaction.
        """
        app = request.app
        repo = getattr(app.state, "repository", None)
        offset = (page - 1) * page_size

        anonymizer = getattr(app.state, "anonymizer", None)
        forbidden = set(DEFAULT_PII_FIELDS)
        if anonymizer and hasattr(anonymizer, "_pii_fields"):
            forbidden.update(anonymizer._pii_fields)
        forbidden_set = frozenset(forbidden)

        entries: list[AuditLogEntry] = []
        total = 0

        if repo is not None and hasattr(repo, "get_audit_logs"):
            try:
                raw_rows = await repo.get_audit_logs(
                    limit=page_size,
                    offset=offset,
                    event_type=event_type,
                    severity=severity,
                    sensor_id=sensor_id,
                )
                total = await repo.count_audit_logs(
                    event_type=event_type,
                    severity=severity,
                    sensor_id=sensor_id,
                )
                for row in raw_rows:
                    cleaned_detail = _sanitize_audit_detail(row.get("detail", {}), forbidden_set)
                    entries.append(
                        AuditLogEntry(
                            id=row.get("id", 0),
                            ts=row.get("ts"),
                            event_type=row.get("event_type", ""),
                            severity=row.get("severity", "INFO"),
                            sensor_id=row.get("sensor_id"),
                            user_id=row.get("user_id"),
                            detail=cleaned_detail,
                            ip_address=row.get("ip_address"),
                        )
                    )
            except Exception as exc:
                logger.warning("Error fetching audit logs from repository: %s", exc)

        return AuditLogResponse(
            entries=entries,
            total=total,
            page=page,
            page_size=page_size,
        )
