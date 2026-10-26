# Troubleshooting

| Symptom | What to do |
|---|---|
| Hoodini cannot bind port 8883 | Check Mosquitto's Network card; after checking existing clients, blank or remap its 8883 host port. Hoodini's port cannot change. |
| Broker unavailable | Check `mqtt.url` includes scheme and port (`mqtt://broker.example.com:1883`); for TLS use a trusted certificate. If the add-on uses Supervisor MQTT, install/configure the broker and restart Hoodini. |
| Pairing cannot reach server | Check reserved server IPv4, firewall, TLS 8883, API 8765 and 2.4 GHz Wi-Fi. A laptop probe does not prove hood reachability. |
| No HA entities after pairing or restart | Wait 3.5–5 minutes after the hood is ready for fresh status, then check the broker and bridge/device availability. Do not force status with undocumented requests. |
| Connection rejected | Check sanitized reason logs; if the saved trust binding is wrong, forget and re-enrol only on a trusted LAN. Never share CONNECT bytes. |
| Dashboard unavailable | Check add-on ingress and logs, or standalone loopback `127.0.0.1:8099`. LAN API 8765 does not serve admin. |

If you need help, [file a redacted bug report](https://github.com/dmednis/hoodini/issues/new?template=bug.yml). See [known limitations](known-limitations.md) and [security](security.md).
