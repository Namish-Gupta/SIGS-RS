"""
Telemetry repository — persistence service for sanitized telemetry into TimescaleDB.

SRS REQ-PA-3: format sanitized data as SQL INSERTs compatible with
              PostgreSQL/TimescaleDB schemas.
Security constraint: RAW telemetry containing PII must NEVER reach this repository.
Only telemetry that has passed Inspector -> Anonymizer may be persisted.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import TYPE_CHECKING, Any

from sigs_rs.common.constants import COMPLIANCE_GUIDELINE_VERSION
from sigs_rs.common.exceptions import PersistenceError
from sigs_rs.telemetry.models import SanitizedTelemetryPacket

if TYPE_CHECKING:
    from sigs_rs.persistence.database import DatabasePool

logger = logging.getLogger(__name__)


class TelemetryRepository:
    """
    INSERT-only persistence service for sanitized telemetry records and audit events.
    """

    RESTRICTED_PII_FIELDS: frozenset[str] = frozenset({
        "latitude",
        "longitude",
        "lat",
        "lon",
        "mac_address",
        "device_id",
        "geo",
    })

    def __init__(self, db: "DatabasePool") -> None:
        self._db = db

    async def probe_connection(self) -> None:
        """
        Execute a lightweight synthetic probe to validate database connectivity.
        Used by the CircuitBreaker during HALF_OPEN recovery to avoid bypassing external telemetry.
        """
        await self._db.execute("SELECT 1")

    async def insert(self, record: SanitizedTelemetryPacket) -> None:
        """
        Persist a single sanitized telemetry record to TimescaleDB.

        Guarantees:
        1. Only SanitizedTelemetryPacket instances are accepted.
        2. Fails closed if any PII field is found anywhere on the record.
        3. Formats parameters and executes parameterised SQL INSERT.

        Raises:
            PersistenceError: on security violation or database error.
        """
        # 1. Type gate: reject anything that is not SanitizedTelemetryPacket
        if not isinstance(record, SanitizedTelemetryPacket):
            raise PersistenceError(
                f"Security violation: Only verified SanitizedTelemetryPacket may be persisted. "
                f"Received: {type(record).__name__}."
            )

        # 2. Strict PII assertion across all attributes and nested dicts
        data = record.model_dump()
        found_pii = self._scan_for_pii(data)
        if found_pii:
            raise PersistenceError(
                f"Security violation: Sanitized record contains restricted PII fields: {sorted(found_pii)}. "
                f"Persistence aborted."
            )

        # 3. Parameterized SQL INSERT into hypertable
        query = """
            INSERT INTO telemetry.sanitized_readings (
                ts, ingested_at, sensor_id, resource_type, value, unit,
                metadata, water_ext, energy_ext, air_quality_ext
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10
            );
        """
        metadata_json = json.dumps(record.metadata) if record.metadata else None
        water_ext_json = json.dumps(record.water_ext) if record.water_ext else None
        energy_ext_json = json.dumps(record.energy_ext) if record.energy_ext else None
        air_ext_json = json.dumps(record.air_quality_ext) if record.air_quality_ext else None

        try:
            await self._db.execute(
                query,
                record.timestamp,
                record.ingested_at,
                record.sensor_id,
                record.resource_type,
                record.value,
                record.unit,
                metadata_json,
                water_ext_json,
                energy_ext_json,
                air_ext_json,
            )
            logger.debug("Persisted sanitized reading for sensor '%s'.", record.sensor_id)
        except Exception as exc:
            if isinstance(exc, PersistenceError):
                raise
            raise PersistenceError(f"Failed to persist sanitized record: {exc}") from exc

    async def insert_batch(self, records: list[SanitizedTelemetryPacket]) -> None:
        """
        Persist multiple sanitized records within an atomic transaction.
        Rolls back the entire batch if any insertion fails.
        """
        if not records:
            return

        async with self._db.transaction():
            for record in records:
                await self.insert(record)

    async def insert_audit(
        self,
        event_type: str,
        severity: str = "INFO",
        sensor_id: str | None = None,
        user_id: str | None = None,
        detail: dict[str, Any] | None = None,
        ip_address: str | None = None,
    ) -> None:
        """
        Persist an audit log entry to audit.security_log.
        """
        query = """
            INSERT INTO audit.security_log (
                ts, event_type, severity, sensor_id, user_id, detail, ip_address
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7
            );
        """
        clean_detail = self._sanitize_audit_detail(detail or {})
        detail_json = json.dumps(clean_detail)
        try:
            await self._db.execute(
                query,
                datetime.now(timezone.utc),
                event_type,
                severity,
                sensor_id,
                user_id,
                detail_json,
                ip_address,
            )
        except Exception as exc:
            if isinstance(exc, PersistenceError):
                raise
            raise PersistenceError(f"Failed to persist audit log: {exc}") from exc

    async def get_by_sensor(
        self,
        sensor_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query sanitized readings for a given sensor (ordered by timestamp DESC)."""
        query = """
            SELECT id, ts, ingested_at, sensor_id, resource_type, value, unit,
                   metadata, water_ext, energy_ext, air_quality_ext
            FROM telemetry.sanitized_readings
            WHERE sensor_id = $1
            ORDER BY ts DESC
            LIMIT $2;
        """
        rows = await self._db.fetch(query, sensor_id, limit)
        return [dict(r) if not isinstance(r, dict) else r for r in rows]

    async def count_readings(self, sensor_id: str | None = None) -> int:
        """Return the count of stored readings (optionally filtered by sensor)."""
        if sensor_id:
            query = "SELECT COUNT(*) FROM telemetry.sanitized_readings WHERE sensor_id = $1;"
            val = await self._db.fetchval(query, sensor_id)
        else:
            query = "SELECT COUNT(*) FROM telemetry.sanitized_readings;"
            val = await self._db.fetchval(query)
        return int(val or 0)

    async def get_audit_logs(
        self,
        limit: int = 100,
        offset: int = 0,
        event_type: str | None = None,
        severity: str | None = None,
        sensor_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query security audit records with optional filtering and pagination."""
        conditions: list[str] = []
        params: list[Any] = []

        if event_type:
            params.append(event_type)
            conditions.append(f"event_type = ${len(params)}")
        if severity:
            params.append(severity)
            conditions.append(f"severity = ${len(params)}")
        if sensor_id:
            params.append(sensor_id)
            conditions.append(f"sensor_id = ${len(params)}")

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        limit_idx = len(params)
        params.append(offset)
        offset_idx = len(params)

        query = f"""
            SELECT id, ts, event_type, severity, sensor_id, user_id, detail, ip_address
            FROM audit.security_log
            {where_clause}
            ORDER BY ts DESC
            LIMIT ${limit_idx} OFFSET ${offset_idx};
        """
        rows = await self._db.fetch(query, *params)
        return [dict(r) if not isinstance(r, dict) else r for r in rows]

    async def count_audit_logs(
        self,
        event_type: str | None = None,
        severity: str | None = None,
        sensor_id: str | None = None,
    ) -> int:
        """Count total security audit records matching filters."""
        conditions: list[str] = []
        params: list[Any] = []

        if event_type:
            params.append(event_type)
            conditions.append(f"event_type = ${len(params)}")
        if severity:
            params.append(severity)
            conditions.append(f"severity = ${len(params)}")
        if sensor_id:
            params.append(sensor_id)
            conditions.append(f"sensor_id = ${len(params)}")

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"SELECT COUNT(*) FROM audit.security_log {where_clause};"
        val = await self._db.fetchval(query, *params)
        return int(val or 0)

    async def get_setting(self, key: str) -> Any | None:
        """Retrieve a configuration value from config.middleware_settings."""
        query = "SELECT value FROM config.middleware_settings WHERE key = $1;"
        val = await self._db.fetchval(query, key)
        if isinstance(val, str):
            try:
                return json.loads(val)
            except Exception:
                return val
        return val

    async def set_setting(self, key: str, value: Any, updated_by: str) -> None:
        """Persist or update a configuration setting in config.middleware_settings."""
        query = """
            INSERT INTO config.middleware_settings (key, value, updated_by, updated_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value,
                updated_by = EXCLUDED.updated_by,
                updated_at = EXCLUDED.updated_at;
        """
        value_json = json.dumps(value) if not isinstance(value, str) else value
        await self._db.execute(query, key, value_json, updated_by, datetime.now(timezone.utc))

    async def signoff_compliance_state(
        self,
        expected_version: str,
        audit_notes: str,
        user_id: str,
    ) -> tuple[bool, dict[str, Any]]:
        """
        Atomically verify guideline version and apply formal audit signoff in database.
        Coordinates across multiple application instances using DB transaction and row-level locking (SRS BR-3).

        Returns (True, new_state_dict) if signoff was successfully applied.
        Returns (False, current_state_dict) if active version conflicted with expected_version.
        """
        select_query = "SELECT value FROM config.middleware_settings WHERE key = $1 FOR UPDATE;"
        upsert_query = """
            INSERT INTO config.middleware_settings (key, value, updated_by, updated_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value,
                updated_by = EXCLUDED.updated_by,
                updated_at = EXCLUDED.updated_at;
        """
        async with self._db.transaction() as conn:
            raw_val = await conn.fetchval(select_query, "compliance.state")
            current_state = {}
            if isinstance(raw_val, str):
                try:
                    current_state = json.loads(raw_val)
                except Exception:
                    current_state = {}
            elif isinstance(raw_val, dict):
                current_state = raw_val

            current_version = current_state.get("guideline_version", COMPLIANCE_GUIDELINE_VERSION)
            if current_version != expected_version:
                return False, current_state

            now = datetime.now(timezone.utc)
            new_state = {
                "guideline_version": expected_version,
                "approved_version": expected_version,
                "lockout_active": False,
                "audited": True,
                "audit_notes": audit_notes,
                "audited_by": user_id,
                "audited_at": now.isoformat(),
            }
            await conn.execute(
                upsert_query,
                "compliance.state",
                json.dumps(new_state),
                user_id,
                now,
            )
            return True, new_state

    async def revise_compliance_state(
        self,
        new_version: str,
        revision_reason: str,
        user_id: str,
    ) -> dict[str, Any]:
        """
        Atomically persist guideline revision and activate compliance lockout in database.
        Coordinates across multiple application instances using DB transaction and row-level locking (SRS BR-3).
        """
        select_query = "SELECT value FROM config.middleware_settings WHERE key = $1 FOR UPDATE;"
        upsert_query = """
            INSERT INTO config.middleware_settings (key, value, updated_by, updated_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value,
                updated_by = EXCLUDED.updated_by,
                updated_at = EXCLUDED.updated_at;
        """
        async with self._db.transaction() as conn:
            await conn.fetchval(select_query, "compliance.state")
            now = datetime.now(timezone.utc)
            state_payload = {
                "guideline_version": new_version,
                "approved_version": None,
                "lockout_active": True,
                "audited": False,
                "revision_reason": revision_reason,
                "revised_by": user_id,
                "revised_at": now.isoformat(),
            }
            await conn.execute(
                upsert_query,
                "compliance.state",
                json.dumps(state_payload),
                user_id,
                now,
            )
            return state_payload

    def _scan_for_pii(self, data: Any) -> list[str]:
        """Recursively check any data structure for restricted PII keys (case-insensitive)."""
        found: list[str] = []
        if isinstance(data, dict):
            for k, v in data.items():
                if k.lower() in self.RESTRICTED_PII_FIELDS:
                    found.append(k)
                found.extend(self._scan_for_pii(v))
        elif isinstance(data, list):
            for item in data:
                found.extend(self._scan_for_pii(item))
        return found

    def _sanitize_audit_detail(self, data: Any) -> Any:
        """Recursively redact any restricted PII keys from audit event details."""
        if isinstance(data, dict):
            clean = {}
            for k, v in data.items():
                if k.lower() in self.RESTRICTED_PII_FIELDS:
                    clean[k] = "[REDACTED_PII]"
                else:
                    clean[k] = self._sanitize_audit_detail(v)
            return clean
        elif isinstance(data, list):
            return [self._sanitize_audit_detail(item) for item in data]
        return data
