"""Escaped, read-only admin presentation; no device commands or private metadata."""

import html
import json
import math
import time
from datetime import UTC, datetime
from importlib.resources import files
from string import Template

from hoodini.api.schemas import as_public_dict
from hoodini.config import MqttSource
from hoodini.ha_bridge.client import BrokerStatus

_TEMPLATES = files("hoodini.api").joinpath("templates")
_DEVICE = Template(_TEMPLATES.joinpath("device.html").read_text(encoding="utf-8"))
_DIAGNOSTIC = Template(
    _TEMPLATES.joinpath("diagnostic.html").read_text(encoding="utf-8")
)
_BROKER = Template(_TEMPLATES.joinpath("broker.html").read_text(encoding="utf-8"))
_SOURCES = {
    "explicit": "Configured broker",
    "supervisor": "Supervisor MQTT service",
    "unavailable": "Supervisor MQTT service (lookup failed)",
}
_PROFILES = {"GENERIC": "Generic", "HIDDEN_ADVANCE": "Hidden Advance"}
_MQTT = {
    1: "CONNECT",
    2: "CONNACK",
    3: "PUBLISH",
    4: "PUBACK",
    8: "SUBSCRIBE",
    9: "SUBACK",
    12: "PINGREQ",
    13: "PINGRESP",
    14: "DISCONNECT",
}
_BLE = {2: "GET_INFO", 4: "SSID_LIST", 6: "NETWORK_PAR"}


def _escape(value):
    return html.escape(str(value), quote=True)


def _json(value):
    return _escape(json.dumps(value, ensure_ascii=False, indent=2))


def _time(timestamp):
    if timestamp is None:
        return "Not yet received"
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def device_card(view, csrf):
    public = as_public_dict(view)
    if view.session_state == "ready":
        status, tone = (
            ("Online", "success")
            if view.online
            else ("Waiting for first status", "waiting")
        )
    elif view.online:
        status, tone = "Reconnecting", "waiting"
    else:
        status, tone = "Disconnected", "muted"
    if status == "Waiting for first status":
        note = "Connected and command-ready. First status usually takes 3.5–5 minutes; HA stays unavailable until then."
    elif not view.online:
        note = "Waiting for the hood to connect. Previously stored state is not shown as live."
    elif status == "Reconnecting":
        note = "Recent confirmed state is shown while the hood reconnects. Commands wait for a ready session."
    else:
        note = (
            "Showing confirmed hood reports. Use Home Assistant to control this device."
        )
    fan = light = "Awaiting fresh status"
    if view.online:
        speed, mode = view.caps.get(110), view.caps.get(64)
        fan = (
            "Boost"
            if mode == 5
            else (
                "Not reported"
                if speed is None
                else "Off"
                if speed == 0
                else f"Speed {speed}"
            )
        )
        intensity = view.caps.get(96)
        light = (
            "Not reported"
            if intensity is None
            else ("Off" if intensity == 0 else f"{intensity}%")
        )
    options = []
    for value, label in (("", "Automatic"), *_PROFILES.items()):
        selected = " selected" if value == (view.profile_override or "") else ""
        options.append(f'<option value="{value}"{selected}>{label}</option>')
    return _DEVICE.substitute(
        cuid=_escape(view.cuid),
        csrf=_escape(csrf),
        status=_escape(status),
        tone=tone,
        note=_escape(note),
        profile=_escape(_PROFILES.get(view.profile.key, view.profile.key)),
        model=_escape(public["info"].get("PRF", "Model not identified")),
        fan=_escape(fan),
        light=_escape(light),
        rssi=_escape(
            f"{view.rssi} dBm"
            if view.online and view.rssi is not None
            else "Not available"
        ),
        last_seen=_escape(_time(view.last_seen)),
        capabilities=len(view.known_ids),
        options="".join(options),
        raw=_json(public),
    )


def broker_card(
    status: BrokerStatus | None,
    source: MqttSource | None,
    *,
    now: float | None = None,
) -> str:
    now = time.time() if now is None else now
    if status is None:
        endpoint, since, ha = "None", "Not available", "Not available"
        ha_note = "HA announcements can be observed only while a broker connection is running."
        if source == "unavailable":
            badge, tone = "Unavailable", "error"
            note = (
                "Hoodini could not obtain the Supervisor MQTT service at startup. "
                "Install and start the Mosquitto add-on or configure MQTT, then "
                "restart Hoodini. You can also configure mqtt.url explicitly and restart."
            )
        else:
            badge, tone = "Not configured", "muted"
            note = (
                "No broker is configured, so Home Assistant entities are not published. "
                "Add an [mqtt] section to the configuration and restart Hoodini."
            )
    else:
        endpoint = status.endpoint or "Invalid broker URL"
        since = (
            _time(status.since)
            if status.since is not None
            else "Starting"
            if status.state == "connecting"
            else "Not available"
        )
        if status.state == "connecting":
            badge, tone = "Connecting", "waiting"
            note = (
                f"Previous connection failed: {status.reason}. Trying again."
                if status.reason
                else "Connecting to the MQTT broker."
            )
        elif status.state == "connected":
            badge, tone = "Connected", "success"
            note = (
                "Connected to the MQTT broker. Hoodini can publish discovery and "
                "state and receive commands."
            )
        elif status.state == "retrying":
            badge, tone = "Disconnected", "error"
            if status.retry_at is None:
                retry = "Preparing to retry."
            else:
                remaining = math.ceil(status.retry_at - now)
                retry = (
                    f"Retrying in about {remaining} s."
                    if remaining > 0
                    else "Retrying shortly."
                )
            note = (
                f"Broker connection failed: {status.reason}. {retry}"
                if status.reason
                else f"Connection closed. {retry}"
            )
        elif status.state == "stopped":
            badge, tone = "Stopped", "muted"
            note = "The bridge is shutting down or stopped. Automatic retry is paused."
        else:
            badge, tone = "Unknown", "muted"
            note = "Broker connection status is not available."
        if status.ha_state is None:
            ha = "Not announced yet"
            ha_note = (
                "Home Assistant announces itself when its MQTT integration connects. "
                "No announcement yet is normal; entities may still work."
            )
        else:
            ha = f"{status.ha_state.capitalize()} — last received {_time(status.ha_seen_at)}"
            ha_note = (
                "This is the last announcement Hoodini received, not a live "
                "availability check. Retained announcements may describe an earlier event."
            )
            if status.state != "connected":
                ha_note += (
                    " It cannot be refreshed while the broker connection is down."
                )
    return _BROKER.substitute(
        status=_escape(badge),
        tone=tone,
        note=_escape(note),
        endpoint=_escape(endpoint),
        source=_escape(_SOURCES.get(source, "Not configured")),
        since=_escape(since),
        ha=_escape(ha),
        ha_note=_escape(ha_note),
    )


def snapshot(
    devices,
    diagnostics,
    csrf,
    *,
    broker: BrokerStatus | None = None,
    broker_source: MqttSource | None = None,
):
    views = devices.list()
    records = diagnostics.history
    recent = []
    for record in reversed(records[-50:]):
        kind = record.get("kind")
        code = record.get("packet_type") if kind == "mqtt" else record.get("command")
        names = _MQTT if kind == "mqtt" else _BLE
        recent.append(
            _DIAGNOSTIC.substitute(
                time=_escape(_time(record.get("timestamp"))),
                kind=_escape(str(kind).upper()),
                name=_escape(names.get(code, str(code))),
                direction="Received" if record.get("direction") == "in" else "Sent",
                length=_escape(record.get("length", 0)),
                raw=_json(record),
            )
        )
    return {
        "devices": "".join(device_card(view, csrf) for view in views)
        or (
            '<article class="empty"><h3>No hoods paired yet</h3>'
            "<p>Pair from a Bluetooth-capable laptop using the Hoodini CLI. "
            "Once the hood connects, it will appear here.</p>"
            "<p>Browser Bluetooth pairing is not included in this version.</p></article>"
        ),
        "diagnostics": "".join(recent)
        or '<p class="empty">No diagnostic traffic yet.</p>',
        "broker": broker_card(broker, broker_source),
        "summary": f"{len(views)} paired · {sum(view.online for view in views)} online",
        "diagnostic_summary": f"Latest {len(recent)} of {len(records)} buffered records. Newest first.",
    }
