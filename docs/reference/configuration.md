# Configuration

Most add-on users leave options at their defaults; `config.toml` is for standalone installs. Keep passwords and tokens private.

| Setting | Add-on | Standalone TOML | Default / purpose |
|---|---|---|---|
| `log_level` | yes | yes | `INFO`; DEBUG only for local troubleshooting |
| `mqtt.url` | yes | yes | Empty: add-on uses Supervisor MQTT service; explicit URL requires scheme and port |
| `mqtt.username` | yes | yes | Optional explicit-broker username |
| `mqtt.password` | yes | yes | Optional explicit-broker password |
| `mqtt.tls` | yes | yes | `false`; enable TLS with system-trusted certificate |
| `api.port` | yes | yes | `8765`; LAN API, not hood TLS 8883 |
| `api.interfaces` | yes | yes | `[]`; advertised network interfaces |
| `api.addresses` | yes | yes | `[]`; advertised server addresses |
| `gateway.bind` | no | yes | `0.0.0.0`; hood listener binding |
| `admin.bind` | no | yes | `127.0.0.1:8099`; standalone admin only |
| `data_dir` | no | yes | `./data` native, `/data` image; `HOODINI_DATA_DIR` can override default |

The hood TLS listener is **fixed to port 8883**. The add-on uses host networking; no port mapping option exists. Explicit `mqtt.url` never inherits Supervisor service credentials; if the service is unavailable, install/configure it and restart the add-on.
