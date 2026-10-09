import pytest
import asyncio
import time
from sigs_rs.middleware.sliding_window import SlidingWindow
from redis.asyncio import Redis, RedisError

REDIS_URL = "redis://127.0.0.1:6379/0"

async def check_redis_available():
    r = Redis.from_url(REDIS_URL, socket_timeout=0.5)
    try:
        await r.ping()
        return True
    except Exception:
        return False
    finally:
        await r.aclose()

@pytest.fixture
async def real_redis():
    if not await check_redis_available():
        pytest.skip("Redis not available on 127.0.0.1:6379")
    window = SlidingWindow(window_seconds=10, max_packets=50, redis_url=REDIS_URL)
    await window.reset()
    yield window
    await window.reset()
    await window.close()

@pytest.mark.asyncio
@pytest.mark.real_redis
async def test_real_redis_atomicity(real_redis):
    window = real_redis
    sensor_id = "sensor-atomic-1"
    
    # Test identical timestamps (timestamp collision fix)
    now = time.time()
    
    async def insert_packet():
        return await window.check(sensor_id, now=now)
        
    results = await asyncio.gather(*(insert_packet() for _ in range(50)))
    assert sum(results) == 50, "Expected all 50 concurrent packets to be accepted"
    
    # Next packet should be rejected
    res = await window.check(sensor_id, now=now)
    assert not res, "51st packet should be rejected"
    
    # Test expiration
    res_future = await window.check(sensor_id, now=now + 11)
    assert res_future, "Packet should be accepted after window expires"

@pytest.mark.asyncio
@pytest.mark.real_redis
async def test_real_redis_error_fail_closed():
    window = SlidingWindow(window_seconds=10, max_packets=50, redis_url="redis://127.0.0.1:6379/99")
    # Force an invalid command or timeout by closing the connection immediately
    await window.close()
    
    with pytest.raises(RuntimeError, match="Fail-closed"):
        await window.check("sensor-fail", now=time.time())

