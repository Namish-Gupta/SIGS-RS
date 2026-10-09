"""
Shared pytest fixtures for SIGS-RS tests.

Provides:
- settings: isolated test settings (no DB, no MQTT)
- app: FastAPI test application
- client: httpx AsyncClient for API testing
- anonymizer, inspector, sliding_window: middleware test fixtures
"""
from __future__ import annotations

import os
import pytest

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


# ── Force test environment before any imports ────────────────────────────────
os.environ.setdefault("REDIS_URL", "redis://mock")

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/sigs_test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-pytest-only")
os.environ.setdefault("ADMIN_USERNAME", "testadmin")
os.environ.setdefault("ADMIN_PASSWORD", "testpassword")
os.environ.setdefault("MQTT_USE_TLS", "false")
os.environ.setdefault("MQTT_BROKER_PORT", "18883")


@pytest.fixture(scope="session")
def settings():
    """Return isolated test settings (clears lru_cache first)."""
    from sigs_rs.config import get_settings
    get_settings.cache_clear()
    s = get_settings()
    return s


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def app(settings) -> FastAPI:
    """Create a FastAPI test application with all routers."""
    from sigs_rs.main import create_app
    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncClient:
    """
    Async HTTP client that runs the FastAPI lifespan, populating app.state
    (queue, inspector, anonymizer, circuit_breaker, etc).
    """
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as c:
            yield c


# ── Middleware fixtures ───────────────────────────────────────────────────────

@pytest.fixture
def sliding_window():
    from tests.mock_sliding_window import MockAsyncSlidingWindow as SlidingWindow
    return SlidingWindow(window_seconds=10, max_packets=50)


@pytest.fixture
def payload_validator():
    from sigs_rs.middleware.payload_validator import PayloadValidator
    return PayloadValidator()


@pytest.fixture
def anonymizer():
    from sigs_rs.middleware.anonymizer import PrivacyAnonymizer
    return PrivacyAnonymizer()


@pytest.fixture
def inspector(sliding_window, payload_validator):
    from sigs_rs.middleware.inspector import AnomalousPacketInspector
    return AnomalousPacketInspector(sliding_window, payload_validator)


# ── Data fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def valid_payload_dict() -> dict:
    return {
        "sensor_id": "sensor-001",
        "timestamp": "2026-07-17T10:30:00Z",
        "resource_type": "water",
        "value": 42.7,
        "unit": "liters_per_min",
        "metadata": {
            "firmware_version": "2.1.0",
            "battery_level": 87,
            "signal_strength_dbm": -65,
        },
        "water_ext": {
            "pressure_bar": 2.3,
            "ph_level": 7.1,
        },
        "geo": {"latitude": 12.9716, "longitude": 80.2209},
        "mac_address": "AA:BB:CC:DD:EE:FF",
    }


@pytest.fixture
def valid_payload(valid_payload_dict):
    from sigs_rs.telemetry.models import RawTelemetryPacket
    return RawTelemetryPacket(**valid_payload_dict)


@pytest.fixture
def telemetry_validator():
    from sigs_rs.telemetry.validator import TelemetryValidator
    return TelemetryValidator()

@pytest.fixture(autouse=True)
def mock_redis_sliding_window(monkeypatch, request):
    """Replace SlidingWindow methods with in-memory versions for tests."""
    if "real_redis" in request.keywords:
        return
    from sigs_rs.middleware.sliding_window import SlidingWindow
    import collections
    import time

    _windows = collections.defaultdict(collections.deque)

    async def mock_check(self, sensor_id: str, now: float | None = None) -> bool:
        if now is None:
            now = time.time()
        window = _windows[sensor_id]
        cutoff = now - self.window_seconds
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= self.max_packets:
            return False
        window.append(now)
        return True

    async def mock_cleanup(self, now: float | None = None) -> int:
        if now is None:
            now = time.time()
        keys = list(_windows.keys())
        removed = 0
        cutoff = now - self.window_seconds
        for k in keys:
            window = _windows[k]
            while window and window[0] < cutoff:
                window.popleft()
            if not window:
                del _windows[k]
                removed += 1
        return removed

    def mock_sensor_count(self, sensor_id: str) -> int:
        return len(_windows[sensor_id])

    def mock_active_sensor_count(self) -> int:
        return len([s for s, w in _windows.items() if w])

    async def mock_reset(self) -> None:
        _windows.clear()


    def mock_init(self, window_seconds: int, max_packets: int, redis_url: str = None):
        self.window_seconds = window_seconds
        self.max_packets = max_packets
        class DummyRedis:
            async def aclose(self): pass
        self._redis = DummyRedis()

    monkeypatch.setattr(SlidingWindow, "check", mock_check)
    monkeypatch.setattr(SlidingWindow, "__init__", mock_init)
    monkeypatch.setattr(SlidingWindow, "cleanup", mock_cleanup)
    monkeypatch.setattr(SlidingWindow, "reset", mock_reset)
    monkeypatch.setattr(SlidingWindow, "sensor_count", mock_sensor_count, raising=False)
    monkeypatch.setattr(SlidingWindow, "active_sensor_count", mock_active_sensor_count, raising=False)
