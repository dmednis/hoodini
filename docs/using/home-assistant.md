# Home Assistant entities

1. Configure the broker and [pair](../getting-started/pairing.md) the hood.
2. Wait for first status, then open **Settings → Devices & services → MQTT** and find Hoodini.
3. Control the fan or light from the device page. For an automation, use the fan or light entity as an action target and check the reported state before relying on it.

| Entity | Hidden Advance profile | Generic profile |
|---|---|---|
| Fan | Three speeds and boost | Three speeds, no boost (if the hood reports fan speed) |
| Light | Dimmable | Dimmable (if the hood reports light intensity) |
| Filters and Wi-Fi | Diagnostic sensors (some disabled by default) | Observed capabilities |

Success: reported values change only after hood status, not immediately after a command. MQTT discovery uses a device topic and both bridge and device availability. After a server restart, displayed state remains unavailable until a fresh hood status, usually 3.5–5 minutes after readiness. If entities do not appear, see [troubleshooting](../reference/troubleshooting.md).
