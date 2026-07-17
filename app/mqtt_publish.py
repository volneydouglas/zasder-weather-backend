"""MQTT publishing with Home Assistant auto-discovery.

When MQTT_HOST is set, a background task publishes each device's latest reading
to `<prefix>/<node>/state` and, once per device, retained Home Assistant MQTT
discovery configs to `<disc_prefix>/sensor/...` so every sensor auto-appears in
HA with the right unit/device-class. The payload builders are pure + unit-
tested; the connection uses paho-mqtt (optional import — a no-op if it's not
installed or MQTT_HOST is unset).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from . import db
from .config import settings

log = logging.getLogger("zasder.mqtt")

_PUBLISH_INTERVAL_S = 30

# HA sensor map: (field, friendly, unit, device_class, state_class, icon).
_SENSORS: list[tuple[str, str, str | None, str | None, str, str | None]] = [
    ("tempf",          "Temperature",     "°F",   "temperature", "measurement", None),
    ("feelsLike",      "Feels Like",      "°F",   "temperature", "measurement", None),
    ("dewPoint",       "Dew Point",       "°F",   "temperature", "measurement", None),
    ("humidity",       "Humidity",        "%",    "humidity",    "measurement", None),
    ("baromrelin",     "Pressure",        "inHg", "pressure",    "measurement", None),
    ("windspeedmph",   "Wind Speed",      "mph",  "wind_speed",  "measurement", None),
    ("windgustmph",    "Wind Gust",       "mph",  "wind_speed",  "measurement", None),
    ("winddir",        "Wind Direction",  "°",    None,          "measurement", "mdi:compass-outline"),
    ("dailyrainin",    "Rain Today",      "in",   "precipitation", "total_increasing", None),
    ("hourlyrainin",   "Rain Rate",       "in/h", "precipitation_intensity", "measurement", None),
    ("uv",             "UV Index",        None,   None,          "measurement", "mdi:weather-sunny"),
    ("solarradiation", "Solar Radiation", "W/m²", "irradiance",  "measurement", None),
]


def _node(mac: str) -> str:
    """Topic-safe node id from a MAC (compact, lowercase)."""
    return mac.replace(":", "").replace("-", "").lower()


def state_topic(topic_prefix: str, mac: str) -> str:
    return f"{topic_prefix}/{_node(mac)}/state"


def discovery_messages(device: dict[str, Any], topic_prefix: str = "zasder",
                       disc_prefix: str = "homeassistant"
                       ) -> list[tuple[str, dict[str, Any]]]:
    """(config_topic, payload) per HA discovery sensor for one device."""
    mac = device["mac"]
    node = _node(mac)
    name = device.get("name") or mac
    dev_block = {
        "identifiers": [f"zasder_{node}"],
        "name": name,
        "manufacturer": "Zasder Weather",
        "model": "Weather Station",
    }
    st = state_topic(topic_prefix, mac)
    out: list[tuple[str, dict[str, Any]]] = []
    for field, friendly, unit, dclass, sclass, icon in _SENSORS:
        cfg: dict[str, Any] = {
            "name": friendly,
            "unique_id": f"zasder_{node}_{field}",
            "object_id": f"zasder_{node}_{field}",
            "state_topic": st,
            "value_template": f"{{{{ value_json.{field} }}}}",
            "availability_topic": f"{topic_prefix}/{node}/status",
            "device": dev_block,
        }
        if unit:
            cfg["unit_of_measurement"] = unit
        if dclass:
            cfg["device_class"] = dclass
        if sclass:
            cfg["state_class"] = sclass
        if icon:
            cfg["icon"] = icon
        out.append((f"{disc_prefix}/sensor/zasder_{node}/{field}/config", cfg))
    return out


def state_message(device: dict[str, Any], topic_prefix: str = "zasder"
                  ) -> tuple[str, dict[str, Any]]:
    """(state_topic, payload) — the device's non-null current readings."""
    last = device.get("lastData") or {}
    payload = {f: last.get(f) for f, *_ in _SENSORS if last.get(f) is not None}
    return state_topic(topic_prefix, device["mac"]), payload


class MqttPublisher:
    """Background task: connects to MQTT and republishes state every 30s +
    retained HA discovery once per device. No-op if paho-mqtt is missing."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._client: Any = None
        self._announced: set[str] = set()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="mqtt-publisher")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            log.warning("MQTT_HOST is set but paho-mqtt is not installed; "
                        "MQTT publishing disabled")
            return
        client = mqtt.Client()
        if settings.mqtt_username:
            client.username_pw_set(settings.mqtt_username, settings.mqtt_password)
        try:
            client.connect(settings.mqtt_host, settings.mqtt_port, keepalive=60)
            client.loop_start()
        except Exception as e:
            log.warning("MQTT connect to %s:%s failed: %s",
                        settings.mqtt_host, settings.mqtt_port, e)
            return
        self._client = client
        log.info("MQTT publisher connected to %s:%s (prefix=%s)",
                 settings.mqtt_host, settings.mqtt_port, settings.mqtt_topic_prefix)
        while not self._stop.is_set():
            try:
                await self._publish(client)
            except Exception as e:
                log.exception("MQTT publish failed: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_PUBLISH_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass

    async def _publish(self, client: Any) -> None:
        prefix = settings.mqtt_topic_prefix
        disc = settings.mqtt_discovery_prefix
        for d in await db.list_devices():
            node = _node(d["mac"])
            if node not in self._announced:
                for topic, cfg in discovery_messages(d, prefix, disc):
                    client.publish(topic, json.dumps(cfg), retain=True)
                client.publish(f"{prefix}/{node}/status", "online", retain=True)
                self._announced.add(node)
            topic, payload = state_message(d, prefix)
            if payload:
                client.publish(topic, json.dumps(payload), retain=True)
