# How it works

**Tested protocol:** The hood speaks MQTT 3.1.1 over TLS on fixed port 8883. Its MQTT CONNECT includes a legal Will; Hoodini accepts it but does not publish it. The hood does not validate the server certificate. Per-device trust on first use (TOFU) binds the first credential to a hood identifier; it is not mutual TLS.

**Tested handshake:** The hood subscribes, publishes an application `connect` announcing firmware, receives `connected` and `time`, then subscribes to `op/mode`. Only then can commands be sent. A `ping` receives `time`. Commands receive an ACK; ACK result 1 is acceptance, not proof of a changed physical state. A later status frame confirms changes. Status frames merge capabilities; the known capability set persists until the device is forgotten.

**Tested pairing:** You put the hood into setup mode with its own buttons (hold T6 for six seconds, twice); the laptop then finds it over Bluetooth, confirms the server's TLS 8883 and LAN API reachability, and sends Wi-Fi and server information. A bundled setup key is used by default; `extract-key` saves a local optional override without printing the key. Provisioning refuses payloads over 245 bytes.

**Inferred:** Other models may report different capabilities, so controls are offered only for capability IDs the hood has reported, and values are limited by a fixed per-profile allowlist. The generic profile allows light 0–100 and fan speeds 0–3, without boost. **Unverified:** the BLE reply CRC rule, fragmentation and behaviour on other hardware. For provenance of anonymized protocol examples, see [fixture provenance](https://github.com/dmednis/hoodini/blob/main/protocol-fixtures/README.md); no raw credentials or captures are published.
