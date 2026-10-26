# Application-payload fixtures

These are payload bytes only, not MQTT envelopes, real topic prefixes, credentials, or device identifiers. SHA-256 digests and per-file evidence classifications appear in [manifest.json](manifest.json).

- `observed-mqtt.json`: two light-command payloads transcribed from dated 2026-08-24 reverse-engineering notes. The original raw captures were not independently inspected. Neither a paired ack nor a status report is supplied.
- `supplemental-mqtt.json`: six command recipe instantiations and nine synthetic payload cases. Their chosen sequences, timestamps, layouts and checksums are constructed test vectors, **not observations**. A synthetic ack with matching sequence is not evidence of a recorded exchange.

The observed transcription and the recipe/synthetic material have different evidentiary weight. No observed status, ack, connect or time payload is established here; synthetic vectors are not hardware evidence. Nothing here establishes MQTT packet flags or physical hood behavior. See the [protocol guide](../docs/reference/how-it-works.md) for the current wire contract and its limitations. Do not use these fixtures as authorization to connect to or provision a device.
