"""
System-wide constants and default values.
These are the baseline values; runtime config overrides them via environment variables.
"""

# ─── Sliding-Window (Anomalous Packet Inspector) ──────────────────────────────
WINDOW_SECONDS: int = 10
MAX_PACKETS_PER_WINDOW: int = 50
WINDOW_CLEANUP_INTERVAL: int = 60  # seconds between idle-sensor sweeps

# ─── Telemetry Payload ─────────────────────────────────────────────────────────
MAX_PAYLOAD_BYTES: int = 524_288  # 512 KB  (SRS REQ-TI-3)

# ─── MQTT ─────────────────────────────────────────────────────────────────────
MQTT_PORT_TLS: int = 8883
MQTT_TOPIC_PREFIX: str = "telemetry/#"

# ─── Circuit Breaker ──────────────────────────────────────────────────────────
CB_FAILURE_THRESHOLD_MS: int = 2_000   # SRS REQ-SF-2
CB_COOLDOWN_SECONDS: int = 30

# ─── Ingestion Queue ──────────────────────────────────────────────────────────
QUEUE_MAX_SIZE: int = 10_000

# ─── PII Fields ───────────────────────────────────────────────────────────────
# Default fields stripped by the Privacy Anonymizer (SRS REQ-PA-1, Appendix A)
DEFAULT_PII_FIELDS: list[str] = [
    "latitude",
    "longitude",
    "lat",
    "lon",
    "mac_address",
    "device_id",
    "geo",
]

# ─── Resource Types ───────────────────────────────────────────────────────────
VALID_RESOURCE_TYPES: tuple[str, ...] = ("water", "energy", "air_quality")

# ─── Valid Units per Resource Type (implementation decision) ──────────────────
# Used by the schema validator to reject structurally invalid unit strings.
# The SRS does not enumerate units; these reflect the agreed sensor-grid spec.
VALID_UNITS_BY_RESOURCE: dict[str, frozenset[str]] = {
    "water": frozenset({
        "liters_per_min", "L/min", "litres_per_min",
        "gallons_per_min", "gal/min",
        "cubic_meters_per_hour", "m3/h",
        "milliliters_per_sec", "mL/s",
    }),
    "energy": frozenset({
        "kWh", "Wh", "MWh",
        "W", "kW", "MW",
        "J", "kJ", "MJ",
    }),
    "air_quality": frozenset({
        "AQI", "index", "ppm",
    }),
}

# ─── Impossible-Value Bounds (SRS REQ-API-3) — enforced by Inspector Phase 3 ─
IMPOSSIBLE_VALUE_BOUNDS: dict[str, dict[str, float | None]] = {
    "energy":      {"min": 0.0,   "max": None},   # kWh — cannot be negative
    "water":       {"min": 0.0,   "max": None},   # litres/min — cannot be negative
    "air_quality": {"min": 0.0,   "max": 500.0},  # AQI index 0–500
}

# ─── Structural field bounds ───────────────────────────────────────────────────
SENSOR_ID_MIN_LEN: int = 1
SENSOR_ID_MAX_LEN: int = 64
TIMESTAMP_FUTURE_TOLERANCE_MINUTES: int = 5   # clock-skew tolerance
BATTERY_LEVEL_MIN: int = 0
BATTERY_LEVEL_MAX: int = 100
LAT_MIN: float = -90.0
LAT_MAX: float = 90.0
LON_MIN: float = -180.0
LON_MAX: float = 180.0

# ─── Audit Event Types ────────────────────────────────────────────────────────
class AuditEvent:
    PACKET_DROPPED_RATE       = "PACKET_DROPPED_RATE"
    PACKET_DROPPED_IMPOSSIBLE = "PACKET_DROPPED_IMPOSSIBLE"
    PACKET_DROPPED_OVERSIZED  = "PACKET_DROPPED_OVERSIZED"
    PACKET_DROPPED_MALFORMED  = "PACKET_DROPPED_MALFORMED"
    ANONYMIZER_FAIL_SECURE    = "ANONYMIZER_FAIL_SECURE"
    AUTH_FAILURE              = "AUTH_FAILURE"
    CONFIG_CHANGE             = "CONFIG_CHANGE"
    CB_OPEN                   = "CIRCUIT_BREAKER_OPEN"
    CB_CLOSE                  = "CIRCUIT_BREAKER_CLOSE"
    SYSTEM_FAIL_CLOSED        = "SYSTEM_FAIL_CLOSED"
    SYSTEM_STARTUP            = "SYSTEM_STARTUP"
    SYSTEM_SHUTDOWN           = "SYSTEM_SHUTDOWN"

# ─── Role Names ───────────────────────────────────────────────────────────────
class Role:
    SECURITY_ENGINEER = "security_engineer"
    BACKEND_DEVELOPER = "backend_developer"
    QA_TESTER         = "qa_tester"
    DEVOPS            = "devops"
