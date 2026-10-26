# Hoodini

Hoodini connects Elica Connect cooker hoods to Home Assistant locally. The hood talks to Hoodini over TLS port 8883; Hoodini publishes state and accepts commands through a separate MQTT broker.

```text
hood ── TLS 8883 ──▶ Hoodini ── MQTT ──▶ broker ◀── Home Assistant
  ▲                      ▲
  └─ one-time Bluetooth setup from a laptop (hoodini pair)
```

1. Check the [requirements](getting-started/requirements.md).
2. [Install the Home Assistant add-on](getting-started/home-assistant-addon.md) or [run standalone](getting-started/standalone.md).
3. [Pair your hood](getting-started/pairing.md).

Success: the hood reports status and appears in Home Assistant's MQTT devices. If it does not, see [troubleshooting](reference/troubleshooting.md). Read the [known limitations](reference/known-limitations.md) before changing your network.

Unofficial; not affiliated with or endorsed by Elica S.p.A.
