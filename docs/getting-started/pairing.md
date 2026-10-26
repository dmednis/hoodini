# Pair your hood

1. On a trusted Bluetooth laptop with Python 3.12+ and [uv](https://docs.astral.sh/uv/), install the CLI from the release tag (not PyPI):

   ```sh
   uv tool install --python 3.12 "hoodini[pairing] @ git+https://github.com/dmednis/hoodini@v0.1.0#subdirectory=server"
   hoodini --version
   hoodini pair --help
   ```

2. Reserve the server IPv4 in DHCP, ensure its TLS 8883 and LAN API 8765 are reachable, and prepare a 2.4 GHz Wi-Fi network. In the dashboard select **Reveal API token** for the interactive CLI prompt; never put the token in shell history.
3. Hold the hood's T6 control for six seconds to reset Wi-Fi, then six seconds again for the roughly five-minute Bluetooth setup window.
4. Run `hoodini pair --server 192.0.2.10 --ssid 'YOUR_WIFI_NAME'` (substitute your reserved server address and Wi-Fi name), or omit `--server` to discover and confirm a server address. Enter the Wi-Fi password and API token when prompted.

Success: the CLI reports a ready session and the first status appears in about **3.5–5 minutes after command readiness**; then MQTT entities appear. If the CLI times out, inspect the dashboard later before retrying. See [troubleshooting](../reference/troubleshooting.md).

The CLI uses a bundled setup key. If you have your own legally obtained app copy and need an override, install `hoodini[extract]` from the same Git tag and run `hoodini extract-key /path/to/your/app.apk`; the extracted key is saved locally, not printed. Never share it or the app.

!!! warning
    Laptop reachability does not prove the hood can reach the server. See [known limitations](../reference/known-limitations.md).
