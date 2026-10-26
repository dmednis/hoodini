# Dashboard

1. In the add-on, select **Open Web UI**. Standalone: open `http://127.0.0.1:8099/` locally or through an SSH tunnel. The LAN API on 8765 does not serve admin.
2. Inspect device cards for the resolved profile, last confirmed report, fan/light and connection status. Refresh runs every five seconds except while editing, focusing details or when the tab is hidden.
3. For pairing, select **Reveal API token**, copy it into the CLI's interactive prompt and hide it again. To change a profile, choose **Automatic**, **Generic** or **Hidden Advance** and save. **Forget this hood** removes trust and discovery, not the hood's Wi-Fi settings.

![Dashboard with synthetic fixture data](https://raw.githubusercontent.com/dmednis/hoodini/main/addon/hoodini/images/dashboard.png)

Success: the page shows a device card after pairing and first status. If the card says **Waiting for first status**, allow 3.5–5 minutes; if a refresh fails, the previous report remains with a stale-data warning. See [troubleshooting](../reference/troubleshooting.md). Review and redact diagnostics before sharing.
