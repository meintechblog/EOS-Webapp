from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from threading import Lock
from typing import Any

import paho.mqtt.client as mqtt
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.repositories.input_channels import list_input_channels
from app.repositories.mappings import EnabledMappingSnapshot, list_enabled_mapping_snapshots
from app.repositories.parameter_bindings import list_enabled_parameter_binding_snapshots
from app.services.input_ingest import InputIngestPipelineService
from app.services.parameter_dynamic_ingest import ParameterDynamicIngestService


class _MqttWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        channel_id: int,
        channel_code: str,
        config_json: dict[str, Any],
        ingest_pipeline: InputIngestPipelineService,
        parameter_dynamic_ingest_service: ParameterDynamicIngestService | None,
        on_message_callback,
    ) -> None:
        self.channel_id = channel_id
        self.channel_code = channel_code
        self._settings = settings
        self._ingest_pipeline = ingest_pipeline
        self._parameter_dynamic_ingest_service = parameter_dynamic_ingest_service
        self._on_message_callback = on_message_callback
        self._logger = logging.getLogger(f"app.mqtt_ingest.worker.{channel_code}")
        self._lock = Lock()

        self._channel_state: dict[str, Any] = {
            "id": channel_id,
            "code": channel_code,
            "channel_type": "mqtt",
            "enabled": True,
            "config_json": dict(config_json or {}),
        }

        self._host = _config_str(config_json, "host") or settings.mqtt_broker_host
        self._port = _config_int(config_json, "port", settings.mqtt_broker_port)
        self._qos = _config_int(config_json, "qos", settings.mqtt_qos)
        self._discovery_topic = _config_str(config_json, "discovery_topic") or settings.mqtt_discovery_topic
        self._client_id = (
            _config_str(config_json, "client_id")
            or f"{settings.mqtt_client_id}-{channel_code}"
        )
        self._username = _config_str(config_json, "username")
        self._password = _config_str(config_json, "password")

        self._mapping_by_topic: dict[str, EnabledMappingSnapshot] = {}
        self._subscribed_topics: set[str] = set()
        self._connected = False

        self._client = mqtt.Client(client_id=self._client_id, clean_session=True)
        if self._username is not None:
            self._client.username_pw_set(self._username, self._password)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)

    def start(self) -> None:
        self._logger.info(
            "starting mqtt worker channel=%s broker=%s:%s discovery=%s",
            self.channel_code,
            self._host,
            self._port,
            self._discovery_topic,
        )
        self._client.connect_async(host=self._host, port=self._port, keepalive=60)
        self._client.loop_start()

    def stop(self) -> None:
        self._logger.info("stopping mqtt worker channel=%s", self.channel_code)
        self._client.loop_stop()
        try:
            self._client.disconnect()
        except Exception:
            self._logger.exception("mqtt worker disconnect failed channel=%s", self.channel_code)

    def replace_topics(
        self,
        mapping_snapshots: list[EnabledMappingSnapshot],
        parameter_topics: list[str] | None = None,
    ) -> None:
        topics = {snapshot.mqtt_topic for snapshot in mapping_snapshots}
        for topic in parameter_topics or []:
            if topic.strip() != "":
                topics.add(topic)
        mapping_by_topic = {snapshot.mqtt_topic: snapshot for snapshot in mapping_snapshots}

        with self._lock:
            previous_topics = set(self._subscribed_topics)
            self._subscribed_topics = topics
            self._mapping_by_topic = mapping_by_topic
            connected = self._connected

        if not connected:
            return

        for topic in sorted(previous_topics - topics):
            self._client.unsubscribe(topic)
            self._logger.info("mqtt unsubscribed channel=%s topic=%s", self.channel_code, topic)

        for topic in sorted(topics - previous_topics):
            result, _mid = self._client.subscribe(topic, qos=self._qos)
            if result != mqtt.MQTT_ERR_SUCCESS:
                self._logger.error(
                    "mqtt subscribe failed channel=%s topic=%s rc=%s",
                    self.channel_code,
                    topic,
                    result,
                )
            else:
                self._logger.info("mqtt subscribed channel=%s topic=%s", self.channel_code, topic)

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "channel_id": self.channel_id,
                "channel_code": self.channel_code,
                "connected": self._connected,
                "broker_host": self._host,
                "broker_port": self._port,
                "client_id": self._client_id,
                "discovery_topic": self._discovery_topic,
                "subscriptions_count": len(self._subscribed_topics),
                "subscribed_topics": sorted(self._subscribed_topics),
            }

    def _on_connect(self, client: mqtt.Client, _userdata: object, _flags: dict[str, int], rc: int) -> None:
        if rc != 0:
            self._logger.error("mqtt connect failed channel=%s rc=%s", self.channel_code, rc)
            return

        with self._lock:
            self._connected = True
            topics = sorted(self._subscribed_topics)

        self._logger.info(
            "mqtt connected channel=%s mapping_topics=%s",
            self.channel_code,
            len(topics),
        )

        discovery_result, _mid = client.subscribe(self._discovery_topic, qos=self._qos)
        if discovery_result != mqtt.MQTT_ERR_SUCCESS:
            self._logger.error(
                "mqtt discovery subscribe failed channel=%s topic=%s rc=%s",
                self.channel_code,
                self._discovery_topic,
                discovery_result,
            )

        for topic in topics:
            result, _mid = client.subscribe(topic, qos=self._qos)
            if result != mqtt.MQTT_ERR_SUCCESS:
                self._logger.error(
                    "mqtt subscribe failed channel=%s topic=%s rc=%s",
                    self.channel_code,
                    topic,
                    result,
                )

    def _on_disconnect(self, _client: mqtt.Client, _userdata: object, rc: int) -> None:
        with self._lock:
            self._connected = False
        self._logger.warning("mqtt disconnected channel=%s rc=%s", self.channel_code, rc)

    def _on_message(self, _client: mqtt.Client, _userdata: object, message: mqtt.MQTTMessage) -> None:
        event_received_ts = datetime.now(timezone.utc)
        topic = message.topic
        payload = message.payload.decode("utf-8", errors="replace")

        try:
            result = self._ingest_pipeline.ingest(
                channel=_ChannelProxy(
                    id=self.channel_id,
                    code=self.channel_code,
                    channel_type="mqtt",
                ),
                input_key=topic,
                payload_text=payload,
                event_received_ts=event_received_ts,
                metadata={
                    "source": "mqtt",
                    "retain": bool(message.retain),
                    "qos": int(message.qos),
                    "channel_code": self.channel_code,
                },
            )
        except Exception:
            self._logger.exception(
                "mqtt ingest failed channel=%s topic=%s",
                self.channel_code,
                topic,
            )
            return

        if (
            self._parameter_dynamic_ingest_service is not None
            and self._settings.param_dynamic_allow_mqtt
            and topic.startswith("eos/param/")
        ):
            try:
                self._parameter_dynamic_ingest_service.ingest(
                    channel=_ChannelProxy(
                        id=self.channel_id,
                        code=self.channel_code,
                        channel_type="mqtt",
                    ),
                    input_key=topic,
                    payload_text=payload,
                    event_received_ts=event_received_ts,
                    metadata={
                        "source": "mqtt",
                        "retain": bool(message.retain),
                        "qos": int(message.qos),
                        "channel_code": self.channel_code,
                    },
                )
            except Exception:
                self._logger.exception(
                    "mqtt dynamic parameter ingest failed channel=%s topic=%s",
                    self.channel_code,
                    topic,
                )

        self._on_message_callback(
            channel_code=self.channel_code,
            event_received_ts=event_received_ts,
            normalized_key=result.normalized_key,
        )


class _ChannelProxy:
    def __init__(self, *, id: int, code: str, channel_type: str):
        self.id = id
        self.code = code
        self.channel_type = channel_type


class MqttIngestService:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: sessionmaker,
        ingest_pipeline: InputIngestPipelineService,
        parameter_dynamic_ingest_service: ParameterDynamicIngestService | None = None,
    ):
        self._settings = settings
        self._session_factory = session_factory
        self._ingest_pipeline = ingest_pipeline
        self._parameter_dynamic_ingest_service = parameter_dynamic_ingest_service
        self._logger = logging.getLogger("app.mqtt_ingest")
        self._lock = Lock()

        self._workers: dict[int, _MqttWorker] = {}
        self._messages_received = 0
        self._last_message_ts: datetime | None = None
        self._seen_discovery_keys: set[tuple[str, str]] = set()
        self._observed_topics_count = 0
        self._last_discovery_ts: datetime | None = None

        self._output_client = mqtt.Client(client_id=f"{settings.mqtt_client_id}-output", clean_session=True)
        self._output_client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._output_started = False

    def start(self) -> None:
        self._start_output_client()
        self.sync_subscriptions_from_db()

    def stop(self) -> None:
        with self._lock:
            workers = list(self._workers.values())
            self._workers = {}
        for worker in workers:
            worker.stop()
        self._stop_output_client()

    def sync_subscriptions_from_db(self) -> None:
        with self._session_factory() as db:
            mqtt_channels = [
                channel
                for channel in list_input_channels(db, channel_type="mqtt")
                if channel.enabled
            ]
            mapping_snapshots = list_enabled_mapping_snapshots(db, channel_type="mqtt")
            parameter_binding_snapshots = list_enabled_parameter_binding_snapshots(
                db,
                channel_type="mqtt",
            )

        snapshots_by_channel: dict[int, list[EnabledMappingSnapshot]] = {}
        for snapshot in mapping_snapshots:
            snapshots_by_channel.setdefault(snapshot.channel_id, []).append(snapshot)
        parameter_topics_by_channel: dict[int, list[str]] = {}
        for snapshot in parameter_binding_snapshots:
            parameter_topics_by_channel.setdefault(snapshot.channel_id, []).append(snapshot.input_key)

        next_workers: dict[int, _MqttWorker] = {}
        for channel in mqtt_channels:
            worker = _MqttWorker(
                settings=self._settings,
                channel_id=channel.id,
                channel_code=channel.code,
                config_json=dict(channel.config_json or {}),
                ingest_pipeline=self._ingest_pipeline,
                parameter_dynamic_ingest_service=self._parameter_dynamic_ingest_service,
                on_message_callback=self._handle_worker_message,
            )
            worker.replace_topics(
                snapshots_by_channel.get(channel.id, []),
                parameter_topics=parameter_topics_by_channel.get(channel.id, []),
            )
            next_workers[channel.id] = worker

        with self._lock:
            previous_workers = list(self._workers.values())
            self._workers = next_workers

        for worker in previous_workers:
            worker.stop()
        for worker in next_workers.values():
            worker.start()

    def get_connection_status(self) -> dict[str, object]:
        with self._lock:
            worker_status = [worker.get_status() for worker in self._workers.values()]

        worker_status_sorted = sorted(worker_status, key=lambda item: (str(item["channel_code"]), int(item["channel_id"])))
        connected_count = sum(1 for status in worker_status_sorted if status["connected"])
        total_subscriptions = sum(int(status["subscriptions_count"]) for status in worker_status_sorted)

        first = worker_status_sorted[0] if worker_status_sorted else None
        return {
            "connected": connected_count > 0,
            "broker_host": first["broker_host"] if first else self._settings.mqtt_broker_host,
            "broker_port": first["broker_port"] if first else self._settings.mqtt_broker_port,
            "client_id": first["client_id"] if first else self._settings.mqtt_client_id,
            "discovery_topic": first["discovery_topic"] if first else self._settings.mqtt_discovery_topic,
            "subscriptions_count": total_subscriptions,
            "subscribed_topics": sorted({topic for status in worker_status_sorted for topic in status["subscribed_topics"]}),
            "channels_total": len(worker_status_sorted),
            "channels_connected": connected_count,
            "channels": worker_status_sorted,
        }

    def get_telemetry_status(self) -> dict[str, object]:
        with self._lock:
            last_message_ts = self._last_message_ts
            messages_received = self._messages_received
        return {
            "messages_received": messages_received,
            "last_message_ts": last_message_ts.isoformat() if last_message_ts else None,
        }

    def get_discovery_status(self) -> dict[str, object]:
        with self._lock:
            observed_topics_count = self._observed_topics_count
            last_discovery_ts = self._last_discovery_ts
        return {
            "discovery_topic": self._settings.mqtt_discovery_topic,
            "observed_topics_count": observed_topics_count,
            "last_discovery_ts": last_discovery_ts.isoformat() if last_discovery_ts else None,
        }

    def publish_json(
        self,
        *,
        topic: str,
        payload: dict[str, object],
        qos: int | None = None,
        retain: bool | None = None,
    ) -> tuple[bool, str | None]:
        qos_value = self._settings.mqtt_qos if qos is None else qos
        retain_value = False if retain is None else retain
        try:
            payload_text = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
            publish_result = self._output_client.publish(
                topic=topic,
                payload=payload_text,
                qos=qos_value,
                retain=retain_value,
            )
            publish_result.wait_for_publish(timeout=2.0)
            if publish_result.rc != mqtt.MQTT_ERR_SUCCESS:
                return False, f"mqtt publish rc={publish_result.rc}"
            return True, None
        except Exception as exc:
            self._logger.exception("mqtt publish failed topic=%s", topic)
            return False, str(exc)

    def _start_output_client(self) -> None:
        if self._output_started:
            return
        self._output_client.connect_async(
            host=self._settings.mqtt_broker_host,
            port=self._settings.mqtt_broker_port,
            keepalive=60,
        )
        self._output_client.loop_start()
        self._output_started = True

    def _stop_output_client(self) -> None:
        if not self._output_started:
            return
        self._output_client.loop_stop()
        try:
            self._output_client.disconnect()
        except Exception:
            self._logger.exception("mqtt output client disconnect failed")
        self._output_started = False

    def _handle_worker_message(
        self,
        *,
        channel_code: str,
        event_received_ts: datetime,
        normalized_key: str,
    ) -> None:
        with self._lock:
            self._messages_received += 1
            self._last_message_ts = event_received_ts
            self._seen_discovery_keys.add((channel_code, normalized_key))
            self._observed_topics_count = len(self._seen_discovery_keys)
            self._last_discovery_ts = event_received_ts


def _config_str(config_json: dict[str, Any], key: str) -> str | None:
    value = config_json.get(key)
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _config_int(config_json: dict[str, Any], key: str, fallback: int) -> int:
    value = config_json.get(key)
    if value is None:
        return fallback
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed
