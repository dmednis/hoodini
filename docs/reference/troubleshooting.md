# Troubleshooting

| Symptom | What to do |
|---|---|
| Hoodini cannot bind port 8883 | Check Mosquitto's Network card; after checking existing clients, blank or remap its 8883 host port. Hoodini's port cannot change. |
| Broker unavailable | Inspect the dashboard’s **Home Assistant connection** card for state and classified failure reason. Check `mqtt.url` includes a scheme and valid port (`mqtt://broker.example.com:1883`); for TLS use a trusted certificate. This outbound broker connection is separate from the hood-facing gateway on 8883. |
| Supervisor MQTT service unavailable | The startup lookup failed. Install/start Mosquitto or configure MQTT (or set `mqtt.url` explicitly), then restart Hoodini. There is no runtime rediscovery. |
| HA seen: Not announced yet | Not inherently an error; entities may still work. HA birth normally accompanies MQTT connection/reconnection, which may predate Hoodini subscribing. |
| HA seen: Offline while broker Connected | Broker transport and the last HA announcement are separate. Inspect HA’s MQTT integration. The timestamp is local receipt time, not the actual HA outage start; retained announcements may describe earlier events. |
| Pairing cannot reach server | Check reserved server IPv4, firewall, TLS 8883, API 8765 and 2.4 GHz Wi-Fi. A laptop probe does not prove hood reachability. |
| No HA entities after pairing or restart | Wait 3.5–5 minutes after the hood is ready for fresh status, then check the broker and bridge/device availability. Do not force status with undocumented requests. |
| Connection rejected | Check sanitized reason logs; if the saved trust binding is wrong, forget and re-enrol only on a trusted LAN. Never share CONNECT bytes. |
| Dashboard unavailable | Check add-on ingress and logs, or standalone loopback `127.0.0.1:8099`. LAN API 8765 does not serve admin. |

If you need help, [file a redacted bug report](https://github.com/dmednis/hoodini/issues/new?template=bug.yml). See [known limitations](known-limitations.md) and [security](security.md).
