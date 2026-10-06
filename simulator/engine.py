"""
Simulator engine for orchestrating virtual IoT sensor traffic and MQTT publishing.

Supports all traffic modes: NORMAL, BURST, RATE_ATTACK, INVALID_METRICS, MALFORMED_PAYLOAD, MIXED.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import random
import ssl
import time
from typing import Any, Callable

import aiomqtt

from simulator.generator import GeneratedPacket, TelemetryGenerator, VirtualSensor
from simulator.models import SimulationStats, SimulatorConfig, TrafficMode

logger = logging.getLogger("simulator")


def create_tls_context(
    ca_cert_path: str | None = None,
    client_cert_path: str | None = None,
    client_key_path: str | None = None,
    insecure_skip_verify: bool = False,
) -> ssl.SSLContext:
    """Build a standard TLS 1.2+ SSLContext for MQTT client connection."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2

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

    if client_cert_path and client_key_path:
        cert_file = Path(client_cert_path)
        key_file = Path(client_key_path)
        if cert_file.is_file() and key_file.is_file():
            ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))

    return ctx


class Simulator:
    """
    Virtual Sensor Grid Simulator engine.
    """

    def __init__(self, config: SimulatorConfig) -> None:
        self.config = config
        self.generator = TelemetryGenerator(
            sensor_count=config.sensors,
            resource_types=config.resource_types,
            seed=config.seed,
        )
        self.stats = SimulationStats()
        self._rng = random.Random(config.seed)
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        """Signal simulator to halt."""
        self._stop_event.set()

    def _determine_packet_nature(
        self,
        mode: TrafficMode,
        is_attack_sensor: bool = False,
    ) -> tuple[bool, bool]:
        """
        Determine whether the next packet should be anomalous or malformed.
        Returns: (force_anomaly, force_malformed)
        """
        if is_attack_sensor and mode == TrafficMode.RATE_ATTACK:
            # Rate attack packets are structurally valid by default to test rate-limiter,
            # but flagged as malicious.
            return (False, False)

        if mode == TrafficMode.INVALID_METRICS:
            return (True, False)
        elif mode == TrafficMode.MALFORMED_PAYLOAD:
            return (False, True)
        elif mode == TrafficMode.NORMAL or mode == TrafficMode.BURST:
            rnd = self._rng.random() * 100.0
            anomaly_threshold = self.config.anomaly_percent
            malformed_threshold = anomaly_threshold + self.config.malformed_percent
            if rnd < anomaly_threshold:
                return (True, False)
            elif rnd < malformed_threshold:
                return (False, True)
            return (False, False)
        elif mode == TrafficMode.MIXED:
            rnd = self._rng.random() * 100.0
            # Default mixed ratios if not customized: 10% anomalies, 5% malformed
            a_rate = self.config.anomaly_percent if self.config.anomaly_percent > 0 else 10.0
            m_rate = self.config.malformed_percent if self.config.malformed_percent > 0 else 5.0
            if rnd < a_rate:
                return (True, False)
            elif rnd < (a_rate + m_rate):
                return (False, True)
            return (False, False)

        return (False, False)

    async def run(
        self,
        progress_callback: Callable[[SimulationStats], None] | None = None,
    ) -> SimulationStats:
        """
        Run the simulation according to config.
        """
        self.stats = SimulationStats(start_time=time.monotonic())
        self._stop_event.clear()

        logger.info(
            "Starting Virtual Sensor Grid Simulator: mode=%s, sensors=%d, target_rate=%.1f pkt/s, duration=%.1fs, dry_run=%s",
            self.config.traffic_mode.value,
            self.config.sensors,
            self.config.packets_per_sec,
            self.config.duration_seconds,
            self.config.dry_run,
        )

        if self.config.dry_run:
            await self._run_loop(client=None, progress_callback=progress_callback)
        else:
            await self._run_with_mqtt(progress_callback=progress_callback)

        self.stats.end_time = time.monotonic()
        logger.info(
            "Simulation finished: %d packets generated, %d sent in %.2fs (%.1f pkt/s)",
            self.stats.packets_generated,
            self.stats.packets_sent,
            self.stats.elapsed_seconds,
            self.stats.packets_per_sec,
        )
        return self.stats

    async def _run_with_mqtt(
        self,
        progress_callback: Callable[[SimulationStats], None] | None = None,
    ) -> None:
        """Connect to MQTT broker and run simulation loop."""
        client_kwargs: dict[str, Any] = {
            "hostname": self.config.broker_host,
            "port": self.config.broker_port,
            "identifier": f"sigs-simulator-{self._rng.randint(1000, 9999)}",
        }

        if self.config.broker_username:
            client_kwargs["username"] = self.config.broker_username
        if self.config.broker_password:
            client_kwargs["password"] = self.config.broker_password

        if self.config.use_tls:
            client_kwargs["tls_context"] = create_tls_context(
                ca_cert_path=self.config.ca_cert_path,
                client_cert_path=self.config.client_cert_path,
                client_key_path=self.config.client_key_path,
                insecure_skip_verify=self.config.insecure_skip_verify,
            )

        try:
            async with aiomqtt.Client(**client_kwargs) as client:
                logger.info(
                    "Connected to MQTT broker at %s:%d (TLS=%s)",
                    self.config.broker_host,
                    self.config.broker_port,
                    self.config.use_tls,
                )
                await self._run_loop(client=client, progress_callback=progress_callback)
        except Exception as exc:
            logger.error("MQTT connection failed (%s:%d): %s", self.config.broker_host, self.config.broker_port, exc)
            self.stats.packets_failed += 1
            raise

    async def _run_loop(
        self,
        client: aiomqtt.Client | None,
        progress_callback: Callable[[SimulationStats], None] | None = None,
    ) -> None:
        """Core asynchronous packet dispatch and pacing loop."""
        start_time = time.monotonic()
        duration = self.config.duration_seconds
        target_pps = max(self.config.packets_per_sec, 0.1)
        mode = self.config.traffic_mode

        # Set up attack target sensor if in RATE_ATTACK or MIXED
        attack_sensor: VirtualSensor | None = None
        if mode in (TrafficMode.RATE_ATTACK, TrafficMode.MIXED):
            if self.config.rate_attack_target:
                # Custom specified attack target
                attack_sensor = VirtualSensor(
                    sensor_id=self.config.rate_attack_target,
                    resource_type=self.config.resource_types[0],
                    mac_address="DE:AD:BE:EF:66:66",
                    firmware_version="1.0.0",
                    latitude=12.9716,
                    longitude=80.2209,
                )
            else:
                attack_sensor = self.generator.get_sensor(0)

        last_progress_time = start_time
        rate_attack_executed = False

        while not self._stop_event.is_set():
            now = time.monotonic()
            elapsed = now - start_time

            if duration > 0 and elapsed >= duration:
                break

            # Handle Traffic Modes:
            if mode == TrafficMode.RATE_ATTACK and not rate_attack_executed:
                # Intentionally exceed 50 packets / 10 seconds for the attack sensor
                await self._execute_rate_attack(
                    client=client,
                    target_sensor=attack_sensor or self.generator.get_sensor(0),
                    count=self.config.rate_attack_burst_count,
                )
                rate_attack_executed = True
                continue

            elif mode == TrafficMode.MIXED and not rate_attack_executed and elapsed >= 1.0:
                # In MIXED mode, inject the rate attack midway through the run
                await self._execute_rate_attack(
                    client=client,
                    target_sensor=attack_sensor or self.generator.get_sensor(0),
                    count=self.config.rate_attack_burst_count,
                )
                rate_attack_executed = True
                continue

            # Determine rate multiplier for BURST mode
            current_pps = target_pps
            if mode in (TrafficMode.BURST, TrafficMode.MIXED):
                cycle = elapsed % self.config.burst_interval
                # In burst window (first 0.5s of every cycle)
                if cycle < 0.5:
                    current_pps = target_pps * self.config.burst_multiplier

            # Select sensor
            sensor = self.generator.get_sensor()
            force_anomaly, force_malformed = self._determine_packet_nature(mode)

            packet = self.generator.generate_packet(
                sensor=sensor,
                force_anomaly=force_anomaly,
                force_malformed=force_malformed,
            )

            await self._dispatch_packet(client, packet)

            # Periodic progress reporting
            if now - last_progress_time >= 2.0:
                last_progress_time = now
                if progress_callback:
                    progress_callback(self.stats)
                else:
                    logger.info(
                        "Progress: %d packets sent (%.1f pkt/s, malformed=%d, malicious=%d)",
                        self.stats.packets_sent or self.stats.packets_generated,
                        self.stats.packets_per_sec,
                        self.stats.malformed_packets,
                        self.stats.malicious_packets,
                    )

            # Pacing sleep to respect current_pps
            interval = 1.0 / current_pps
            await asyncio.sleep(interval)

    async def _execute_rate_attack(
        self,
        client: aiomqtt.Client | None,
        target_sensor: VirtualSensor,
        count: int = 60,
    ) -> None:
        """
        Send a rapid flurry of packets for a single sensor exceeding
        the 50 packets / 10 seconds sliding window threshold.
        """
        logger.warning(
            "Executing RATE_ATTACK: firing %d packets rapidly for sensor %s (exceeding 50 pkts/10s threshold)",
            count,
            target_sensor.sensor_id,
        )

        for _ in range(count):
            packet = self.generator.generate_packet(
                sensor=target_sensor,
                force_anomaly=False,
                force_malformed=False,
            )
            # Mark packet as intentionally malicious rate-attack packet
            packet.is_malicious = True

            await self._dispatch_packet(client, packet)
            # Very small sleep between attack packets (e.g., 0.01s = 100 pkt/s)
            await asyncio.sleep(0.01)

    async def _dispatch_packet(
        self,
        client: aiomqtt.Client | None,
        packet: GeneratedPacket,
    ) -> None:
        """Track statistics and publish packet to MQTT if client connected."""
        self.stats.packets_generated += 1
        if packet.is_malformed:
            self.stats.malformed_packets += 1
        if packet.is_malicious:
            self.stats.malicious_packets += 1

        topic = self.config.topic
        if self.config.topic_template:
            topic = f"{self.config.topic.rstrip('/')}/{packet.topic_suffix}"

        if client is not None:
            try:
                await client.publish(topic, payload=packet.payload, qos=0)
                self.stats.packets_sent += 1
            except Exception as exc:
                self.stats.packets_failed += 1
                logger.debug("Failed to publish packet to topic %s: %s", topic, exc)
        else:
            # Dry-run mode counts successfully generated packets as sent
            self.stats.packets_sent += 1
