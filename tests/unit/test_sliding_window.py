"""
Comprehensive unit tests for the SlidingWindow — Phase 3.

Covers:
  - First packet for a new sensor
  - Exactly 50 packets in 10 seconds (boundary)
  - 51st packet rejected
  - Packets leaving the sliding window (time advance)
  - Multiple independent sensors
  - Burst traffic (all 50 at same timestamp)
  - Cleanup of stale sensors
  - Constructor validation
  - sensor_count / active_sensor_count introspection
  - Concurrent sensor independence (sensor A full does not block sensor B)
"""
from __future__ import annotations

import pytest

from sigs_rs.middleware.sliding_window import SlidingWindow


# ─── Helpers ──────────────────────────────────────────────────────────────────

def fill_window(sw: SlidingWindow, sensor_id: str, count: int, start: float = 1000.0) -> float:
    """
    Insert `count` packets for `sensor_id` at consecutive timestamps
    starting from `start`, each 0.01s apart.  Returns the last timestamp used.
    """
    t = start
    for _ in range(count):
        result = sw.check(sensor_id, now=t)
        assert result is True, f"Expected acceptance at count {_}, got rejection"
        t += 0.01
    return t - 0.01  # last timestamp used


# ─── Constructor ──────────────────────────────────────────────────────────────

class TestConstructor:
    def test_valid_params(self):
        sw = SlidingWindow(window_seconds=10, max_packets=50)
        assert sw.window_seconds == 10
        assert sw.max_packets == 50

    def test_zero_window_raises(self):
        with pytest.raises(ValueError, match="positive"):
            SlidingWindow(window_seconds=0, max_packets=50)

    def test_negative_window_raises(self):
        with pytest.raises(ValueError, match="positive"):
            SlidingWindow(window_seconds=-1, max_packets=50)

    def test_zero_max_packets_raises(self):
        with pytest.raises(ValueError, match="positive"):
            SlidingWindow(window_seconds=10, max_packets=0)

    def test_negative_max_packets_raises(self):
        with pytest.raises(ValueError, match="positive"):
            SlidingWindow(window_seconds=10, max_packets=-5)

    def test_min_valid_params(self):
        """window_seconds=1, max_packets=1 — tightest possible config."""
        sw = SlidingWindow(window_seconds=1, max_packets=1)
        assert sw.check("s", now=100.0) is True
        assert sw.check("s", now=100.5) is False


# ─── First packet ─────────────────────────────────────────────────────────────

class TestFirstPacket:
    def test_first_packet_accepted(self):
        sw = SlidingWindow(window_seconds=10, max_packets=50)
        assert sw.check("sensor-001", now=1000.0) is True

    def test_sensor_count_after_first(self):
        sw = SlidingWindow(window_seconds=10, max_packets=50)
        sw.check("sensor-001", now=1000.0)
        assert sw.sensor_count("sensor-001") == 1

    def test_active_sensors_after_first(self):
        sw = SlidingWindow(window_seconds=10, max_packets=50)
        sw.check("sensor-001", now=1000.0)
        assert sw.active_sensor_count() == 1

    def test_brand_new_sensor_has_zero_count(self):
        sw = SlidingWindow(window_seconds=10, max_packets=50)
        assert sw.sensor_count("never-seen") == 0


# ─── Exactly 50 packets ──────────────────────────────────────────────────────

class TestExactly50:
    """SRS REQ-API-2: limit is 50 packets per 10-second window."""

    def test_50_packets_all_accepted(self):
        sw = SlidingWindow(window_seconds=10, max_packets=50)
        for i in range(50):
            assert sw.check("s1", now=1000.0 + i * 0.01) is True
        assert sw.sensor_count("s1") == 50

    def test_50th_packet_is_boundary(self):
        sw = SlidingWindow(window_seconds=10, max_packets=50)
        fill_window(sw, "s1", 49, start=1000.0)
        # 50th packet — must be accepted
        assert sw.check("s1", now=1000.49) is True
        assert sw.sensor_count("s1") == 50


# ─── 51st packet ──────────────────────────────────────────────────────────────

class Test51st:
    """The 51st packet within the window MUST be dropped."""

    def test_51st_packet_rejected(self):
        sw = SlidingWindow(window_seconds=10, max_packets=50)
        fill_window(sw, "s1", 50, start=1000.0)
        # 51st — still within the 10-second window
        assert sw.check("s1", now=1000.50) is False

    def test_count_stays_at_50_after_rejection(self):
        """Rejected packets do NOT add a timestamp to the window."""
        sw = SlidingWindow(window_seconds=10, max_packets=50)
        fill_window(sw, "s1", 50, start=1000.0)
        sw.check("s1", now=1000.50)  # rejected
        sw.check("s1", now=1000.51)  # rejected again
        assert sw.sensor_count("s1") == 50  # no growth

    def test_many_rejections(self):
        sw = SlidingWindow(window_seconds=10, max_packets=50)
        fill_window(sw, "s1", 50, start=1000.0)
        for i in range(100):
            assert sw.check("s1", now=1000.5 + i * 0.01) is False


# ─── Window expiry ────────────────────────────────────────────────────────────

class TestWindowExpiry:
    """Packets that leave the 10-second window must free capacity."""

    def test_after_full_window_elapses_sensor_resets(self):
        sw = SlidingWindow(window_seconds=10, max_packets=50)
        fill_window(sw, "s1", 50, start=1000.0)
        # 51st at t=1000.5 → rejected
        assert sw.check("s1", now=1000.5) is False
        # Advance time 10+ seconds past the LATEST timestamp (1000.49)
        # cutoff = 1010.5 - 10 = 1000.5 → all entries (1000.0..1000.49) < 1000.5
        assert sw.check("s1", now=1010.5) is True
        assert sw.sensor_count("s1") == 1  # only the new one

    def test_gradual_expiry_one_by_one(self):
        """As the oldest packet expires, one new slot opens."""
        sw = SlidingWindow(window_seconds=10, max_packets=3)
        # t=100.0, 101.0, 102.0 — fill 3 slots
        assert sw.check("s1", now=100.0) is True
        assert sw.check("s1", now=101.0) is True
        assert sw.check("s1", now=102.0) is True
        # t=103.0 — all 3 still in window (100..102 all within 10s of 103)
        assert sw.check("s1", now=103.0) is False
        # t=110.01 — t=100.0 has expired, one slot freed
        assert sw.check("s1", now=110.01) is True
        # still full: 101.0, 102.0, 110.01
        assert sw.check("s1", now=110.02) is False
        # t=111.01 — t=101.0 has expired, one more slot
        assert sw.check("s1", now=111.01) is True

    def test_partial_expiry(self):
        """Only expired entries are removed; recent ones stay."""
        sw = SlidingWindow(window_seconds=10, max_packets=50)
        # Fill 30 at t=1000.0..1000.29
        fill_window(sw, "s1", 30, start=1000.0)
        # Fill 20 more at t=1005.0..1005.19
        for i in range(20):
            sw.check("s1", now=1005.0 + i * 0.01)
        assert sw.sensor_count("s1") == 50
        # Advance to t=1010.3: first 30 (t=1000.0..1000.29) are expired
        # but second 20 (t=1005.0..1005.19) are still in window
        assert sw.check("s1", now=1010.3) is True  # frees 30 slots
        # After eviction: 20 old + 1 new = 21
        assert sw.sensor_count("s1") == 21


# ─── Multiple independent sensors ────────────────────────────────────────────

class TestMultipleSensors:
    """Each sensor has an independent window."""

    def test_two_sensors_independent(self):
        sw = SlidingWindow(window_seconds=10, max_packets=50)
        fill_window(sw, "A", 50, start=1000.0)
        # Sensor A is full
        assert sw.check("A", now=1000.5) is False
        # Sensor B is unaffected
        assert sw.check("B", now=1000.5) is True
        assert sw.sensor_count("B") == 1

    def test_100_sensors_all_independent(self):
        sw = SlidingWindow(window_seconds=10, max_packets=5)
        for i in range(100):
            assert sw.check(f"sensor-{i}", now=1000.0) is True
        assert sw.active_sensor_count() == 100

    def test_one_sensor_full_others_open(self):
        sw = SlidingWindow(window_seconds=10, max_packets=3)
        fill_window(sw, "full", 3, start=100.0)
        assert sw.check("full", now=100.1) is False
        assert sw.check("open-1", now=100.1) is True
        assert sw.check("open-2", now=100.1) is True


# ─── Burst traffic ────────────────────────────────────────────────────────────

class TestBurstTraffic:
    """All packets arrive at the exact same timestamp."""

    def test_burst_exactly_at_limit(self):
        sw = SlidingWindow(window_seconds=10, max_packets=50)
        for i in range(50):
            assert sw.check("burst", now=1000.0) is True
        assert sw.sensor_count("burst") == 50

    def test_burst_one_over_limit(self):
        sw = SlidingWindow(window_seconds=10, max_packets=50)
        for i in range(50):
            sw.check("burst", now=1000.0)
        assert sw.check("burst", now=1000.0) is False

    def test_burst_recovery_after_window(self):
        sw = SlidingWindow(window_seconds=10, max_packets=50)
        for i in range(50):
            sw.check("burst", now=1000.0)
        assert sw.check("burst", now=1000.0) is False
        # After 10 seconds, all burst entries expire
        assert sw.check("burst", now=1010.01) is True


# ─── Cleanup ──────────────────────────────────────────────────────────────────

class TestCleanup:
    def test_cleanup_removes_stale_sensors(self):
        sw = SlidingWindow(window_seconds=10, max_packets=50)
        sw.check("s1", now=1000.0)
        sw.check("s2", now=1005.0)
        # At t=1011 — s1's last entry (1000.0) is stale, s2's (1005.0) is active
        removed = sw.cleanup(now=1011.0)
        assert removed == 1
        assert sw.active_sensor_count() == 1
        assert sw.sensor_count("s1") == 0
        assert sw.sensor_count("s2") == 1

    def test_cleanup_no_stale(self):
        sw = SlidingWindow(window_seconds=10, max_packets=50)
        sw.check("s1", now=1000.0)
        removed = sw.cleanup(now=1005.0)
        assert removed == 0

    def test_cleanup_all_stale(self):
        sw = SlidingWindow(window_seconds=10, max_packets=50)
        sw.check("s1", now=1000.0)
        sw.check("s2", now=1000.0)
        removed = sw.cleanup(now=1020.0)
        assert removed == 2
        assert sw.active_sensor_count() == 0

    def test_cleanup_empty_deques(self):
        """Cleanup should also remove sensors with empty deques."""
        sw = SlidingWindow(window_seconds=10, max_packets=50)
        sw.check("s1", now=1000.0)
        # Simulate all entries expiring via a check that evicts
        sw.check("s1", now=1020.0)  # evicts old, adds new
        # Now remove that one too
        removed = sw.cleanup(now=1040.0)
        assert removed == 1

    def test_returns_int(self):
        sw = SlidingWindow(window_seconds=10, max_packets=50)
        assert isinstance(sw.cleanup(), int)


# ─── Reset ────────────────────────────────────────────────────────────────────

class TestReset:
    def test_reset_clears_all(self):
        sw = SlidingWindow(window_seconds=10, max_packets=50)
        fill_window(sw, "s1", 50, start=1000.0)
        fill_window(sw, "s2", 30, start=1000.0)
        sw.reset()
        assert sw.active_sensor_count() == 0
        assert sw.sensor_count("s1") == 0
        assert sw.check("s1", now=2000.0) is True


# ─── Edge cases ───────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_max_packets_one(self):
        """Tightest possible rate limit: 1 packet per window."""
        sw = SlidingWindow(window_seconds=1, max_packets=1)
        assert sw.check("s1", now=100.0) is True
        assert sw.check("s1", now=100.5) is False
        assert sw.check("s1", now=101.01) is True  # first one expired

    def test_very_large_window(self):
        """Window of 3600s (1 hour) — all packets stay in window."""
        sw = SlidingWindow(window_seconds=3600, max_packets=3)
        assert sw.check("s1", now=0.0) is True
        assert sw.check("s1", now=1800.0) is True
        assert sw.check("s1", now=3599.0) is True
        assert sw.check("s1", now=3599.5) is False  # full

    def test_timestamps_at_exact_cutoff_boundary(self):
        """Timestamp exactly at cutoff (now - window_seconds) is expired."""
        sw = SlidingWindow(window_seconds=10, max_packets=1)
        sw.check("s1", now=100.0)
        # At t=110.0: cutoff = 100.0.  Entry at 100.0 < 100.0 is False,
        # so 100.0 is NOT evicted (cutoff boundary is exclusive).
        assert sw.check("s1", now=110.0) is False
        # At t=110.001: cutoff = 100.001.  Entry at 100.0 < 100.001, evicted.
        assert sw.check("s1", now=110.001) is True

    def test_monotonic_default(self):
        """When now is not provided, time.monotonic() is used."""
        sw = SlidingWindow(window_seconds=10, max_packets=50)
        assert sw.check("s1") is True  # uses real clock
        assert sw.sensor_count("s1") == 1
