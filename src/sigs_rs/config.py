"""
Application configuration loaded from environment variables.
Uses pydantic-settings for validation and type coercion.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator, AnyUrl, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from urllib.parse import urlparse

from sigs_rs.common import constants
from sigs_rs.common.exceptions import ConfigError, ProductionDatabaseError


class Settings(BaseSettings):
    """
    All runtime configuration, sourced from environment variables or .env file.
    Validated at startup — the application refuses to boot on invalid config.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ─── Application ─────────────────────────────────────────────────────────
    app_env: str = Field(default="development")
    app_host: str = Field(default="0.0.0.0")
    app_port: int = Field(default=8000)
    debug: bool = Field(default=False)

    # ─── Database ─────────────────────────────────────────────────────────────
    database_url: str = Field(
        default="postgresql+asyncpg://sigs_user:sigs_pass@localhost:5432/sigs_db",
        description="PostgreSQL/TimescaleDB connection string.",
    )
    database_ssl_mode: str = Field(default="disable")
    # Comma-separated list of allowed DB host patterns (SRS REQ-SF-1)
    allowed_db_hosts: str = Field(default="localhost,timescaledb,127.0.0.1")

    # ─── MQTT ─────────────────────────────────────────────────────────────────
    mqtt_broker_url: str | None = Field(
        default=None,
        description="Unified MQTT broker URL (e.g. mqtt://localhost:1883 or mqtts://broker:8883).",
    )
    mqtt_broker_host: str = Field(default="localhost")
    mqtt_broker_port: int = Field(default=constants.MQTT_PORT_TLS)
    mqtt_use_tls: bool = Field(default=False)
    mqtt_insecure_skip_verify: bool = Field(default=False)
    mqtt_ca_cert_path: str = Field(default="")
    mqtt_client_cert_path: str = Field(default="")
    mqtt_client_key_path: str = Field(default="")
    mqtt_topic_prefix: str = Field(default=constants.MQTT_TOPIC_PREFIX)
    mqtt_client_id: str = Field(default="sigs-rs-gateway")

    # ─── Security Middleware ──────────────────────────────────────────────────
    inspector_window_seconds: int = Field(default=constants.WINDOW_SECONDS)
    inspector_max_packets_per_window: int = Field(default=constants.MAX_PACKETS_PER_WINDOW)
    inspector_window_cleanup_interval: int = Field(default=constants.WINDOW_CLEANUP_INTERVAL)

    # PII fields to strip — comma-separated list
    anonymizer_pii_fields: str = Field(
        default=",".join(constants.DEFAULT_PII_FIELDS)
    )

    max_payload_size_bytes: int = Field(default=constants.MAX_PAYLOAD_BYTES)

    # ─── Circuit Breaker ──────────────────────────────────────────────────────
    circuit_breaker_failure_threshold_ms: int = Field(
        default=constants.CB_FAILURE_THRESHOLD_MS
    )
    circuit_breaker_cooldown_seconds: int = Field(
        default=constants.CB_COOLDOWN_SECONDS
    )

    # ─── Authentication ───────────────────────────────────────────────────────
    jwt_secret_key: str = Field(
        default="change-me-to-a-long-random-secret-in-production"
    )
    jwt_algorithm: str = Field(default="HS256")
    jwt_access_token_expire_minutes: int = Field(default=60)

    admin_username: str = Field(default="admin")
    admin_password: str = Field(default="changeme")
    admin_role: str = Field(default=constants.Role.SECURITY_ENGINEER)

    # ─── Ingestion Queue ──────────────────────────────────────────────────────
    ingestion_queue_max_size: int = Field(default=constants.QUEUE_MAX_SIZE)

    # ─── Derived properties ───────────────────────────────────────────────────

    @property
    def pii_fields(self) -> list[str]:
        """Parse comma-separated PII field list into a Python list."""
        return [f.strip() for f in self.anonymizer_pii_fields.split(",") if f.strip()]

    # ─── Validators ───────────────────────────────────────────────────────────

    @field_validator("database_url")
    @classmethod
    def _validate_db_url_not_production(cls, v: str) -> str:
        """
        Refuse to boot if DATABASE_URL points to a production host.
        SRS REQ-SF-1: only staging/testing clusters are permitted.
        This check is intentionally simple — host-pattern matching.
        """
        # Extract host from the URL (works for postgresql+asyncpg://user:pass@host/db)
        try:
            after_at = v.split("@")[-1]           # user:pass@host:port/db → host:port/db
            host = after_at.split(":")[0].split("/")[0]
        except Exception:
            return v  # Let the DB driver surface connection errors

        blocked_keywords = ["prod", "production", "live"]
        if any(kw in host.lower() for kw in blocked_keywords):
            raise ProductionDatabaseError(
                f"DATABASE_URL host '{host}' looks like a production database. "
                "Only staging or test clusters are permitted (SRS REQ-SF-1)."
            )
        return v

    @field_validator("jwt_secret_key")
    @classmethod
    def _warn_default_secret(cls, v: str) -> str:
        """Warn (but do not block) if using the default insecure secret."""
        if v == "change-me-to-a-long-random-secret-in-production":
            import warnings
            warnings.warn(
                "JWT_SECRET_KEY is using the default insecure value. "
                "Set a strong random secret before deploying.",
                UserWarning,
                stacklevel=2,
            )
        return v

    @model_validator(mode="after")
    def _validate_mqtt_and_environment(self) -> "Settings":
        """
        Validate MQTT configuration:
        - Parses mqtt_broker_url if provided.
        - Enforces TLS 1.2+ for production mode (SRS REQ-TI-1, REQ-SE-1).
        - Allows plain MQTT only in development and testing environments.
        """
        if self.mqtt_broker_url:
            parsed = urlparse(self.mqtt_broker_url)
            if parsed.scheme in ("mqtts", "ssl"):
                self.mqtt_use_tls = True
                self.mqtt_broker_port = parsed.port or constants.MQTT_PORT_TLS
            elif parsed.scheme in ("mqtt", "tcp"):
                self.mqtt_use_tls = False
                self.mqtt_broker_port = parsed.port or 1883
            if parsed.hostname:
                self.mqtt_broker_host = parsed.hostname

        if self.app_env.lower() in ("prod", "production", "live"):
            if not self.mqtt_use_tls:
                raise ConfigError(
                    "Production configuration requires TLS 1.2+ for MQTT telemetry ingestion (SRS REQ-TI-1, REQ-SE-1)."
                )
            if self.mqtt_broker_port == 1883:
                raise ConfigError(
                    "Production configuration requires MQTT TLS port 8883 (SRS REQ-TI-1)."
                )
            if self.mqtt_insecure_skip_verify:
                raise ConfigError(
                    "Production configuration cannot disable TLS certificate verification."
                )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton."""
    return Settings()
