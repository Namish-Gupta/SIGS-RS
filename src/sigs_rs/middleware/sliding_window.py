"""
Sliding-window algorithm for the Anomalous Packet Inspector.

SRS REQ-API-1: track timestamps of all packets per sensor ID over a
               rolling 10-second window.
SRS REQ-API-2: drop packets if sensor exceeds 50 packets per window.

Data structure: Redis Sorted Set
  - Key: `sigs:ratelimit:{sensor_id}`
  - Score: timestamp
  - Value: timestamp + unique_id

Concurrency model:
  Uses a Redis Lua script to atomically remove expired packets, check the limit,
  and insert the new packet. Ensures global rate-limiting across all gateway pods.
"""
from __future__ import annotations

import time
import logging
import uuid
from redis.asyncio import Redis, RedisError

logger = logging.getLogger(__name__)

LUA_RATE_LIMIT = """
local key = KEYS[1]
local max_packets = tonumber(ARGV[1])
local window_seconds = tonumber(ARGV[2])
local unique_id = ARGV[3]
local passed_now = tonumber(ARGV[4])

local now
if passed_now and passed_now > 0 then
    now = passed_now
else
    local time_res = redis.call('TIME')
    now = tonumber(time_res[1]) + (tonumber(time_res[2]) / 1000000.0)
end

local cutoff = now - window_seconds

redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
local count = redis.call('ZCARD', key)

if count >= max_packets then
    return 0
end

local member = tostring(now) .. "-" .. unique_id
redis.call('ZADD', key, now, member)
-- Retain the key for the duration of the window to avoid memory leaks
redis.call('PEXPIRE', key, window_seconds * 1000 + 1000)
return 1
"""

class SlidingWindow:
    """
    Per-sensor rolling timestamp window using Redis.
    """

    def __init__(self, window_seconds: int, max_packets: int, redis_url: str) -> None:
        if window_seconds <= 0:
            raise ValueError(f"window_seconds must be positive, got {window_seconds}")
        if max_packets <= 0:
            raise ValueError(f"max_packets must be positive, got {max_packets}")
        if not redis_url:
            raise ValueError("redis_url must be provided for shared rate limiting.")

        self.window_seconds = window_seconds
        self.max_packets = max_packets
        self._redis = Redis.from_url(redis_url, decode_responses=True, socket_timeout=2.0)
        self._script = self._redis.register_script(LUA_RATE_LIMIT)

    async def check(self, sensor_id: str, now: float | None = None) -> bool:
        """
        Check whether the sensor is within the allowed rate and, if so,
        record the current timestamp.
        """
        key = f"sigs:ratelimit:{sensor_id}"
        unique_id = uuid.uuid4().hex
        passed_now = now if now is not None else 0.0

        try:
            result = await self._script(
                keys=[key],
                args=[self.max_packets, self.window_seconds, unique_id, passed_now]
            )
            return bool(result)
        except RedisError as exc:
            logger.critical("Redis error during rate limit check: %s. Failing closed.", exc)
            # Fail closed as required
            raise RuntimeError(f"Fail-closed: Shared rate limit state unavailable: {exc}") from exc

    async def cleanup(self, now: float | None = None) -> int:
        """
        Redis EXPIRE handles cleanup automatically.
        """
        return 0

    async def reset(self) -> None:
        """Clear all tracked state. Useful for tests."""
        try:
            keys = await self._redis.keys("sigs:ratelimit:*")
            if keys:
                await self._redis.delete(*keys)
        except RedisError:
            pass

    async def close(self) -> None:
        """Close the Redis connection pool."""
        if hasattr(self, '_redis') and self._redis:
            await self._redis.aclose()
