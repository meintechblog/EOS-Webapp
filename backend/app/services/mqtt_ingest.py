import logging
from datetime import datetime, timezone
from threading import Lock

import paho.mqtt.client as mqtt
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.repositories.mappings import EnabledMappingSnapshot, list_enabled_mapping_snapshots
from app.repositories.telemetry import create_telemetry_event
from app.services.payload_parser import parse_payload


class MqttIngestService:
    def __init__(self, *, settings: Settings, session_factory: sessionmaker):
        self._settings = settings
        self._session_factory = session_factory
        self._logger = logging.getLogger("app.mqtt_ingest")
        self._lock = Lock()
        self._connected = False
        self._subscribed_topics: set[str] = set()
        self._mapping_by_topic: dict[str, EnabledMappingSnapshot] = {}
        self._messages_received = 0
        self._last_message_ts: datetime | None = None

        self._client = mqtt.Client(client_id=self._settings.mqtt_client_id, clean_session=True)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)

    def start(self) -> None:
        self._logger.info(
            "starting mqtt service broker=%s:%s",
            self._settings.mqtt_broker_host,
            self._settings.mqtt_broker_port,
        )
        self._client.connect_async(
            host=self._settings.mqtt_broker_host,
            port=self._settings.mqtt_broker_port,
            keepalive=60,
        )
        self._client.loop_start()

    def stop(self) -> None:
        self._logger.info("stopping mqtt service")
        self._client.loop_stop()
        try:
            self._client.disconnect()
        except Exception:
            self._logger.exception("error while disconnecting mqtt client")

    def sync_subscriptions_from_db(self) -> None:
        try:
            with self._session_factory() as db:
                snapshots = list_enabled_mapping_snapshots(db)
        except Exception:
            self._logger.exception("failed to load enabled mappings for mqtt sync")
            return

        next_mapping_by_topic = {snapshot.mqtt_topic: snapshot for snapshot in snapshots}
        next_topics = set(next_mapping_by_topic)

        with self._lock:
            previous_topics = set(self._subscribed_topics)
            self._mapping_by_topic = next_mapping_by_topic
            self._subscribed_topics = next_topics
            connected = self._connected

        if not connected:
            self._logger.info(
                "mqtt subscriptions prepared while disconnected topics=%d",
                len(next_topics),
            )
            return

        topics_to_remove = sorted(previous_topics - next_topics)
        topics_to_add = sorted(next_topics - previous_topics)

        for topic in topics_to_remove:
            self._client.unsubscribe(topic)
            self._logger.info("mqtt unsubscribed topic=%s", topic)

        for topic in topics_to_add:
            result, _ = self._client.subscribe(topic, qos=self._settings.mqtt_qos)
            if result != mqtt.MQTT_ERR_SUCCESS:
                self._logger.error("mqtt subscribe failed topic=%s rc=%s", topic, result)
            else:
                self._logger.info("mqtt subscribed topic=%s", topic)

    def get_connection_status(self) -> dict[str, object]:
        with self._lock:
            connected = self._connected
            topics = sorted(self._subscribed_topics)
        return {
            "connected": connected,
            "broker_host": self._settings.mqtt_broker_host,
            "broker_port": self._settings.mqtt_broker_port,
            "client_id": self._settings.mqtt_client_id,
            "subscriptions_count": len(topics),
            "subscribed_topics": topics,
        }

    def get_telemetry_status(self) -> dict[str, object]:
        with self._lock:
            last_message_ts = self._last_message_ts
            messages_received = self._messages_received
        return {
            "messages_received": messages_received,
            "last_message_ts": last_message_ts.isoformat() if last_message_ts else None,
        }

    def _on_connect(self, client: mqtt.Client, _userdata: object, _flags: dict[str, int], rc: int) -> None:
        if rc != 0:
            self._logger.error("mqtt connect failed rc=%s", rc)
            return

        with self._lock:
            self._connected = True
            topics_to_subscribe = sorted(self._subscribed_topics)

        self._logger.info("mqtt connected topics=%d", len(topics_to_subscribe))
        for topic in topics_to_subscribe:
            result, _ = client.subscribe(topic, qos=self._settings.mqtt_qos)
            if result != mqtt.MQTT_ERR_SUCCESS:
                self._logger.error("mqtt subscribe failed topic=%s rc=%s", topic, result)
            else:
                self._logger.info("mqtt subscribed topic=%s", topic)

    def _on_disconnect(self, _client: mqtt.Client, _userdata: object, rc: int) -> None:
        with self._lock:
            self._connected = False
        self._logger.warning("mqtt disconnected rc=%s", rc)

    def _on_message(self, _client: mqtt.Client, _userdata: object, message: mqtt.MQTTMessage) -> None:
        topic = message.topic
        payload = message.payload.decode("utf-8", errors="replace")

        with self._lock:
            mapping = self._mapping_by_topic.get(topic)

        if mapping is None:
            self._logger.warning("received mqtt message for unmapped topic=%s", topic)
            return

        parsed_value = parse_payload(payload, mapping.payload_path, logger=self._logger)
        event_ts = datetime.now(timezone.utc)
        try:
            with self._session_factory() as db:
                create_telemetry_event(
                    db,
                    mapping_id=mapping.id,
                    eos_field=mapping.eos_field,
                    raw_payload=payload,
                    parsed_value=parsed_value,
                    event_ts=event_ts,
                )
        except Exception:
            self._logger.exception("failed to persist telemetry event topic=%s", topic)
            return

        with self._lock:
            self._messages_received += 1
            self._last_message_ts = event_ts

