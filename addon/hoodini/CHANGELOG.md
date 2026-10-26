# Changelog

## Unreleased

- Show MQTT broker connection status, settings source, safe failure reasons and approximate retry timing on the dashboard.
- Show the last received Home Assistant online/offline announcement without treating an absent announcement as an error.

## 0.1.0

- Remove disk capture mode; diagnostics remain available as bounded redacted in-memory history. Legacy `capture` options are ignored with a fixed log message.
- Bundle the shared setup key; a key saved with `extract-key` overrides it.

- Add a locally bundled Pico administration dashboard: responsive status cards, live read-only refresh, profile settings, forget confirmation, on-demand token reveal/copy and expandable diagnostics.

- Initial unofficial Home Assistant add-on packaging, ingress admin and one-shot Supervisor MQTT service integration.
- Document fixed-port conflicts, first-status and state-freshness delays, and explicit MQTT option isolation.
