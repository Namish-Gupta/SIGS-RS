"""
Telemetry generator for the Virtual Sensor Grid Simulator.

Generates realistic JSON telemetry for water, energy, and air quality sensors,
as well as anomalous (impossible metric values) and malformed (schema/size violations) payloads.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import random
from typing import Any

from simulator.models import TrafficMode


VALID_RESOURCE_TYPES = ("water", "energy", "air_quality")

STANDARD_UNITS = {
    "water": "liters_per_min",
    "energy": "kWh",
    "air_quality": "AQI",
}

RESOURCE_COORDINATES = {
    "water": {"lat": 12.9716, "lon": 80.2209},      # Chennai region
    "energy": {"lat": 28.6139, "lon": 77.2090},     # Delhi region
    "air_quality": {"lat": 19.0760, "lon": 72.8777}, # Mumbai region
}


@dataclass
class VirtualSensor:
    """Internal state representation of a single simulated IoT sensor."""
    sensor_id: str
    resource_type: str
    mac_address: str
    firmware_version: str
    latitude: float
    longitude: float
    battery_level: int = 100
    base_value: float = 20.0


@dataclass
class GeneratedPacket:
    """A generated telemetry packet ready for publishing."""
    payload: bytes | str
    sensor_id: str
    resource_type: str
    is_malformed: bool
    is_malicious: bool
    topic_suffix: str


class TelemetryGenerator:
    """
    Stateful and reproducible generator of IoT telemetry packets.
    """

    def __init__(
        self,
        sensor_count: int = 50,
        resource_types: list[str] | None = None,
        seed: int | None = None,
    ) -> None:
        self._rng = random.Random(seed)
        self._resource_types = [
            r for r in (resource_types or list(VALID_RESOURCE_TYPES))
            if r in VALID_RESOURCE_TYPES
        ]
        if not self._resource_types:
            self._resource_types = list(VALID_RESOURCE_TYPES)

        self._sensor_count = max(sensor_count, 1)
        self._sensors: list[VirtualSensor] = []
        self._init_sensor_pool()

    def _init_sensor_pool(self) -> None:
        """Initialize the population of virtual sensors."""
        for i in range(self._sensor_count):
            resource = self._resource_types[i % len(self._resource_types)]
            base_coords = RESOURCE_COORDINATES[resource]
            
            # Small jitter for geographic dispersion (±0.05 degrees ~ 5km)
            lat = base_coords["lat"] + self._rng.uniform(-0.05, 0.05)
            lon = base_coords["lon"] + self._rng.uniform(-0.05, 0.05)
            
            # Realistic MAC address
            mac = ":".join(f"{self._rng.randint(0, 255):02X}" for _ in range(6))
            firmware = f"{self._rng.randint(1, 3)}.{self._rng.randint(0, 5)}.{self._rng.randint(0, 9)}"
            battery = self._rng.randint(65, 100)
            
            if resource == "water":
                base_val = self._rng.uniform(15.0, 60.0)
            elif resource == "energy":
                base_val = self._rng.uniform(2.0, 25.0)
            else:  # air_quality
                base_val = self._rng.uniform(40.0, 180.0)

            sensor = VirtualSensor(
                sensor_id=f"sensor-{resource}-{i+1:04d}",
                resource_type=resource,
                mac_address=mac,
                firmware_version=firmware,
                latitude=round(lat, 6),
                longitude=round(lon, 6),
                battery_level=battery,
                base_value=base_val,
            )
            self._sensors.append(sensor)

    @property
    def sensors(self) -> list[VirtualSensor]:
        return self._sensors

    def get_sensor(self, index: int | None = None) -> VirtualSensor:
        if index is not None and 0 <= index < len(self._sensors):
            return self._sensors[index]
        return self._rng.choice(self._sensors)

    def generate_packet(
        self,
        sensor: VirtualSensor | None = None,
        force_anomaly: bool = False,
        force_malformed: bool = False,
    ) -> GeneratedPacket:
        """
        Generate a single telemetry packet.
        """
        if sensor is None:
            sensor = self.get_sensor()

        if force_malformed:
            return self._generate_malformed_packet(sensor)
        elif force_anomaly:
            return self._generate_anomalous_packet(sensor)
        else:
            return self._generate_normal_packet(sensor)

    def _generate_normal_packet(self, sensor: VirtualSensor) -> GeneratedPacket:
        """Generate a realistic, compliant telemetry packet."""
        resource = sensor.resource_type
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        unit = STANDARD_UNITS[resource]

        # Natural fluctuation around base value
        if resource == "water":
            # Flow rate (L/min)
            val = round(max(0.5, sensor.base_value + self._rng.uniform(-5.0, 5.0)), 2)
            ext: dict[str, Any] = {
                "pressure_bar": round(self._rng.uniform(1.8, 3.5), 2),
                "ph_level": round(self._rng.uniform(6.8, 7.6), 2),
                "temperature_celsius": round(self._rng.uniform(16.0, 24.0), 1),
                "turbidity_ntu": round(self._rng.uniform(0.1, 1.2), 2),
            }
            ext_key = "water_ext"
        elif resource == "energy":
            # Energy consumption (kWh)
            val = round(max(0.1, sensor.base_value + self._rng.uniform(-1.5, 1.5)), 2)
            ext = {
                "voltage_v": round(self._rng.uniform(225.0, 235.0), 1),
                "current_a": round(self._rng.uniform(5.0, 20.0), 2),
                "power_factor": round(self._rng.uniform(0.91, 0.99), 2),
                "frequency_hz": round(self._rng.uniform(49.9, 50.1), 2),
            }
            ext_key = "energy_ext"
        else:  # air_quality
            # Air Quality Index (AQI 0-500)
            val = round(max(10.0, min(450.0, sensor.base_value + self._rng.uniform(-15.0, 15.0))), 1)
            ext = {
                "pm25_ug_m3": round(self._rng.uniform(10.0, 65.0), 1),
                "pm10_ug_m3": round(self._rng.uniform(20.0, 95.0), 1),
                "co2_ppm": round(self._rng.uniform(390.0, 650.0), 1),
                "o3_ppb": round(self._rng.uniform(15.0, 45.0), 1),
                "temperature_celsius": round(self._rng.uniform(22.0, 32.0), 1),
                "humidity_percent": round(self._rng.uniform(40.0, 75.0), 1),
            }
            ext_key = "air_quality_ext"

        # Slight battery discharge simulation
        if self._rng.random() < 0.02 and sensor.battery_level > 5:
            sensor.battery_level -= 1

        payload_dict = {
            "sensor_id": sensor.sensor_id,
            "timestamp": now_iso,
            "resource_type": resource,
            "value": val,
            "unit": unit,
            "metadata": {
                "battery_level": sensor.battery_level,
                "firmware_version": sensor.firmware_version,
                "signal_strength_dbm": self._rng.randint(-85, -50),
            },
            ext_key: ext,
            "geo": {
                "latitude": sensor.latitude,
                "longitude": sensor.longitude,
            },
            "mac_address": sensor.mac_address,
        }

        payload_bytes = json.dumps(payload_dict).encode("utf-8")
        topic_suffix = f"{resource}/{sensor.sensor_id}"

        return GeneratedPacket(
            payload=payload_bytes,
            sensor_id=sensor.sensor_id,
            resource_type=resource,
            is_malformed=False,
            is_malicious=False,
            topic_suffix=topic_suffix,
        )

    def _generate_anomalous_packet(self, sensor: VirtualSensor) -> GeneratedPacket:
        """
        Generate an anomalous packet with impossible metric values (SRS REQ-API-3).
        Enforced by AnomalousPacketInspector.
        """
        packet = self._generate_normal_packet(sensor)
        data = json.loads(packet.payload.decode("utf-8"))
        resource = sensor.resource_type

        anomaly_kind = self._rng.choice(["impossible_value", "extreme_value", "future_timestamp", "bad_battery"])

        if anomaly_kind == "impossible_value":
            if resource == "water":
                # Negative water is physically impossible
                data["value"] = -self._rng.uniform(10.0, 999.0)
            elif resource == "energy":
                # Negative energy consumption is impossible
                data["value"] = -self._rng.uniform(5.0, 500.0)
            else:  # air_quality
                # Negative AQI or AQI > 500 is impossible
                data["value"] = self._rng.choice([-10.0, self._rng.uniform(501.0, 9999.0)])
        elif anomaly_kind == "extreme_value":
            if resource == "air_quality":
                data["value"] = round(self._rng.uniform(501.0, 2000.0), 1)
            else:
                data["value"] = -9999.0
        elif anomaly_kind == "future_timestamp":
            # Timestamp far into future exceeding the 5 minute tolerance
            future_dt = datetime.now(timezone.utc) + timedelta(minutes=60)
            data["timestamp"] = future_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        elif anomaly_kind == "bad_battery":
            data["metadata"]["battery_level"] = self._rng.choice([-10, 150])

        payload_bytes = json.dumps(data).encode("utf-8")
        return GeneratedPacket(
            payload=payload_bytes,
            sensor_id=sensor.sensor_id,
            resource_type=resource,
            is_malformed=False,
            is_malicious=True,
            topic_suffix=packet.topic_suffix,
        )

    def _generate_malformed_packet(self, sensor: VirtualSensor) -> GeneratedPacket:
        """
        Generate a malformed packet violating schema or size limits (SRS REQ-TI-2 / REQ-TI-3).
        Enforced by TelemetryValidator and size gates.
        """
        resource = sensor.resource_type
        malformed_kind = self._rng.choice([
            "bad_json",
            "missing_field",
            "wrong_type",
            "invalid_unit",
            "oversized_payload",
            "unknown_resource",
        ])

        if malformed_kind == "bad_json":
            payload: bytes | str = b'{"sensor_id": "' + sensor.sensor_id.encode() + b'", "broken_json": [1, 2, '
        elif malformed_kind == "missing_field":
            # Missing sensor_id or timestamp or value or unit
            normal = self._generate_normal_packet(sensor)
            data = json.loads(normal.payload.decode("utf-8"))
            field_to_drop = self._rng.choice(["sensor_id", "timestamp", "value", "unit", "resource_type"])
            data.pop(field_to_drop, None)
            payload = json.dumps(data).encode("utf-8")
        elif malformed_kind == "wrong_type":
            normal = self._generate_normal_packet(sensor)
            data = json.loads(normal.payload.decode("utf-8"))
            data["value"] = "NaN_not_a_float"
            payload = json.dumps(data).encode("utf-8")
        elif malformed_kind == "invalid_unit":
            normal = self._generate_normal_packet(sensor)
            data = json.loads(normal.payload.decode("utf-8"))
            data["unit"] = "invalid_custom_unit_xyz"
            payload = json.dumps(data).encode("utf-8")
        elif malformed_kind == "unknown_resource":
            normal = self._generate_normal_packet(sensor)
            data = json.loads(normal.payload.decode("utf-8"))
            data["resource_type"] = "radiation_gamma"
            payload = json.dumps(data).encode("utf-8")
        elif malformed_kind == "oversized_payload":
            # Over 512 KB payload size limit (SRS REQ-TI-3)
            normal = self._generate_normal_packet(sensor)
            data = json.loads(normal.payload.decode("utf-8"))
            # Inject 525 KB padding
            data["metadata"]["oversized_junk"] = "X" * (525 * 1024)
            payload = json.dumps(data).encode("utf-8")
        else:
            payload = b"MALFORMED_RAW_BYTES\x00\xff"

        return GeneratedPacket(
            payload=payload,
            sensor_id=sensor.sensor_id,
            resource_type=resource,
            is_malformed=True,
            is_malicious=False,
            topic_suffix=f"{resource}/{sensor.sensor_id}",
        )
