"""
MQTT subscriber — secure telemetry ingestion for SIGS-RS.

SRS REQ-TI-1: accept secure MQTT connections exclusively on Port 8883 with TLS 1.2+.
SRS REQ-TI-2: parse incoming JSON payloads per Community Sensor Grid API schema.
SRS REQ-TI-3: reject and drop any packet exceeding 512 KB.
SRS REQ-TI-4: enqueue verified, parsed packets into in-memory queue for Inspector.
SRS REQ-SE-1: require TLS 1.2+ and support X.509 client certificate authentication (mTLS).
SRS REQ-API-5: silently discard dropped packets (no rejection acknowledgement to external client).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import ssl
from typing import TYPE_CHECKING, Any

import aiomqtt

from sigs_rs.common.constants import AuditEvent
from sigs_rs.common.exceptions import (
    MalformedPayloadError,
    PayloadTooLargeError,
    QueueFullError,
)
from sigs_rs.telemetry.models import RawTelemetryPacket
from sigs_rs.telemetry.validator import TelemetryValidator

if TYPE_CHECKING:
    from sigs_rs.config import Settings
    from sigs_rs.ingestion.buffer import IngestionQueue
    from sigs_rs.management.audit_logger import AuditLogger

logger = logging.getLogger(__name__)


def create_mqtt_tls_context(
    ca_cert_path: str | None = None,
    client_cert_path: str | None = None,
    client_key_path: str | None = None,
    insecure_skip_verify: bool = False,
) -> ssl.SSLContext:
    """
    Build SSLContext enforcing TLS 1.2+ and supporting X.509 client certs (mTLS).

    SRS REQ-TI-1: TLS 1.2 or higher.
    SRS REQ-SE-1: X.509 client certificate authentication.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2  # Enforce TLS 1.2 minimum

    # CA Certificate
    if ca_cert_path and Path(ca_cert_path).is_file():
        ctx.load_verify_locations(cafile=ca_cert_path)
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.check_hostname = not insecure_skip_verify
    elif insecure_skip_verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    else:
        ctx.load_default_certs()
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.check_hostname = True

    # Client Certificate & Key (mTLS)
    if client_cert_path and client_key_path:
        cert_file = Path(client_cert_path)
        key_file = Path(client_key_path)
        if cert_file.is_file() and key_file.is_file():
            ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
            logger.info("Loaded X.509 client certificate for mTLS: %s", cert_file.name)

    return ctx


class MQTTSubscriber:
    """
    Async MQTT subscriber that receives telemetry packets from the
    Virtual Sensor Grid and feeds them into the IngestionQueue.

    Enforces the pipeline:
    MQTT Message
         ↓
    Payload size validation (≤ 512 KB)
         ↓
    JSON/Schema validation (TelemetryValidator)
         ↓
    Internal IngestionQueue
         ↓
    Anomalous Packet Inspector
         ↓
    Privacy Anonymizer
         ↓
    Persistence
    """

    def __init__(
        self,
        settings: "Settings",
        queue: "IngestionQueue",
        validator: "TelemetryValidator | None" = None,
        audit_logger: "AuditLogger | None" = None,
        reconnect_initial_interval: float = 1.0,
        reconnect_max_interval: float = 30.0,
    ) -> None:
        self._settings = settings
        self._queue = queue
        self._validator = validator or TelemetryValidator(max_bytes=settings.max_payload_size_bytes)
        self._audit = audit_logger
        self._reconnect_initial_interval = reconnect_initial_interval
        self._reconnect_max_interval = reconnect_max_interval

        self._running: bool = False
        self._is_connected: bool = False
        self._client: aiomqtt.Client | None = None

        # Telemetry metrics
        self._packets_received: int = 0
        self._packets_enqueued: int = 0
        self._packets_dropped_size: int = 0
        self._packets_dropped_malformed: int = 0
        self._packets_dropped_queue_full: int = 0

    @property
    def is_connected(self) -> bool:
        """True if the subscriber is actively connected to the MQTT broker."""
        return self._is_connected

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def stats(self) -> dict[str, int]:
        """Subscriber performance and drop statistics."""
        return {
            "received": self._packets_received,
            "enqueued": self._packets_enqueued,
            "dropped_size": self._packets_dropped_size,
            "dropped_malformed": self._packets_dropped_malformed,
            "dropped_queue_full": self._packets_dropped_queue_full,
        }

    def _build_client_kwargs(self) -> dict[str, Any]:
        """Assemble aiomqtt.Client constructor arguments based on settings."""
        kwargs: dict[str, Any] = {
            "hostname": self._settings.mqtt_broker_host,
            "port": self._settings.mqtt_broker_port,
            "identifier": self._settings.mqtt_client_id,
        }
        if self._settings.mqtt_use_tls:
            kwargs["tls_context"] = create_mqtt_tls_context(
                ca_cert_path=self._settings.mqtt_ca_cert_path,
                client_cert_path=self._settings.mqtt_client_cert_path,
                client_key_path=self._settings.mqtt_client_key_path,
                insecure_skip_verify=self._settings.mqtt_insecure_skip_verify,
            )
        return kwargs

    async def start(self) -> None:
        """
        Connect to the MQTT broker and consume telemetry messages.
        Maintains connection with automatic reconnect and exponential backoff.
        """
        self._running = True
        backoff = self._reconnect_initial_interval

        logger.info(
            "MQTT subscriber starting: host=%s port=%d tls=%s topic=%s",
            self._settings.mqtt_broker_host,
            self._settings.mqtt_broker_port,
            self._settings.mqtt_use_tls,
            self._settings.mqtt_topic_prefix,
        )

        while self._running:
            try:
                client_kwargs = self._build_client_kwargs()
                async with aiomqtt.Client(**client_kwargs) as client:
                    self._client = client
                    self._is_connected = True
                    backoff = self._reconnect_initial_interval  # Reset backoff on successful connect
                    logger.info(
                        "Connected to MQTT broker at %s:%d (TLS=%s)",
                        self._settings.mqtt_broker_host,
                        self._settings.mqtt_broker_port,
                        self._settings.mqtt_use_tls,
                    )

                    await client.subscribe(self._settings.mqtt_topic_prefix)
                    logger.info("Subscribed to MQTT topic: %s", self._settings.mqtt_topic_prefix)

                    async for message in client.messages:
                        if not self._running:
                            break
                        await self.handle_message(message)

            except asyncio.CancelledError:
                logger.info("MQTT subscriber task cancelled.")
                break
            except Exception as exc:
                self._is_connected = False
                self._client = None
                if not self._running:
                    break
                logger.warning(
                    "MQTT broker connection error (%s:%d): %s. Reconnecting in %.1fs...",
                    self._settings.mqtt_broker_host,
                    self._settings.mqtt_broker_port,
                    exc,
                    backoff,
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    break
                backoff = min(backoff * 2, self._reconnect_max_interval)

        self._is_connected = False
        self._client = None
        logger.info("MQTT subscriber stopped.")

    async def stop(self) -> None:
        """Gracefully disconnect and stop the MQTT subscriber."""
        self._running = False
        self._is_connected = False
        logger.info("MQTT subscriber stopping.")

    async def handle_message(self, message: Any) -> bool:
        """
        Process a single incoming MQTT message through size gate and validator,
        then enqueue into the IngestionQueue.

        Silent drop on failure (SRS REQ-API-5): No rejection acknowledgement
        is returned to the client or published to the broker.
        """
        self._packets_received += 1
        topic = str(getattr(message, "topic", "unknown"))
        payload = getattr(message, "payload", b"")

        if isinstance(payload, str):
            raw_bytes = payload.encode("utf-8")
        elif isinstance(payload, (bytes, bytearray, memoryview)):
            raw_bytes = bytes(payload)
        else:
            raw_bytes = b""

        # 1. Payload size enforcement (SRS REQ-TI-3: 512 KB)
        if len(raw_bytes) > self._settings.max_payload_size_bytes:
            self._packets_dropped_size += 1
            logger.warning(
                "MQTT packet dropped: oversized payload (%d bytes > %d bytes limit) on topic %s",
                len(raw_bytes),
                self._settings.max_payload_size_bytes,
                topic,
            )
            if self._audit:
                await self._audit.log(
                    AuditEvent.PACKET_DROPPED_OVERSIZED,
                    severity="WARN",
                    detail={"topic": topic, "size_bytes": len(raw_bytes)},
                )
            return False  # Silent drop

        # 2. JSON & Schema validation (SRS REQ-TI-2)
        try:
            packet = self._validator.validate_bytes(raw_bytes)
        except (MalformedPayloadError, PayloadTooLargeError) as exc:
            self._packets_dropped_malformed += 1
            logger.warning(
                "MQTT packet dropped: schema validation failed on topic %s: %s",
                topic,
                exc,
            )
            if self._audit:
                await self._audit.log(
                    AuditEvent.PACKET_DROPPED_MALFORMED,
                    severity="WARN",
                    detail={"topic": topic, "reason": str(exc)},
                )
            return False  # Silent drop
        except Exception as exc:
            self._packets_dropped_malformed += 1
            logger.warning("MQTT packet dropped: unexpected error on topic %s: %s", topic, exc)
            return False  # Silent drop

        # 3. Internal queue enqueueing (SRS REQ-TI-4)
        try:
            await self._queue.put(packet)
            self._packets_enqueued += 1
            logger.debug(
                "MQTT packet enqueued: sensor=%s resource=%s depth=%d",
                packet.sensor_id,
                packet.resource_type,
                self._queue.depth,
            )
            return True
        except QueueFullError:
            self._packets_dropped_queue_full += 1
            logger.warning(
                "MQTT packet dropped: ingestion queue full (backpressure) for sensor %s",
                packet.sensor_id,
            )
            return False

    def _parse(self, raw: bytes) -> RawTelemetryPacket:
        """
        Validate raw message bytes into a RawTelemetryPacket.
        Preserved for backwards compatibility with tests and callers.
        """
        if len(raw) > self._settings.max_payload_size_bytes:
            raise PayloadTooLargeError(
                f"MQTT payload {len(raw)} bytes exceeds {self._settings.max_payload_size_bytes} byte limit."
            )
        return self._validator.validate_bytes(raw)
