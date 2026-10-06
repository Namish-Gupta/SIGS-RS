"""
Unit tests for DatabasePool and asyncpg connection pooling.

Verifies:
- Pool lifecycle (connect, disconnect, is_connected)
- Parameter delegation (dsn normalization, min_size, max_size, ssl)
- Error handling (wrapping asyncpg errors in PersistenceError)
- Health check ping (SELECT 1)
- Atomic transaction context management
- Schema initialization execution
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from sigs_rs.common.exceptions import PersistenceError
from sigs_rs.persistence.database import (
    DatabasePool,
    create_ssl_context,
    normalize_dsn,
)


class TestDatabasePoolUnit:

    def test_init_parameters(self):
        pool = DatabasePool(
            database_url="postgresql+asyncpg://user:pass@localhost:5432/mydb",
            ssl_mode="verify-full",
            min_size=10,
            max_size=30,
        )
        assert pool.url == "postgresql+asyncpg://user:pass@localhost:5432/mydb"
        assert pool.ssl_mode == "verify-full"
        assert pool.min_size == 10
        assert pool.max_size == 30
        assert not pool.is_connected

    @pytest.mark.asyncio
    async def test_connect_creates_asyncpg_pool(self):
        mock_pool = MagicMock()
        with patch("asyncpg.create_pool", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = mock_pool

            pool = DatabasePool(
                database_url="postgresql+asyncpg://user:pass@localhost:5432/mydb",
                ssl_mode="disable",
                min_size=5,
                max_size=20,
            )
            connected = await pool.connect()
            assert connected is True
            assert pool.is_connected

            mock_create.assert_awaited_once_with(
                dsn="postgresql://user:pass@localhost:5432/mydb",
                min_size=5,
                max_size=20,
                ssl=False,
            )

    @pytest.mark.asyncio
    async def test_connect_failure_graceful_handling(self):
        with patch("asyncpg.create_pool", new_callable=AsyncMock) as mock_create:
            mock_create.side_effect = ConnectionRefusedError("Database unreachable")

            pool = DatabasePool("postgresql://user:pass@localhost:5432/mydb")
            connected = await pool.connect(raise_on_error=False)
            assert connected is False
            assert not pool.is_connected

    @pytest.mark.asyncio
    async def test_connect_failure_raises_persistence_error(self):
        with patch("asyncpg.create_pool", new_callable=AsyncMock) as mock_create:
            mock_create.side_effect = ConnectionRefusedError("Database unreachable")

            pool = DatabasePool("postgresql://user:pass@localhost:5432/mydb")
            with pytest.raises(PersistenceError, match="Database connection failed"):
                await pool.connect(raise_on_error=True)

    @pytest.mark.asyncio
    async def test_disconnect_closes_pool(self):
        mock_pool = MagicMock()
        mock_pool.close = AsyncMock()

        with patch("asyncpg.create_pool", new_callable=AsyncMock, return_value=mock_pool):
            pool = DatabasePool("postgresql://user:pass@localhost:5432/mydb")
            await pool.connect()
            assert pool.is_connected

            await pool.disconnect()
            assert not pool.is_connected
            mock_pool.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_check_health_success(self):
        mock_conn = MagicMock()
        mock_conn.fetchval = AsyncMock(return_value=1)

        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

        with patch("asyncpg.create_pool", new_callable=AsyncMock, return_value=mock_pool):
            pool = DatabasePool("postgresql://user:pass@localhost:5432/mydb")
            await pool.connect()

            assert await pool.check_health() is True
            mock_conn.fetchval.assert_awaited_once_with("SELECT 1;")

    @pytest.mark.asyncio
    async def test_check_health_failure(self):
        mock_conn = MagicMock()
        mock_conn.fetchval = AsyncMock(side_effect=TimeoutError("DB query timed out"))

        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

        with patch("asyncpg.create_pool", new_callable=AsyncMock, return_value=mock_pool):
            pool = DatabasePool("postgresql://user:pass@localhost:5432/mydb")
            await pool.connect()

            assert await pool.check_health() is False

    @pytest.mark.asyncio
    async def test_check_health_when_disconnected(self):
        pool = DatabasePool("postgresql://user:pass@localhost:5432/mydb")
        assert await pool.check_health() is False

    @pytest.mark.asyncio
    async def test_execute_and_fetch_delegation(self):
        mock_pool = MagicMock()
        mock_pool.execute = AsyncMock(return_value="INSERT 0 1")
        mock_pool.fetch = AsyncMock(return_value=[{"id": 1}])
        mock_pool.fetchrow = AsyncMock(return_value={"id": 1})
        mock_pool.fetchval = AsyncMock(return_value=42)

        with patch("asyncpg.create_pool", new_callable=AsyncMock, return_value=mock_pool):
            pool = DatabasePool("postgresql://user:pass@localhost:5432/mydb")
            await pool.connect()

            assert await pool.execute("INSERT INTO test VALUES ($1);", 1) == "INSERT 0 1"
            assert await pool.fetch("SELECT * FROM test;") == [{"id": 1}]
            assert await pool.fetchrow("SELECT * FROM test WHERE id = $1;", 1) == {"id": 1}
            assert await pool.fetchval("SELECT count(*) FROM test;") == 42

    @pytest.mark.asyncio
    async def test_operations_raise_persistence_error_on_failure(self):
        mock_pool = MagicMock()
        mock_pool.execute = AsyncMock(side_effect=Exception("Disk full"))

        with patch("asyncpg.create_pool", new_callable=AsyncMock, return_value=mock_pool):
            pool = DatabasePool("postgresql://user:pass@localhost:5432/mydb")
            await pool.connect()

            with pytest.raises(PersistenceError, match="Database execute error: Disk full"):
                await pool.execute("INSERT INTO test VALUES ($1);", 1)

    @pytest.mark.asyncio
    async def test_operations_when_not_connected_raise_error(self):
        pool = DatabasePool("postgresql://user:pass@localhost:5432/mydb")
        with pytest.raises(PersistenceError, match="Database pool is not connected"):
            await pool.execute("SELECT 1;")

    @pytest.mark.asyncio
    async def test_transaction_context_manager(self):
        mock_tx = MagicMock()
        mock_tx.__aenter__ = AsyncMock()
        mock_tx.__aexit__ = AsyncMock()

        mock_conn = MagicMock()
        mock_conn.transaction.return_value = mock_tx

        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

        with patch("asyncpg.create_pool", new_callable=AsyncMock, return_value=mock_pool):
            pool = DatabasePool("postgresql://user:pass@localhost:5432/mydb")
            await pool.connect()

            async with pool.transaction() as conn:
                assert conn is mock_conn

            mock_tx.__aenter__.assert_awaited_once()
            mock_tx.__aexit__.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_init_schema_executes_ddl(self, tmp_path):
        schema_file = tmp_path / "schema.sql"
        schema_file.write_text("CREATE TABLE test_tbl (id int);", encoding="utf-8")

        mock_conn = MagicMock()
        mock_conn.execute = AsyncMock()

        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

        with patch("asyncpg.create_pool", new_callable=AsyncMock, return_value=mock_pool):
            pool = DatabasePool("postgresql://user:pass@localhost:5432/mydb")
            await pool.connect()

            await pool.init_schema(schema_file=schema_file)
            mock_conn.execute.assert_awaited_once_with("CREATE TABLE test_tbl (id int);")
