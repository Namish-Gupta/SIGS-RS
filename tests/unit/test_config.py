"""Unit tests for application configuration."""
from __future__ import annotations

import os
import pytest

from sigs_rs.common.exceptions import ProductionDatabaseError


class TestConfigValidation:
    def test_production_db_url_rejected(self):
        """DATABASE_URL pointing to production is rejected at startup."""
        from sigs_rs.config import Settings
        with pytest.raises(ProductionDatabaseError):
            Settings(database_url="postgresql+asyncpg://user:pass@production-db:5432/mydb")

    def test_staging_db_url_accepted(self):
        from sigs_rs.config import Settings
        s = Settings(database_url="postgresql+asyncpg://user:pass@staging-db:5432/mydb")
        assert "staging" in s.database_url

    def test_localhost_db_url_accepted(self):
        from sigs_rs.config import Settings
        s = Settings(database_url="postgresql+asyncpg://user:pass@localhost:5432/test")
        assert "localhost" in s.database_url

    def test_pii_fields_parsed(self):
        from sigs_rs.config import Settings
        s = Settings(anonymizer_pii_fields="latitude,longitude,mac_address")
        assert s.pii_fields == ["latitude", "longitude", "mac_address"]

    def test_pii_fields_strips_whitespace(self):
        from sigs_rs.config import Settings
        s = Settings(anonymizer_pii_fields=" latitude , longitude ")
        assert s.pii_fields == ["latitude", "longitude"]
