"""Custom exception hierarchy for SIGS-RS."""


class SIGSError(Exception):
    """Base exception for all SIGS-RS errors."""


# ─── Ingestion ────────────────────────────────────────────────────────────────

class IngestionError(SIGSError):
    """Raised when a packet cannot be accepted at the ingestion boundary."""


class PayloadTooLargeError(IngestionError):
    """Packet exceeds the 512 KB size limit (SRS REQ-TI-3)."""


class MalformedPayloadError(IngestionError):
    """Packet is not valid JSON or violates the schema (SRS REQ-TI-2)."""


class QueueFullError(IngestionError):
    """In-memory ingestion queue is at capacity — back-pressure."""


# ─── Anomalous Packet Inspector ───────────────────────────────────────────────

class InspectorError(SIGSError):
    """Base for Inspector subsystem errors."""


class RateExceededError(InspectorError):
    """Sensor has exceeded the 50-packet / 10-second sliding window (SRS REQ-API-2)."""


class ImpossibleValueError(InspectorError):
    """Payload contains statistically impossible resource values (SRS REQ-API-3)."""


# ─── Privacy Anonymizer ───────────────────────────────────────────────────────

class AnonymizerError(SIGSError):
    """Raised when the masking function cannot safely strip PII (SRS REQ-PA-4)."""


# ─── Persistence ─────────────────────────────────────────────────────────────

class PersistenceError(SIGSError):
    """Raised when a database write fails."""


class CircuitBreakerOpenError(PersistenceError):
    """Circuit breaker is OPEN — database writes are halted (SRS REQ-SF-2)."""


# ─── Authentication ───────────────────────────────────────────────────────────

class AuthError(SIGSError):
    """Base for authentication and authorization errors."""


class InvalidTokenError(AuthError):
    """JWT token is missing, expired, or has an invalid signature."""


class InsufficientRoleError(AuthError):
    """Authenticated user lacks the required role for this operation."""


# ─── Configuration ────────────────────────────────────────────────────────────

class ConfigError(SIGSError):
    """Raised when the application configuration is invalid at startup."""


class ProductionDatabaseError(ConfigError):
    """DATABASE_URL points to a non-staging host (SRS REQ-SF-1)."""


# ─── Compliance (BR-3) ────────────────────────────────────────────────────────

class ComplianceLockoutError(SIGSError):
    """Raised when simulation testing is suspended due to compliance guideline revisions (SRS BR-3)."""
