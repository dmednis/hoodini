# Update Hoodini

1. Back up your Hoodini data directory and record the installed add-on version.
2. In the Home Assistant add-on store, select **Update** when a new version is available. For Compose, update the pinned image tag before `docker compose pull && docker compose up -d`; for native use `git pull && uv sync` in your checkout and restart.
3. Check logs and the [dashboard](../using/dashboard.md). After a restart, wait for fresh status before trusting Home Assistant availability.

Success: the new version starts, broker discovery reconnects and entities come back after the first hood report. If it fails, restore your backup and check [troubleshooting](../reference/troubleshooting.md). Do not run two instances on host port 8883.
