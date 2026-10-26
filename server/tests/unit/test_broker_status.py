"""Safe broker projections and classifier output, without broker I/O."""

import ssl
from dataclasses import FrozenInstanceError

import aiomqtt
import pytest
from hoodini.config import MqttSettings
from hoodini.ha_bridge.client import HaBridge, _target, broker_failure_reason


@pytest.mark.parametrize(
    "url,tls,endpoint",
    [
        ("mqtt://broker", False, "broker:1883"),
        ("mqtt://broker:1884", False, "broker:1884"),
        ("mqtts://broker", False, "broker:8883"),
        ("mqtt://broker", True, "broker:8883"),
        ("mqtt://127.0.0.1", False, "127.0.0.1:1883"),
        ("mqtt://[::1]", False, "[::1]:1883"),
        (
            "mqtt://synthetic-user:synthetic-secret@broker:1884/path?q=secret#fragment",
            False,
            "broker:1884",
        ),
        ("http://broker", False, None),
        ("mqtt:///path", False, None),
        ("mqtt://broker:bad", False, None),
        ("mqtt://broker:65536", False, None),
        ("mqtt://broker:0", False, None),
        ("mqtt://broker:", False, None),
        ("mqtt://[invalid", False, None),
    ],
)
def test_broker_endpoint_projection(url, tls, endpoint):
    settings = MqttSettings(url, tls=tls)
    assert HaBridge(settings, None, None).status.endpoint == endpoint
    if endpoint is None:
        with pytest.raises(ValueError) as caught:
            _target(settings)
        assert str(caught.value) == "invalid MQTT bridge URL"
        assert caught.value.__cause__ is caught.value.__context__ is None
    else:
        host, port, _ = _target(settings)
        assert str(port) in endpoint and host in endpoint


def test_broker_status_is_immutable_and_initially_connecting():
    bridge = HaBridge(MqttSettings("mqtt://broker"), None, None)
    status = bridge.status
    assert status.state == "connecting" and status.endpoint == "broker:1883"
    assert (
        status.since,
        status.reason,
        status.retry_at,
        status.ha_state,
        status.ha_seen_at,
    ) == (None,) * 5
    with pytest.raises(FrozenInstanceError):
        status.state = "connected"
    with pytest.raises(AttributeError):
        bridge.status = status
    assert bridge._task is None


class SecretReason:
    def __str__(self):
        return "SYNTHETICSECRET"


@pytest.mark.parametrize(
    "error,expected",
    [
        (OSError("refused SYNTHETICSECRET"), "connection refused"),
        (TimeoutError("timed out SYNTHETICSECRET"), "connection timed out"),
        (
            OSError("name resolution SYNTHETICSECRET"),
            "broker host name could not be resolved",
        ),
        (ssl.SSLError("SYNTHETICSECRET"), "TLS handshake failed"),
        (OSError("network unreachable SYNTHETICSECRET"), "network unreachable"),
        (ValueError("SYNTHETICSECRET"), "invalid broker URL"),
        (Exception("SYNTHETICSECRET"), "connection failed"),
        (
            aiomqtt.MqttCodeError(5, "SYNTHETICSECRET"),
            "broker rejected the connection (code 5)",
        ),
        (
            aiomqtt.MqttCodeError(SecretReason(), "SYNTHETICSECRET"),
            "broker rejected the connection",
        ),
    ],
)
def test_broker_failure_reason_never_echoes_error_text(error, expected):
    assert broker_failure_reason(error) == expected
    assert "SYNTHETICSECRET" not in broker_failure_reason(error)
