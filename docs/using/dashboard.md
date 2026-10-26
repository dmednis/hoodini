# Dashboard

1. In the add-on, select **Open Web UI**. Standalone: open `http://127.0.0.1:8099/` locally or through an SSH tunnel. The LAN API on 8765 does not serve admin.
2. Inspect device cards for the resolved profile, last confirmed report, fan/light and connection status. Refresh runs every five seconds except while editing, focusing details or when the tab is hidden.
3. For pairing, select **Reveal API token**, copy it into the CLI's interactive prompt and hide it again. To change a profile, choose **Automatic**, **Generic** or **Hidden Advance** and save. **Forget this hood** removes trust and discovery, not the hood's Wi-Fi settings.

![Dashboard with synthetic fixture data](https://raw.githubusercontent.com/dmednis/hoodini/main/addon/hoodini/images/dashboard.png)

Success: the page shows a device card after pairing and first status. If the card says **Waiting for first status**, allow 3.5–5 minutes; if a refresh fails, the previous report remains with a stale-data warning. See [troubleshooting](../reference/troubleshooting.md). Review and redact diagnostics before sharing.

## Home Assistant connection

The **MQTT broker** card shows Hoodini’s MQTT client connection to your broker, not the hood-facing TLS gateway on port 8883. **Connected** means Hoodini can publish discovery/state and receive commands; **Connecting** means an attempt is in progress, and **Disconnected** shows a classified failure reason and retry timing. This does not prove Home Assistant has processed discovery or that a hood is ready.

**Source** distinguishes a **Configured broker** from the **Supervisor MQTT service**. Only the broker host and port are shown, never credentials. **Not configured** means standalone operation without `[mqtt]`. **Unavailable** means the startup Supervisor lookup failed; install and start Mosquitto or configure MQTT, then restart Hoodini. The Supervisor lookup runs only once at startup, with no runtime rediscovery.

**Status since** is the start of the current displayed connection state. Retry timing is approximate: it shares the five-second refresh cycle, editing/detail pauses, hidden-tab pauses and stale-data behavior. Wall-clock changes can affect displayed times, but not the actual retry sleep.

**HA seen** is the last online/offline announcement received, not a live availability check or the time HA changed state. Retained announcements may describe earlier events, and the observation cannot refresh while the broker connection is down. **Not announced yet** is normal even when entities work: HA normally announces itself when its MQTT integration connects or reconnects.
