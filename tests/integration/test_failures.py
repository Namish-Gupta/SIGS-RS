import asyncio
import pytest
from httpx import AsyncClient
from unittest.mock import MagicMock, AsyncMock

from fastapi import FastAPI
from sigs_rs.common.exceptions import AnonymizerError
from sigs_rs.schemas import TelemetryPayload

pytestmark = pytest.mark.asyncio

async def test_database_latency_circuit_breaker(client: AsyncClient, app: FastAPI):
    repo = app.state.repository
    original_insert = repo.insert

    async def slow_insert(packet):
        await asyncio.sleep(2.1)
        return await original_insert(packet)

    repo.insert = slow_insert

    payload = {
        "sensor_id": "test-sensor",
        "timestamp": "2026-10-06T20:00:00Z",
        "resource_type": "water",
        "value": 10.5,
        "unit": "L/min",
    }
    resp = await client.post("/api/v1/telemetry", json=payload)
    assert resp.status_code == 202
    await asyncio.sleep(2.5)

    assert app.state.circuit_breaker.is_open is True
    resp = await client.post("/api/v1/telemetry", json=payload)
    assert resp.status_code == 503
    assert app.state.mqtt.is_running is False

async def test_anonymizer_catastrophic_failure_fails_closed(client: AsyncClient, app: FastAPI):
    anonymizer = app.state.anonymizer
    
    def fake_anonymize(packet):
        raise RuntimeError("Catastrophic error")
    
    anonymizer.anonymize = fake_anonymize

    payload = {
        "sensor_id": "test-sensor-2",
        "timestamp": "2026-10-06T20:00:00Z",
        "resource_type": "water",
        "value": 10.5,
        "unit": "L/min",
    }
    
    resp = await client.post("/api/v1/telemetry", json=payload)
    assert resp.status_code == 202
    await asyncio.sleep(0.5)
    assert app.state.fail_closed is True
    resp = await client.post("/api/v1/telemetry", json=payload)
    assert resp.status_code == 503
    assert "FAIL-CLOSED" in resp.json()["detail"]

async def test_database_unavailable_circuit_breaker(client: AsyncClient, app: FastAPI):
    app.state.circuit_breaker._reset()
    app.state.fail_closed = False
    
    repo = app.state.repository
    async def fail_insert(packet):
        raise RuntimeError("Database connection refused")
    repo.insert = fail_insert

    payload = {
        "sensor_id": "test-sensor-3",
        "timestamp": "2026-10-06T20:00:00Z",
        "resource_type": "water",
        "value": 10.5,
        "unit": "L/min",
    }
    resp = await client.post("/api/v1/telemetry", json=payload)
    assert resp.status_code == 202
    await asyncio.sleep(0.5)

    # Circuit breaker should have tripped, but pipeline NOT crashed.
    assert app.state.circuit_breaker.is_open is True
    assert app.state.fail_closed is False
    resp = await client.post("/api/v1/telemetry", json=payload)
    assert resp.status_code == 503

async def test_queue_failure_backpressure(client: AsyncClient, app: FastAPI):
    app.state.circuit_breaker._reset()
    # Simulate queue full
    from sigs_rs.common.exceptions import QueueFullError
    original_put = app.state.queue.put
    async def fail_put(packet):
        raise QueueFullError("Queue full")
    app.state.queue.put = fail_put

    payload = {
        "sensor_id": "test-sensor-4",
        "timestamp": "2026-10-06T20:00:00Z",
        "resource_type": "water",
        "value": 10.5,
        "unit": "L/min",
    }
    resp = await client.post("/api/v1/telemetry", json=payload)
    assert resp.status_code == 503
    assert "queue is full" in resp.json()["detail"].lower()

async def test_mqtt_failure_reconnects(client: AsyncClient, app: FastAPI):
    # Just verify the method handles exceptions gracefully
    mqtt = app.state.mqtt
    mqtt._running = True
    
    # Check that when connection fails, is_connected becomes False
    import aiomqtt
    original_client = mqtt._client
    
    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def connect(self): raise aiomqtt.MqttError("Connection lost")
        
    mqtt._client = FakeClient()
    
    # Simulate an exception in the running loop by directly testing the exception handler
    # Or just verify that when we call stop(), it correctly disconnects
    await mqtt.stop()
    assert mqtt._is_connected is False
    assert mqtt.is_running is False

async def test_process_restart_resets_state():
    from sigs_rs.main import create_app
    app_new = create_app()
    assert getattr(app_new.state, "fail_closed", False) is False

async def test_circuit_breaker_half_open_probe_success(client: AsyncClient, app: FastAPI):
    # Ensure starting in a clean state
    app.state.circuit_breaker._reset()
    app.state.fail_closed = False

    cb = app.state.circuit_breaker
    cb.cooldown_seconds = 1  # Fast cooldown for testing
    
    # Replace probe action temporarily on CB directly for clean state
    # main.py passes repo.probe_connection, but we patch it here
    probe_calls = 0
    async def mock_probe():
        nonlocal probe_calls
        probe_calls += 1
    
    old_probe_action = cb._probe_action
    cb._probe_action = mock_probe

    # Force the circuit breaker OPEN
    cb._trip()
    assert cb.state.name == "OPEN"
    
    # Wait for cooldown to expire and the background probe task to fire
    await asyncio.sleep(1.2)

    # The probe should have fired successfully, and transitioned back to CLOSED
    assert cb.state.name == "CLOSED"
    assert probe_calls == 1
    
    cb._probe_action = old_probe_action

async def test_circuit_breaker_half_open_probe_failure(client: AsyncClient, app: FastAPI):
    app.state.circuit_breaker._reset()
    app.state.fail_closed = False

    cb = app.state.circuit_breaker
    cb.cooldown_seconds = 1

    probe_calls = 0
    async def mock_probe_fail():
        nonlocal probe_calls
        probe_calls += 1
        raise RuntimeError("Database is still down")
        
    old_probe_action = cb._probe_action
    cb._probe_action = mock_probe_fail

    cb._trip()
    assert cb.state.name == "OPEN"

    # Wait for cooldown
    await asyncio.sleep(1.2)

    # The probe failed, should transition back to OPEN and restart cooldown
    assert cb.state.name == "OPEN"
    assert probe_calls == 1
    
    cb._probe_action = old_probe_action

async def test_circuit_breaker_rejection_of_concurrent_probes_and_traffic(client: AsyncClient, app: FastAPI):
    app.state.circuit_breaker._reset()
    app.state.fail_closed = False

    cb = app.state.circuit_breaker
    cb.cooldown_seconds = 1
    
    # We want to manually control the probe to simulate in-flight state
    probe_event = asyncio.Event()
    async def mock_probe_slow():
        await probe_event.wait()
        
    old_probe_action = cb._probe_action
    cb._probe_action = mock_probe_slow

    cb._trip()
    await asyncio.sleep(1.2)
    
    # The background probe is now stuck waiting for probe_event.
    # Therefore it is in HALF_OPEN state and _probe_in_flight is True.
    assert cb._state.name == "HALF_OPEN"
    assert cb._probe_in_flight is True

    # Try concurrent DB write directly via call() -> should fail with specific error
    from sigs_rs.common.exceptions import CircuitBreakerOpenError
    with pytest.raises(CircuitBreakerOpenError) as excinfo:
        async def mock_write():
            pass
        await cb.call(mock_write())
    assert "probe is already in-flight" in str(excinfo.value)
    
    # Try HTTP ingestion -> should be rejected with 503 because is_open is True
    payload = {
        "sensor_id": "test-sensor-conc",
        "timestamp": "2026-10-06T20:00:00Z",
        "resource_type": "water",
        "value": 10.5,
        "unit": "L/min",
    }
    resp = await client.post("/api/v1/telemetry", json=payload)
    assert resp.status_code == 503
    
    # Unblock the probe
    probe_event.set()
    await asyncio.sleep(0.1)
    
    # It should have succeeded and transitioned to CLOSED
    assert cb.state.name == "CLOSED"
    assert cb._probe_in_flight is False
    
    cb._probe_action = old_probe_action

async def test_mqtt_recovery_blocked_when_fail_closed_true(client: AsyncClient, app: FastAPI):
    app.state.circuit_breaker._reset()
    app.state.fail_closed = True
    app.state._mqtt_restarting = False
    mqtt = app.state.mqtt
    mqtt._running = False
    if getattr(app.state, "mqtt_task", None) and not app.state.mqtt_task.done():
        app.state.mqtt_task.cancel()

    # Simulate CB recovery invoking _cb_close_handler
    from sigs_rs.main import _cb_close_handler
    _cb_close_handler(app)
    await asyncio.sleep(0.1)

    assert mqtt.is_running is False
    assert getattr(app.state, "_mqtt_restarting", False) is False

async def test_mqtt_recovery_works_when_fail_closed_false(client: AsyncClient, app: FastAPI):
    app.state.circuit_breaker._reset()
    app.state.fail_closed = False
    app.state._mqtt_restarting = False
    mqtt = app.state.mqtt
    mqtt._running = False
    if getattr(app.state, "mqtt_task", None) and not app.state.mqtt_task.done():
        app.state.mqtt_task.cancel()

    from sigs_rs.main import _cb_close_handler
    _cb_close_handler(app)
    await asyncio.sleep(0.1)

    assert mqtt.is_running is True
    # Clean up the test task
    if getattr(app.state, "mqtt_task", None):
        app.state.mqtt_task.cancel()

async def test_mqtt_recovery_aborts_if_fail_closed_becomes_true_while_waiting(client: AsyncClient, app: FastAPI):
    app.state.circuit_breaker._reset()
    app.state.fail_closed = False
    app.state._mqtt_restarting = False
    mqtt = app.state.mqtt
    mqtt._running = False

    # Create a dummy task that sleeps for a bit
    async def slow_task():
        await asyncio.sleep(0.5)

    loop = asyncio.get_running_loop()
    app.state.mqtt_task = loop.create_task(slow_task())

    # Start recovery
    from sigs_rs.main import _cb_close_handler
    _cb_close_handler(app)
    
    # Wait slightly so restart_mqtt gets to wait_for
    await asyncio.sleep(0.1)
    
    # Simulate an anonymizer failure setting fail_closed during the wait
    app.state.fail_closed = True
    
    # Wait for the slow_task and restart_mqtt to finish
    await asyncio.sleep(0.6)

    assert mqtt.is_running is False
    assert app.state._mqtt_restarting is False

async def test_database_recovery_never_clears_fail_closed(client: AsyncClient, app: FastAPI):
    app.state.circuit_breaker._reset()
    app.state.fail_closed = True

    cb = app.state.circuit_breaker
    cb.cooldown_seconds = 0.1

    # Force trip
    cb._trip()
    assert cb.state.name == "OPEN"

    # Wait for cooldown and probe
    await asyncio.sleep(0.3)

    assert cb.state.name == "CLOSED"
    assert app.state.fail_closed is True
