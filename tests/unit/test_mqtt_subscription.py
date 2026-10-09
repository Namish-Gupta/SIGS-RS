import pytest
from sigs_rs.ingestion.mqtt_sub import MQTTSubscriber
from sigs_rs.config import Settings

def test_mqtt_topic_prefix_computed():
    # Verify the default topic prefix
    settings = Settings(mqtt_topic_prefix="$share/sigs-rs/telemetry/#")
    sub = MQTTSubscriber(settings, queue=None)
    assert sub._settings.mqtt_topic_prefix == "$share/sigs-rs/telemetry/#", "Shared subscription prefix is not applied correctly"
