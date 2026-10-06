"""
Production metrics collector and Prometheus exporter for SIGS-RS.

Tracks:
  - total packets received
  - valid packets
  - malformed packets
  - packets dropped by rate threshold
  - packets dropped by invalid metrics
  - packets dropped by anonymization failure
  - packets persisted
  - current queue size
  - processing latency
  - inspector latency
  - anonymizer latency
  - database latency
  - MQTT connection state
  - active sensors
  - circuit breaker state
"""
from __future__ import annotations

import threading
import time
from typing import Any


class LatencyTracker:
    """Thread-safe latency tracker computing count, sum, average, and last value."""

    __slots__ = ("_count", "_sum", "_last", "_lock")

    def __init__(self) -> None:
        self._count: int = 0
        self._sum: float = 0.0
        self._last: float = 0.0
        self._lock = threading.Lock()

    def record(self, duration_seconds: float, count: int = 1) -> None:
        if count <= 0:
            return
        with self._lock:
            self._count += count
            self._sum += (duration_seconds * count)
            self._last = duration_seconds

    @property
    def count(self) -> int:
        return self._count

    @property
    def sum(self) -> float:
        return self._sum

    @property
    def last(self) -> float:
        return self._last

    @property
    def average(self) -> float:
        with self._lock:
            return (self._sum / self._count) if self._count > 0 else 0.0

    def to_dict(self) -> dict[str, float]:
        with self._lock:
            avg = (self._sum / self._count) if self._count > 0 else 0.0
            return {
                "count": self._count,
                "sum_seconds": round(self._sum, 6),
                "avg_seconds": round(avg, 6),
                "last_seconds": round(self._last, 6),
            }


class MetricsCollector:
    """
    Central in-memory metrics registry for gateway observability.
    Supports thread-safe increments and Prometheus text exposition formatting.
    """

    def __init__(self) -> None:
        self._start_time = time.monotonic()
        self._lock = threading.Lock()

        # Packet counters
        self._packets_received: int = 0
        self._packets_valid: int = 0
        self._packets_malformed: int = 0
        self._packets_dropped_rate: int = 0
        self._packets_dropped_invalid_metrics: int = 0
        self._packets_dropped_anonymization: int = 0
        self._packets_persisted: int = 0

        # Latencies
        self.processing_latency = LatencyTracker()
        self.inspector_latency = LatencyTracker()
        self.anonymizer_latency = LatencyTracker()
        self.database_latency = LatencyTracker()

        # Dynamic state gauges
        self._queue_size: int = 0
        self._active_sensors: int = 0
        self._mqtt_connected: int = 0
        self._circuit_breaker_state: str = "CLOSED"

    # ── Counter Updates ───────────────────────────────────────────────────────

    def record_packet_received(self, count: int = 1) -> None:
        with self._lock:
            self._packets_received += count

    def record_packet_valid(self, count: int = 1) -> None:
        with self._lock:
            self._packets_valid += count

    def record_packet_malformed(self, count: int = 1) -> None:
        with self._lock:
            self._packets_malformed += count

    def record_packet_dropped_rate(self, count: int = 1) -> None:
        with self._lock:
            self._packets_dropped_rate += count

    def record_packet_dropped_invalid_metrics(self, count: int = 1) -> None:
        with self._lock:
            self._packets_dropped_invalid_metrics += count

    def record_packet_dropped_anonymization(self, count: int = 1) -> None:
        with self._lock:
            self._packets_dropped_anonymization += count

    def record_packet_persisted(self, count: int = 1) -> None:
        with self._lock:
            self._packets_persisted += count

    # ── Latency Updates ───────────────────────────────────────────────────────

    def record_processing_latency(self, duration_seconds: float, count: int = 1) -> None:
        self.processing_latency.record(duration_seconds, count=count)

    def record_inspector_latency(self, duration_seconds: float) -> None:
        self.inspector_latency.record(duration_seconds)

    def record_anonymizer_latency(self, duration_seconds: float) -> None:
        self.anonymizer_latency.record(duration_seconds)

    def record_database_latency(self, duration_seconds: float) -> None:
        self.database_latency.record(duration_seconds)

    # ── Gauge Updates ─────────────────────────────────────────────────────────

    def set_queue_size(self, size: int) -> None:
        with self._lock:
            self._queue_size = max(0, size)

    def set_active_sensors(self, count: int) -> None:
        with self._lock:
            self._active_sensors = max(0, count)

    def set_mqtt_connected(self, connected: bool) -> None:
        with self._lock:
            self._mqtt_connected = 1 if connected else 0

    def set_circuit_breaker_state(self, state: str) -> None:
        with self._lock:
            self._circuit_breaker_state = state.upper()

    # ── Live State Resolution ─────────────────────────────────────────────────

    def _resolve_live_state(self, app_state: Any | None = None) -> dict[str, Any]:
        """Combine internal counters with live application state if provided."""
        uptime = round(time.monotonic() - self._start_time, 2)
        queue_size = self._queue_size
        active_sensors = self._active_sensors
        mqtt_connected = self._mqtt_connected
        cb_state_str = self._circuit_breaker_state

        if app_state is not None:
            # Live queue depth
            queue = getattr(app_state, "queue", None)
            if queue is not None:
                queue_size = getattr(queue, "depth", queue_size)

            # Live sliding window active sensors
            inspector = getattr(app_state, "inspector", None)
            if inspector is not None:
                window = getattr(inspector, "window", None)
                if window is not None and hasattr(window, "active_sensor_count"):
                    active_sensors = window.active_sensor_count()

            # Live circuit breaker state
            cb = getattr(app_state, "circuit_breaker", None)
            if cb is not None:
                state_obj = getattr(cb, "state", None)
                cb_state_str = getattr(state_obj, "name", cb_state_str)

            # Live MQTT connection state
            mqtt = getattr(app_state, "mqtt", None)
            if mqtt is not None:
                is_conn = getattr(mqtt, "is_connected", False) or (
                    getattr(mqtt, "is_running", False) and not getattr(mqtt, "is_stopped", False)
                )
                mqtt_connected = 1 if is_conn else 0

        # CB state numeric encoding: 0 = CLOSED, 1 = HALF_OPEN, 2 = OPEN
        cb_code = 0
        if "HALF" in cb_state_str:
            cb_code = 1
        elif "OPEN" in cb_state_str:
            cb_code = 2

        with self._lock:
            return {
                "packets_received_total": self._packets_received,
                "packets_valid_total": self._packets_valid,
                "packets_malformed_total": self._packets_malformed,
                "packets_dropped_rate_total": self._packets_dropped_rate,
                "packets_dropped_invalid_metrics_total": self._packets_dropped_invalid_metrics,
                "packets_dropped_anonymization_total": self._packets_dropped_anonymization,
                "packets_persisted_total": self._packets_persisted,
                "current_queue_size": queue_size,
                "active_sensors": active_sensors,
                "mqtt_connected": mqtt_connected,
                "circuit_breaker_state": cb_state_str,
                "circuit_breaker_code": cb_code,
                "uptime_seconds": uptime,
                "processing_latency": self.processing_latency.to_dict(),
                "inspector_latency": self.inspector_latency.to_dict(),
                "anonymizer_latency": self.anonymizer_latency.to_dict(),
                "database_latency": self.database_latency.to_dict(),
            }

    # ── Prometheus Text Format Export ─────────────────────────────────────────

    def to_prometheus_text(self, app_state: Any | None = None) -> str:
        """
        Format all collected metrics in standard Prometheus exposition format (v0.0.4).
        """
        data = self._resolve_live_state(app_state)
        proc_lat = data["processing_latency"]
        insp_lat = data["inspector_latency"]
        anon_lat = data["anonymizer_latency"]
        db_lat = data["database_latency"]

        lines = [
            "# HELP sigs_packets_received_total Total telemetry packets received by gateway",
            "# TYPE sigs_packets_received_total counter",
            f"sigs_packets_received_total {data['packets_received_total']}",
            "",
            "# HELP sigs_packets_valid_total Total telemetry packets that passed initial validation",
            "# TYPE sigs_packets_valid_total counter",
            f"sigs_packets_valid_total {data['packets_valid_total']}",
            "",
            "# HELP sigs_packets_malformed_total Total telemetry packets dropped due to malformed payload or size limit",
            "# TYPE sigs_packets_malformed_total counter",
            f"sigs_packets_malformed_total {data['packets_malformed_total']}",
            "",
            "# HELP sigs_packets_dropped_rate_total Total packets dropped by rate threshold (sliding window)",
            "# TYPE sigs_packets_dropped_rate_total counter",
            f"sigs_packets_dropped_rate_total {data['packets_dropped_rate_total']}",
            "",
            "# HELP sigs_packets_dropped_invalid_metrics_total Total packets dropped by impossible or invalid metrics",
            "# TYPE sigs_packets_dropped_invalid_metrics_total counter",
            f"sigs_packets_dropped_invalid_metrics_total {data['packets_dropped_invalid_metrics_total']}",
            "",
            "# HELP sigs_packets_dropped_anonymization_total Total packets dropped due to anonymization failure",
            "# TYPE sigs_packets_dropped_anonymization_total counter",
            f"sigs_packets_dropped_anonymization_total {data['packets_dropped_anonymization_total']}",
            "",
            "# HELP sigs_packets_persisted_total Total packets successfully persisted to database",
            "# TYPE sigs_packets_persisted_total counter",
            f"sigs_packets_persisted_total {data['packets_persisted_total']}",
            "",
            "# HELP sigs_queue_size_current Current number of packets in the ingestion queue",
            "# TYPE sigs_queue_size_current gauge",
            f"sigs_queue_size_current {data['current_queue_size']}",
            "",
            "# HELP sigs_processing_batch_latency_seconds End-to-end processing latency for a batch in seconds",
            "# TYPE sigs_processing_batch_latency_seconds summary",
            f"sigs_processing_batch_latency_seconds_sum {proc_lat['sum_seconds']}",
            f"sigs_processing_batch_latency_seconds_count {proc_lat['count']}",
            f"sigs_processing_batch_latency_seconds_avg {proc_lat['avg_seconds']}",
            f"sigs_processing_batch_latency_seconds {proc_lat['last_seconds']}",
            "",
            "# HELP sigs_inspector_latency_seconds Inspection stage latency in seconds",
            "# TYPE sigs_inspector_latency_seconds summary",
            f"sigs_inspector_latency_seconds_sum {insp_lat['sum_seconds']}",
            f"sigs_inspector_latency_seconds_count {insp_lat['count']}",
            f"sigs_inspector_latency_seconds_avg {insp_lat['avg_seconds']}",
            f"sigs_inspector_latency_seconds {insp_lat['last_seconds']}",
            "",
            "# HELP sigs_anonymizer_latency_seconds Anonymizer stage latency in seconds",
            "# TYPE sigs_anonymizer_latency_seconds summary",
            f"sigs_anonymizer_latency_seconds_sum {anon_lat['sum_seconds']}",
            f"sigs_anonymizer_latency_seconds_count {anon_lat['count']}",
            f"sigs_anonymizer_latency_seconds_avg {anon_lat['avg_seconds']}",
            f"sigs_anonymizer_latency_seconds {anon_lat['last_seconds']}",
            "",
            "# HELP sigs_database_batch_latency_seconds Database batch persistence latency in seconds",
            "# TYPE sigs_database_batch_latency_seconds summary",
            f"sigs_database_batch_latency_seconds_sum {db_lat['sum_seconds']}",
            f"sigs_database_batch_latency_seconds_count {db_lat['count']}",
            f"sigs_database_batch_latency_seconds_avg {db_lat['avg_seconds']}",
            f"sigs_database_batch_latency_seconds {db_lat['last_seconds']}",
            "",
            "# HELP sigs_mqtt_connected MQTT broker connection status (1 = connected, 0 = disconnected)",
            "# TYPE sigs_mqtt_connected gauge",
            f"sigs_mqtt_connected {data['mqtt_connected']}",
            "",
            "# HELP sigs_active_sensors Count of distinct active sensors tracked by sliding window",
            "# TYPE sigs_active_sensors gauge",
            f"sigs_active_sensors {data['active_sensors']}",
            "",
            "# HELP sigs_circuit_breaker_state Circuit breaker state (0 = CLOSED, 1 = HALF_OPEN, 2 = OPEN)",
            "# TYPE sigs_circuit_breaker_state gauge",
            f"sigs_circuit_breaker_state {data['circuit_breaker_code']}",
            f"sigs_circuit_breaker_state_info{{state=\"{data['circuit_breaker_state']}\"}} 1",
            "",
            "# HELP sigs_uptime_seconds Gateway uptime in seconds",
            "# TYPE sigs_uptime_seconds gauge",
            f"sigs_uptime_seconds {data['uptime_seconds']}",
            "",
        ]
        return "\n".join(lines)

    # ── JSON / Dict Representation ───────────────────────────────────────────

    def to_dict(self, app_state: Any | None = None) -> dict[str, Any]:
        """Return all metrics as a dictionary for JSON reporting."""
        data = self._resolve_live_state(app_state)
        # Compute backward-compatible fields
        packets_ingested = (
            data["packets_persisted_total"]
            + data["packets_dropped_rate_total"]
            + data["packets_dropped_invalid_metrics_total"]
            + data["packets_dropped_anonymization_total"]
        )
        packets_dropped = (
            data["packets_dropped_rate_total"]
            + data["packets_dropped_invalid_metrics_total"]
            + data["packets_dropped_anonymization_total"]
            + data["packets_malformed_total"]
        )

        return {
            # Standard Phase 10 Production Metrics
            "packets_received_total": data["packets_received_total"],
            "packets_valid_total": data["packets_valid_total"],
            "packets_malformed_total": data["packets_malformed_total"],
            "packets_dropped_rate_total": data["packets_dropped_rate_total"],
            "packets_dropped_invalid_metrics_total": data["packets_dropped_invalid_metrics_total"],
            "packets_dropped_anonymization_total": data["packets_dropped_anonymization_total"],
            "packets_persisted_total": data["packets_persisted_total"],
            "current_queue_size": data["current_queue_size"],
            "processing_latency": data["processing_latency"],
            "inspector_latency": data["inspector_latency"],
            "anonymizer_latency": data["anonymizer_latency"],
            "database_latency": data["database_latency"],
            "mqtt_connection_state": data["mqtt_connected"],
            "active_sensors": data["active_sensors"],
            "circuit_breaker_state": data["circuit_breaker_state"],
            "uptime_seconds": data["uptime_seconds"],
            # Legacy / Backward-Compatible keys for Phase 1/7 schemas
            "packets_ingested_total": packets_ingested or data["packets_received_total"],
            "packets_dropped_total": packets_dropped,
            "packets_sanitized_total": data["packets_persisted_total"],
            "queue_depth": data["current_queue_size"],
        }

    def reset(self) -> None:
        """Reset all counters and state. Primarily for testing."""
        with self._lock:
            self._packets_received = 0
            self._packets_valid = 0
            self._packets_malformed = 0
            self._packets_dropped_rate = 0
            self._packets_dropped_invalid_metrics = 0
            self._packets_dropped_anonymization = 0
            self._packets_persisted = 0
            self._queue_size = 0
            self._active_sensors = 0
            self._mqtt_connected = 0
            self._circuit_breaker_state = "CLOSED"
            self.processing_latency = LatencyTracker()
            self.inspector_latency = LatencyTracker()
            self.anonymizer_latency = LatencyTracker()
            self.database_latency = LatencyTracker()


# Global Singleton Metrics Collector
_metrics_collector = MetricsCollector()


def get_metrics() -> MetricsCollector:
    """Return the global MetricsCollector instance."""
    return _metrics_collector
