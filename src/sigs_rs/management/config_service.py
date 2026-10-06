"""
Configuration Service — business logic for dynamic middleware configuration.

SRS BR-1: only Security Engineers may modify thresholds or PII rules.
SRS BR-4: all configuration changes are tied to an enterprise identity in the audit log.
SRS REQ-SE-4: immutable audit logging of configuration modifications.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any

from fastapi import Request

from sigs_rs.common.constants import AuditEvent
from sigs_rs.config import get_settings
from sigs_rs.schemas import (
    AnonymizerConfig,
    ConfigUpdateResponse,
    InspectorConfig,
    MiddlewareStatusResponse,
    UserInfo,
)

logger = logging.getLogger(__name__)


class ConfigService:
    """Service providing configuration retrieval, updates, and status checks."""

    @staticmethod
    async def get_inspector_config(request: Request) -> InspectorConfig:
        """Fetch current Inspector thresholds from active middleware or database."""
        app = request.app
        inspector = getattr(app.state, "inspector", None)
        settings = get_settings()

        if inspector is not None and hasattr(inspector, "_window"):
            window = inspector._window
            window_sec = getattr(window, "window_seconds", settings.inspector_window_seconds)
            max_pkts = getattr(window, "max_packets", settings.inspector_max_packets_per_window)
        else:
            window_sec = settings.inspector_window_seconds
            max_pkts = settings.inspector_max_packets_per_window

        return InspectorConfig(
            window_seconds=window_sec,
            max_packets_per_window=max_pkts,
            cleanup_interval=settings.inspector_window_cleanup_interval,
        )

    @staticmethod
    async def update_inspector_config(
        request: Request,
        body: InspectorConfig,
        user: UserInfo,
    ) -> ConfigUpdateResponse:
        """
        Update Inspector sliding-window thresholds.
        Updates in-memory state, persists to database, and records audit trail.
        """
        app = request.app
        inspector = getattr(app.state, "inspector", None)
        repo = getattr(app.state, "repository", None)
        audit = getattr(app.state, "audit_logger", None)

        # 1. Update in-memory sliding window
        if inspector is not None and hasattr(inspector, "_window"):
            window = inspector._window
            window.window_seconds = body.window_seconds
            window.max_packets = body.max_packets_per_window

        # 2. Persist to database
        if repo is not None and hasattr(repo, "set_setting"):
            try:
                await repo.set_setting(
                    key="inspector.thresholds",
                    value=body.model_dump(),
                    updated_by=user.sub,
                )
            except Exception as exc:
                logger.warning("Failed to persist inspector config to DB: %s", exc)

        # 3. Security audit log (SRS BR-4, REQ-SE-4)
        if audit is not None:
            await audit.log(
                event_type=AuditEvent.CONFIG_CHANGE,
                severity="INFO",
                user_id=user.sub,
                detail={
                    "component": "inspector",
                    "window_seconds": body.window_seconds,
                    "max_packets_per_window": body.max_packets_per_window,
                    "cleanup_interval": body.cleanup_interval,
                },
            )

        now = datetime.now(timezone.utc)
        logger.info(
            "Inspector configuration updated by %s: window=%ds max_pkts=%d",
            user.sub,
            body.window_seconds,
            body.max_packets_per_window,
        )
        return ConfigUpdateResponse(
            updated=True,
            message="Inspector configuration successfully updated.",
            updated_by=user.sub,
            updated_at=now,
        )

    @staticmethod
    async def get_anonymizer_config(request: Request) -> AnonymizerConfig:
        """Fetch current Privacy Anonymizer PII field list."""
        app = request.app
        anonymizer = getattr(app.state, "anonymizer", None)
        settings = get_settings()

        if anonymizer is not None and hasattr(anonymizer, "_pii_fields"):
            pii_list = sorted(list(anonymizer._pii_fields))
        else:
            pii_list = settings.pii_fields

        return AnonymizerConfig(pii_fields=pii_list)

    @staticmethod
    async def update_anonymizer_config(
        request: Request,
        body: AnonymizerConfig,
        user: UserInfo,
    ) -> ConfigUpdateResponse:
        """
        Update Privacy Anonymizer restricted PII field list.
        Updates in-memory state, persists to database, and records audit trail.
        """
        app = request.app
        anonymizer = getattr(app.state, "anonymizer", None)
        repo = getattr(app.state, "repository", None)
        audit = getattr(app.state, "audit_logger", None)

        # 1. Update in-memory anonymizer
        if anonymizer is not None and hasattr(anonymizer, "_pii_fields"):
            anonymizer._pii_fields = set(body.pii_fields)

        # 2. Persist to database
        if repo is not None and hasattr(repo, "set_setting"):
            try:
                await repo.set_setting(
                    key="anonymizer.pii_fields",
                    value=body.pii_fields,
                    updated_by=user.sub,
                )
            except Exception as exc:
                logger.warning("Failed to persist anonymizer config to DB: %s", exc)

        # 3. Security audit log
        if audit is not None:
            await audit.log(
                event_type=AuditEvent.CONFIG_CHANGE,
                severity="INFO",
                user_id=user.sub,
                detail={
                    "component": "anonymizer",
                    "pii_fields": body.pii_fields,
                },
            )

        now = datetime.now(timezone.utc)
        logger.info(
            "Anonymizer PII fields updated by %s: %s",
            user.sub,
            body.pii_fields,
        )
        return ConfigUpdateResponse(
            updated=True,
            message="Anonymizer configuration successfully updated.",
            updated_by=user.sub,
            updated_at=now,
        )

    @staticmethod
    async def get_middleware_status(request: Request) -> MiddlewareStatusResponse:
        """Return live health and statistics for all gateway components."""
        app = request.app
        settings = get_settings()

        inspector = getattr(app.state, "inspector", None)
        anonymizer = getattr(app.state, "anonymizer", None)
        cb = getattr(app.state, "circuit_breaker", None)
        pipeline = getattr(app.state, "pipeline", None)
        queue = getattr(app.state, "queue", None)

        inspector_stats = inspector.stats if inspector else {}
        pipeline_stats = pipeline.stats if pipeline else {}

        return MiddlewareStatusResponse(
            environment=getattr(app.state, "environment", settings.app_env),
            inspector={
                "stats": inspector_stats,
                "window_seconds": getattr(inspector._window, "window_seconds", 10) if inspector else 10,
                "max_packets_per_window": getattr(inspector._window, "max_packets", 50) if inspector else 50,
            },
            anonymizer={
                "configured_pii_fields": sorted(list(getattr(anonymizer, "_pii_fields", []))) if anonymizer else [],
            },
            circuit_breaker={
                "state": cb.state.name if cb else "CLOSED",
                "is_open": cb.is_open if cb else False,
            },
            pipeline={
                "stats": pipeline_stats,
                "fail_closed": getattr(app.state, "fail_closed", False),
            },
            queue={
                "depth": queue.depth if queue else 0,
                "is_full": queue.is_full if queue else False,
            },
        )
