"""
Configuration Service — business logic for dynamic middleware configuration.

SRS BR-1: only Security Engineers may modify thresholds or PII rules.
SRS BR-4: all configuration changes are tied to an enterprise identity in the audit log.
SRS REQ-SE-4: immutable audit logging of configuration modifications.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any

from fastapi import HTTPException, Request, status

from sigs_rs.common.constants import AuditEvent, COMPLIANCE_GUIDELINE_VERSION
from sigs_rs.config import get_settings
from sigs_rs.schemas import (
    AnonymizerConfig,
    ComplianceAuditRequest,
    ComplianceGuidelineRevisionRequest,
    ComplianceStatusResponse,
    ConfigUpdateResponse,
    InspectorConfig,
    MiddlewareStatusResponse,
    UserInfo,
)

logger = logging.getLogger(__name__)


class ConfigService:
    """Service providing configuration retrieval, updates, and status checks."""

    _compliance_lock: asyncio.Lock = asyncio.Lock()

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

        # Capture old configuration for audit logging
        old_config = await ConfigService.get_inspector_config(request)
        result_status = "success"

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
                result_status = "partial_failure_db"

        # 3. Security audit log (SRS BR-4, REQ-SE-4)
        if audit is not None:
            await audit.log(
                event_type=AuditEvent.CONFIG_CHANGE,
                severity="INFO",
                user_id=user.sub,
                detail={
                    "role": user.role,
                    "action": "update_inspector_config",
                    "old_configuration": old_config.model_dump(),
                    "new_configuration": body.model_dump(),
                    "result": result_status,
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

        # Capture old configuration for audit logging
        old_config = await ConfigService.get_anonymizer_config(request)
        result_status = "success"

        # 1. Update in-memory anonymizer
        if anonymizer is not None and hasattr(anonymizer, "update_pii_fields"):
            anonymizer.update_pii_fields(body.pii_fields)
        elif anonymizer is not None and hasattr(anonymizer, "_pii_fields"):
            anonymizer._pii_fields = list(body.pii_fields)

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
                result_status = "partial_failure_db"

        # 3. Security audit log
        if audit is not None:
            await audit.log(
                event_type=AuditEvent.CONFIG_CHANGE,
                severity="INFO",
                user_id=user.sub,
                detail={
                    "role": user.role,
                    "action": "update_anonymizer_config",
                    "old_configuration": old_config.model_dump(),
                    "new_configuration": body.model_dump(),
                    "result": result_status,
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

    @staticmethod
    async def get_compliance_status(request: Request) -> ComplianceStatusResponse:
        """Fetch current privacy compliance guideline version and lockout status (SRS BR-3)."""
        app = request.app
        settings = get_settings()
        repo = getattr(app.state, "repository", None)

        # Synchronize with persistent DB state across instances if available
        if repo is not None and hasattr(repo, "get_setting"):
            try:
                db_state = await repo.get_setting("compliance.state")
                if isinstance(db_state, dict):
                    app.state.compliance_guideline_version = db_state.get(
                        "guideline_version",
                        getattr(app.state, "compliance_guideline_version", settings.compliance_guideline_version),
                    )
                    app.state.compliance_lockout = db_state.get("lockout_active", False)
                    app.state.compliance_audited = db_state.get("audited", not app.state.compliance_lockout)
            except Exception as exc:
                logger.critical("Database failure retrieving compliance state: %s. Enforcing fail-closed.", exc)
                app.state.compliance_lockout = True
                app.state.compliance_audited = False
                app.state.fail_closed = True
                return ComplianceStatusResponse(
                    guideline_version=getattr(
                        app.state, "compliance_guideline_version", settings.compliance_guideline_version
                    ),
                    lockout_active=True,
                    audited=False,
                    message="Simulation testing suspended: database failure enforcing fail-closed compliance lockout (SRS BR-3).",
                )

        version = getattr(app.state, "compliance_guideline_version", settings.compliance_guideline_version)
        lockout = getattr(app.state, "compliance_lockout", False)
        audited = getattr(app.state, "compliance_audited", not lockout)
        msg = (
            "Simulation testing suspended: compliance guideline revision pending formal audit (SRS BR-3)."
            if lockout
            else f"Privacy compliance guidelines v{version} active and verified."
        )
        return ComplianceStatusResponse(
            guideline_version=version,
            lockout_active=lockout,
            audited=audited,
            message=msg,
        )

    @staticmethod
    async def revise_compliance_guideline(
        request: Request,
        body: ComplianceGuidelineRevisionRequest,
        user: UserInfo,
    ) -> ConfigUpdateResponse:
        """
        Dynamically revise privacy compliance guidelines.
        Immediately activates BR-3 compliance lockout, fail-closed state,
        persists to DB, and records a critical security audit event.
        """
        app = request.app
        audit = getattr(app.state, "audit_logger", None)
        repo = getattr(app.state, "repository", None)

        async with ConfigService._compliance_lock:
            old_version = getattr(app.state, "compliance_guideline_version", "unknown")
            now = datetime.now(timezone.utc)

            # 1. Update in-memory state immediately (suspend testing immediately)
            app.state.compliance_guideline_version = body.guideline_version
            app.state.compliance_lockout = True
            app.state.compliance_audited = False
            app.state.fail_closed = True

            state_payload = {
                "guideline_version": body.guideline_version,
                "approved_version": None,
                "lockout_active": True,
                "audited": False,
                "revision_reason": body.revision_reason,
                "revised_by": user.sub,
                "revised_at": now.isoformat(),
            }

            # 2. Persist to DB setting atomically with row locking for cross-instance coordination
            if repo is not None:
                if hasattr(repo, "revise_compliance_state"):
                    try:
                        state_payload = await repo.revise_compliance_state(
                            new_version=body.guideline_version,
                            revision_reason=body.revision_reason,
                            user_id=user.sub,
                        )
                    except Exception as exc:
                        logger.critical("Failed to persist compliance lockout to DB: %s. Remaining fail-closed.", exc)
                elif hasattr(repo, "set_setting"):
                    try:
                        await repo.set_setting(
                            key="compliance.state",
                            value=state_payload,
                            updated_by=user.sub,
                        )
                    except Exception as exc:
                        logger.critical("Failed to persist compliance lockout to DB: %s. Remaining fail-closed.", exc)

            # 3. Security audit log
            if audit is not None:
                await audit.log(
                    event_type=AuditEvent.COMPLIANCE_LOCKOUT,
                    severity="CRITICAL",
                    user_id=user.sub,
                    detail={
                        "role": user.role,
                        "action": "revise_compliance_guideline",
                        "previous_guideline_version": old_version,
                        "revised_guideline_version": body.guideline_version,
                        "revision_reason": body.revision_reason,
                    },
                )

            logger.critical(
                "BR-3 Compliance Lockout triggered by %s: guidelines revised from v%s to v%s. Testing suspended.",
                user.sub,
                old_version,
                body.guideline_version,
            )
            return ConfigUpdateResponse(
                updated=True,
                message=f"Guidelines revised to version {body.guideline_version}. Simulation testing suspended pending audit (SRS BR-3).",
                updated_by=user.sub,
                updated_at=now,
            )

    @staticmethod
    async def compliance_audit_signoff(
        request: Request,
        body: ComplianceAuditRequest,
        user: UserInfo,
    ) -> ConfigUpdateResponse:
        """
        Record compliance team formal audit signoff and lift BR-3 compliance lockout.
        Guarantees serialised cross-instance concurrency and conflict prevention (SRS BR-3).
        """
        app = request.app
        audit = getattr(app.state, "audit_logger", None)
        repo = getattr(app.state, "repository", None)

        async with ConfigService._compliance_lock:
            now = datetime.now(timezone.utc)

            # Atomically verify and apply signoff across instances using DB transaction & row-level locking (SRS BR-3)
            if repo is not None and hasattr(repo, "signoff_compliance_state"):
                try:
                    success, db_state = await repo.signoff_compliance_state(
                        expected_version=body.guideline_version,
                        audit_notes=body.audit_notes,
                        user_id=user.sub,
                    )
                except Exception as exc:
                    logger.critical("Database failure verifying compliance state: %s. Enforcing fail-closed.", exc)
                    app.state.compliance_lockout = True
                    app.state.compliance_audited = False
                    app.state.fail_closed = True
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="State store failure: cannot verify compliance state (enforcing fail-closed).",
                    ) from exc

                if not success:
                    active_ver = db_state.get("guideline_version", "unknown")
                    logger.warning(
                        "Signoff conflict: requested version '%s' does not match active version '%s'.",
                        body.guideline_version,
                        active_ver,
                    )
                    app.state.compliance_guideline_version = active_ver
                    app.state.compliance_lockout = True
                    app.state.compliance_audited = False
                    app.state.fail_closed = True
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Cannot sign off guideline version '{body.guideline_version}': active guideline was revised to '{active_ver}'.",
                    )
            else:
                # Fallback for mock environments without signoff_compliance_state
                current_version = getattr(app.state, "compliance_guideline_version", "unknown")
                if body.guideline_version != current_version:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Cannot sign off guideline version '{body.guideline_version}': active guideline was revised to '{current_version}'.",
                    )
                if repo is not None and hasattr(repo, "set_setting"):
                    state_payload = {
                        "guideline_version": body.guideline_version,
                        "approved_version": body.guideline_version,
                        "lockout_active": False,
                        "audited": True,
                        "audit_notes": body.audit_notes,
                        "audited_by": user.sub,
                        "audited_at": now.isoformat(),
                    }
                    try:
                        await repo.set_setting(
                            key="compliance.state",
                            value=state_payload,
                            updated_by=user.sub,
                        )
                    except Exception as exc:
                        logger.critical("Failed to persist compliance signoff to DB: %s. Enforcing fail-closed.", exc)
                        app.state.compliance_lockout = True
                        app.state.compliance_audited = False
                        app.state.fail_closed = True
                        raise HTTPException(
                            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail="Failed to persist compliance signoff to database; fail-closed maintained.",
                        ) from exc

            # Lift local in-memory lockout
            app.state.compliance_guideline_version = body.guideline_version
            app.state.compliance_lockout = False
            app.state.compliance_audited = True

            pipeline = getattr(app.state, "pipeline", None)
            if getattr(app.state, "fail_closed", False) and (pipeline is None or pipeline._running):
                app.state.fail_closed = False

            if audit is not None:
                await audit.log(
                    event_type=AuditEvent.COMPLIANCE_AUDIT_RESUME,
                    severity="INFO",
                    user_id=user.sub,
                    detail={
                        "role": user.role,
                        "action": "compliance_audit_signoff",
                        "approved_guideline_version": body.guideline_version,
                        "audit_notes": body.audit_notes,
                    },
                )

            logger.info(
                "BR-3 Compliance audit signoff completed by %s (%s) for guideline v%s. Testing resumed.",
                user.sub,
                user.role,
                body.guideline_version,
            )
            return ConfigUpdateResponse(
                updated=True,
                message=f"Compliance audit signoff accepted for version {body.guideline_version}. Simulation testing resumed (SRS BR-3).",
                updated_by=user.sub,
                updated_at=now,
            )
