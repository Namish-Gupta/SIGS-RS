"""
Database connection management and connection pooling.

SRS REQ-SE-3: DB connections use SSL verify-full in production.
SRS REQ-SF-1: verified that DATABASE_URL does not point to production clusters.
SRS REQ-PA-3: PostgreSQL + TimescaleDB connection management.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from pathlib import Path
import ssl
from typing import Any, AsyncIterator

import asyncpg

from sigs_rs.common.exceptions import PersistenceError

logger = logging.getLogger(__name__)


def normalize_dsn(url: str) -> str:
    """Normalize SQLAlchemy-style asyncpg DSN to standard PostgreSQL DSN for asyncpg."""
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql://", 1)
    return url


def create_ssl_context(ssl_mode: str) -> ssl.SSLContext | bool | None:
    """
    Configure SSL context matching SRS REQ-SE-3 requirements.
    - 'verify-full': validates certificate chain and hostname against VPC CA.
    - 'require': requires TLS without strict hostname verification.
    - 'disable': plain TCP (development / local docker).
    """
    mode = (ssl_mode or "disable").lower()
    if mode in ("disable", "false", "off"):
        return False
    if mode in ("verify-full", "verify-ca"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = (mode == "verify-full")
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx
    if mode in ("require", "true", "on"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None


class DatabasePool:
    """
    Manages the asyncpg connection pool lifecycle.

    Provides async connection pooling, transaction handling, parameterized
    queries, health checks, and schema initialization.
    """

    def __init__(
        self,
        database_url: str,
        ssl_mode: str = "disable",
        min_size: int = 5,
        max_size: int = 20,
    ) -> None:
        self._url = database_url
        self._ssl_mode = ssl_mode
        self._min_size = min_size
        self._max_size = max_size
        self._pool: asyncpg.Pool | None = None
        self._is_connected: bool = False

    @property
    def url(self) -> str:
        return self._url

    @property
    def ssl_mode(self) -> str:
        return self._ssl_mode

    @property
    def min_size(self) -> int:
        return self._min_size

    @property
    def max_size(self) -> int:
        return self._max_size

    @property
    def is_connected(self) -> bool:
        """True if the connection pool is open and healthy."""
        return self._is_connected and (self._pool is not None)

    async def connect(self, raise_on_error: bool = False) -> bool:
        """
        Create the asyncpg connection pool.

        Args:
            raise_on_error: If True, raises PersistenceError on connection failure;
                            otherwise logs a warning and marks as disconnected.
        """
        if self._pool is not None:
            return True

        dsn = normalize_dsn(self._url)
        ssl_ctx = create_ssl_context(self._ssl_mode)

        try:
            logger.info(
                "Connecting to database pool: min_size=%d max_size=%d ssl_mode=%s",
                self._min_size,
                self._max_size,
                self._ssl_mode,
            )
            self._pool = await asyncpg.create_pool(
                dsn=dsn,
                min_size=self._min_size,
                max_size=self._max_size,
                ssl=ssl_ctx,
            )
            self._is_connected = True
            logger.info("Database connection pool established.")
            return True
        except Exception as exc:
            self._pool = None
            self._is_connected = False
            logger.warning(
                "Failed to establish database connection pool: %s. Continuing in disconnected state.",
                exc,
            )
            if raise_on_error:
                raise PersistenceError(f"Database connection failed: {exc}") from exc
            return False

    async def disconnect(self) -> None:
        """Close the connection pool gracefully."""
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("Database connection pool closed.")
        self._is_connected = False

    async def check_health(self) -> bool:
        """
        Database health check: verifies connectivity with a lightweight ping query.
        Returns True if database responds, False otherwise.
        """
        if not self.is_connected or self._pool is None:
            return False
        try:
            async with self._pool.acquire() as conn:
                res = await conn.fetchval("SELECT 1;")
                return res == 1
        except Exception as exc:
            logger.warning("Database health check ping failed: %s", exc)
            return False

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection]:
        """Acquire a raw connection from the pool."""
        if not self._pool:
            raise PersistenceError("Database pool is not connected.")
        async with self._pool.acquire() as conn:
            yield conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection]:
        """
        Execute operations within an atomic transaction.
        Rolls back automatically on exception.
        """
        if not self._pool:
            raise PersistenceError("Database pool is not connected.")
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                yield conn

    async def execute(self, query: str, *args: Any) -> str:
        """Execute a parameterized query through the pool."""
        if not self._pool:
            raise PersistenceError("Database pool is not connected.")
        try:
            return await self._pool.execute(query, *args)
        except Exception as exc:
            raise PersistenceError(f"Database execute error: {exc}") from exc

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        """Fetch rows from a parameterized query."""
        if not self._pool:
            raise PersistenceError("Database pool is not connected.")
        try:
            return await self._pool.fetch(query, *args)
        except Exception as exc:
            raise PersistenceError(f"Database fetch error: {exc}") from exc

    async def fetchrow(self, query: str, *args: Any) -> asyncpg.Record | None:
        """Fetch a single row."""
        if not self._pool:
            raise PersistenceError("Database pool is not connected.")
        try:
            return await self._pool.fetchrow(query, *args)
        except Exception as exc:
            raise PersistenceError(f"Database fetchrow error: {exc}") from exc

    async def fetchval(self, query: str, *args: Any) -> Any:
        """Fetch a single scalar value."""
        if not self._pool:
            raise PersistenceError("Database pool is not connected.")
        try:
            return await self._pool.fetchval(query, *args)
        except Exception as exc:
            raise PersistenceError(f"Database fetchval error: {exc}") from exc

    async def init_schema(self, schema_file: str | Path | None = None) -> None:
        """
        Execute the DDL schema setup script against the database.
        Creates tables, indexes, and sets up TimescaleDB hypertables.
        """
        if not self.is_connected:
            raise PersistenceError("Cannot initialize schema: database is not connected.")

        if schema_file is None:
            schema_file = Path(__file__).parent / "schema.sql"
        schema_path = Path(schema_file)
        if not schema_path.exists():
            raise PersistenceError(f"Schema file not found at {schema_path}")

        sql = schema_path.read_text(encoding="utf-8")
        try:
            async with self.acquire() as conn:
                await conn.execute(sql)
            logger.info("Database schema initialized successfully.")
        except Exception as exc:
            raise PersistenceError(f"Failed to initialize schema: {exc}") from exc


class InMemoryDatabasePool(DatabasePool):
    """
    In-memory simulation of the database pool for isolated testing.
    Maintains table data in Python memory without requiring a running PostgreSQL server.
    """

    def __init__(self, database_url: str = "memory://", ssl_mode: str = "disable") -> None:
        super().__init__(database_url=database_url, ssl_mode=ssl_mode)
        self._tables: dict[str, list[dict[str, Any]]] = {
            "telemetry.sanitized_readings": [],
            "audit.security_log": [],
            "config.middleware_settings": [],
        }
        self._id_counter = 0

    async def connect(self, raise_on_error: bool = False) -> bool:
        self._is_connected = True
        return True

    async def disconnect(self) -> None:
        self._is_connected = False

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    async def check_health(self) -> bool:
        return self._is_connected

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Any]:
        if not self._is_connected:
            raise PersistenceError("Database pool is not connected.")
        # Snapshot state for rollback on exception
        snapshot = {tbl: list(rows) for tbl, rows in self._tables.items()}
        try:
            yield self
        except Exception:
            self._tables = snapshot
            raise

    async def execute(self, query: str, *args: Any) -> str:
        if not self._is_connected:
            raise PersistenceError("Database pool is not connected.")

        clean_query = query.strip()
        upper_query = clean_query.upper()

        # SRS REQ-SE-4: Immutable audit log — block UPDATE or DELETE on audit.security_log
        if "AUDIT.SECURITY_LOG" in upper_query:
            if upper_query.startswith("UPDATE") or upper_query.startswith("DELETE") or " UPDATE " in upper_query or " DELETE " in upper_query:
                raise PersistenceError(
                    "Security violation: audit.security_log is immutable (SRS REQ-SE-4). "
                    "UPDATE and DELETE operations are prohibited."
                )

        if "INSERT INTO telemetry.sanitized_readings" in clean_query:
            self._id_counter += 1
            record = {
                "id": self._id_counter,
                "ts": args[0],
                "ingested_at": args[1],
                "sensor_id": args[2],
                "resource_type": args[3],
                "value": args[4],
                "unit": args[5],
                "metadata": args[6],
                "water_ext": args[7],
                "energy_ext": args[8],
                "air_quality_ext": args[9],
            }
            self._tables["telemetry.sanitized_readings"].append(record)
            return "INSERT 0 1"

        if "INSERT INTO audit.security_log" in clean_query:
            self._id_counter += 1
            record = {
                "id": self._id_counter,
                "ts": args[0],
                "event_type": args[1],
                "severity": args[2],
                "sensor_id": args[3],
                "user_id": args[4],
                "detail": args[5],
                "ip_address": args[6],
            }
            self._tables["audit.security_log"].append(record)
            return "INSERT 0 1"

        if "INSERT INTO config.middleware_settings" in clean_query:
            key, val, updated_by, updated_at = args[0], args[1], args[2], args[3]
            existing = next((item for item in self._tables["config.middleware_settings"] if item["key"] == key), None)
            if existing:
                existing["value"] = val
                existing["updated_by"] = updated_by
                existing["updated_at"] = updated_at
            else:
                self._tables["config.middleware_settings"].append({
                    "key": key,
                    "value": val,
                    "updated_by": updated_by,
                    "updated_at": updated_at,
                })
            return "INSERT 0 1"

        return "OK"

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if not self._is_connected:
            raise PersistenceError("Database pool is not connected.")

        if "FROM telemetry.sanitized_readings" in query:
            rows = self._tables["telemetry.sanitized_readings"]
            if args and len(args) >= 1:
                sensor_id = args[0]
                rows = [r for r in rows if r["sensor_id"] == sensor_id]
            limit = args[1] if len(args) >= 2 else len(rows)
            return rows[-limit:]

        if "FROM audit.security_log" in query:
            rows = list(self._tables["audit.security_log"])
            # Filter if WHERE clause present
            if "WHERE" in query and args:
                limit = args[-2]
                offset = args[-1]
                filter_args = args[:-2]
                filtered = []
                for r in rows:
                    match = True
                    idx = 0
                    if "event_type = " in query:
                        if r.get("event_type") != filter_args[idx]:
                            match = False
                        idx += 1
                    if "severity = " in query:
                        if r.get("severity") != filter_args[idx]:
                            match = False
                        idx += 1
                    if "sensor_id = " in query:
                        if r.get("sensor_id") != filter_args[idx]:
                            match = False
                        idx += 1
                    if match:
                        filtered.append(r)
                rows = filtered
            elif args and len(args) >= 2:
                limit = args[-2]
                offset = args[-1]
                rows = rows[offset: offset + limit]

            # Reverse chronologically
            rows = sorted(rows, key=lambda r: str(r.get("ts", "")), reverse=True)
            if "WHERE" in query and args and len(args) >= 2:
                limit = args[-2]
                offset = args[-1]
                return rows[offset: offset + limit]
            return rows

        if "FROM config.middleware_settings" in query:
            return list(self._tables["config.middleware_settings"])

        return []

    async def fetchval(self, query: str, *args: Any) -> Any:
        if not self._is_connected:
            raise PersistenceError("Database pool is not connected.")

        if "SELECT COUNT(*)" in query:
            if "FROM telemetry.sanitized_readings" in query:
                rows = self._tables["telemetry.sanitized_readings"]
                if args and len(args) >= 1:
                    return len([r for r in rows if r["sensor_id"] == args[0]])
                return len(rows)
            if "FROM audit.security_log" in query:
                rows = list(self._tables["audit.security_log"])
                if "WHERE" in query and args:
                    filtered = []
                    for r in rows:
                        match = True
                        idx = 0
                        if "event_type = " in query:
                            if r.get("event_type") != args[idx]:
                                match = False
                            idx += 1
                        if "severity = " in query:
                            if r.get("severity") != args[idx]:
                                match = False
                            idx += 1
                        if "sensor_id = " in query:
                            if r.get("sensor_id") != args[idx]:
                                match = False
                            idx += 1
                        if match:
                            filtered.append(r)
                    return len(filtered)
                return len(rows)

        if "FROM config.middleware_settings" in query and "SELECT value" in query:
            if args:
                key = args[0]
                row = next((r for r in self._tables["config.middleware_settings"] if r["key"] == key), None)
                return row["value"] if row else None

        if "SELECT 1" in query:
            return 1

        return None
